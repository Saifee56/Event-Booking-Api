# Event Ticket Booking API: Documentation

A REST API for booking seats at events. Users can browse events, hold a seat for 10 minutes, and confirm the booking.
Admins manage events and users. The main focus of the project is **preventing double-booking** when many people try
to book the same seat at the same time.

---

## Table of contents

1. [Tech stack](#1-tech-stack)
2. [Project structure](#2-project-structure)
3. [Getting started](#3-getting-started)
4. [Environment variables](#4-environment-variables)
5. [Architecture](#5-architecture)
6. [Data model](#6-data-model)
7. [Authentication (JWT)](#7-authentication-jwt)
8. [Roles and permissions](#8-roles-and-permissions)
9. [API reference](#9-api-reference)
10. [The booking flow and concurrency](#10-the-booking-flow-and-concurrency)
11. [Caching](#11-caching)
12. [Request logging middleware](#12-request-logging-middleware)
13. [Docker setup](#13-docker-setup)
14. [Nginx](#14-nginx)
15. [Security](#15-security)
16. [Testing the API by hand](#16-testing-the-api-by-hand)
17. [Troubleshooting](#17-troubleshooting)
18. [Known limitations and next steps](#18-known-limitations-and-next-steps)

---

## 1. Tech stack

| Tool | Version | What it's used for |
|---|---|---|
| Python | 3.12 | Language |
| Django | 6.1 | Web framework, ORM, admin panel, migrations |
| Django REST Framework | 3.18 | Building the REST API (viewsets, serializers, permissions) |
| Simple JWT | 5.5 | JWT login, token refresh, and blacklisting |
| PostgreSQL | 16 | Main database (the source of truth) |
| Redis | 7 | Cache, and the lock that holds a seat |
| Gunicorn | 26 | Production web server that runs Django (3 worker processes) |
| Nginx | 1.27 | Reverse proxy in front of Gunicorn; serves static files |
| Docker Compose | — | Runs all four services with one command |

---

## 2. Project structure

```
config/                          ← project root: run all docker compose commands here
├── docker-compose.yml           ← the 4 services: db, redis, web, nginx
├── Dockerfile                   ← how the "web" image is built
├── .dockerignore                ← files kept out of the image (.env, .venv, caches)
├── .env                         ← real settings and secrets (never committed)
├── .env.example                 ← template for .env
├── requirements.txt             ← Python packages, all version-pinned
├── manage.py                    ← Django's command-line tool
│
├── nginx/
│   └── default.conf             ← Nginx config: proxy to Django, serve /static/
│
├── config/                      ← the Django *project* (settings and wiring)
│   ├── settings.py              ← all settings, read from environment variables
│   ├── urls.py                  ← root URLs: /admin/ and /api/v1/
│   ├── middleware.py            ← RequestLoggingMiddleware (request ID + timing)
│   ├── wsgi.py                  ← entry point Gunicorn uses
│   └── asgi.py                  ← async entry point (not used)
│
├── auth_service/                ← app: users, login, roles
│   ├── models.py                ← custom User (logs in with email)
│   ├── serializers.py           ← Register, Login, User, ChangeRole serializers
│   ├── views.py                 ← AuthViewSet (register/login/refresh/logout/me), UserViewSet
│   ├── permissions.py           ← ModelPermissions, IsAdminRole, IsUserRole, IsOwnerOrAdmin
│   ├── urls.py                  ← /auth/... routes
│   ├── admin.py                 ← User in the Django admin panel
│   └── migrations/
│       ├── 0001_initial.py      ← users table
│       └── 0002_seed_groups.py  ← creates the "admin" and "users" roles
│
└── events/                      ← app: events, seats, bookings
    ├── models.py                ← Event, Seat, Booking
    ├── serializers.py           ← EventSerializer, SeatSerializer, BookingSerializer
    ├── views.py                 ← EventViewSet (event CRUD + seat list, with caching)
    ├── seat_viewset.py          ← SeatViewSet (hold a seat with a Redis lock)
    ├── booking_viewset.py       ← BookingViewSet (confirm booking, my bookings)
    ├── urls.py                  ← /events/... and /bookings/... routes
    └── migrations/
```

**Project vs app:** in Django, the *project* (`config/config/`) holds settings and global wiring. An *app* is one
feature area with its own models, views and URLs. Splitting the code into `auth_service` and `events` keeps each
part small and easy to find.

---

## 3. Getting started

### Requirements
- Docker and Docker Compose. Nothing else is needed: Python, Postgres and Redis all run inside containers.

### First run

```bash
cd config

# 1. Create your settings file and fill in real values (secret key, DB password)
cp .env.example .env

# 2. Build the image and start everything in the background
docker compose up -d --build

# 3. Check that all 4 containers are running and db/redis are "healthy"
docker compose ps

# 4. Create an admin account for the admin panel
docker compose exec web python manage.py createsuperuser
```

Then open:
- **API:** http://localhost/api/v1/
- **Admin panel:** http://localhost/admin/

`/` returns 404. That's expected, because there's no homepage.

**What happens on startup:** the `web` container runs `migrate` (creates or updates the tables), then
`collectstatic` (copies CSS and JS for the admin panel into a shared volume for Nginx), and then starts Gunicorn.

### Everyday commands

| Task | Command |
|---|---|
| Start | `docker compose up -d` |
| Rebuild after code changes | `docker compose up -d --build web` |
| See the logs | `docker compose logs -f web` |
| Stop | `docker compose down` |
| Stop and **delete all data** | `docker compose down -v` |
| Django shell | `docker compose exec web python manage.py shell` |
| Postgres shell | `docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'` |
| Redis shell | `docker compose exec redis redis-cli` |
| Make migrations | `docker compose run --rm -v "$(pwd)/events/migrations:/app/events/migrations" web python manage.py makemigrations events` |

**Why rebuild after code changes?** The code is copied *into* the image when it's built. There's no live folder
mount, so a container keeps running the old code until you rebuild.

**Why the extra `-v` for makemigrations?** Files created inside a container disappear when it's removed. Mounting
the migrations folder makes the new migration file appear in your real project folder.

---

## 4. Environment variables

All settings come from `.env`, so the same code runs in dev and production with different values, and secrets are
never written in the code.

| Variable | Example | Meaning |
|---|---|---|
| `DJANGO_SECRET_KEY` | long random string | Used to sign data. Must be secret. |
| `DJANGO_DEBUG` | `False` | `True` shows detailed error pages. Never use `True` in production. |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Domain names Django will answer to |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | `ticketdb` / `ticket_app` / … | Database name and login. The `db` container uses them to create the DB. |
| `POSTGRES_HOST` / `POSTGRES_PORT` | `localhost` / `5433` | For connecting from your machine. Compose overrides them to `db` / `5432` inside Docker. |
| `REDIS_URL` | `redis://redis:6379/0` | Redis address (`redis` = the service name, `0` = database number) |
| `JWT_SIGNING_KEY` | empty | Key used to sign tokens. Empty = use `DJANGO_SECRET_KEY`. |
| `JWT_ACCESS_MINUTES` | `15` | Access token lifetime |
| `JWT_REFRESH_DAYS` | `7` | Refresh token lifetime |
| `AUTH_LOGIN_THROTTLE_RATE` | `5/min` | Max login attempts per IP |
| `AUTH_REGISTER_THROTTLE_RATE` | `5/min` | Max sign-ups per IP |
| `DJANGO_SECURE_HTTPS` | `False` | Set to `True` only once Nginx serves HTTPS. It turns on the HTTPS redirect and secure cookies. |
| `DJANGO_HSTS_SECONDS` | `3600` | How long browsers must use HTTPS only (only when HTTPS is on) |

---

## 5. Architecture

### The containers

```
                    your computer
 ┌───────────────────────────────────────────────────────────┐
 │  Browser / Postman / curl                                 │
 │        │ http://localhost  (port 80)                      │
 └────────┼──────────────────────────────────────────────────┘
          ▼
 ┌─────────────────── Docker network ────────────────────────┐
 │                                                           │
 │  ┌──────────┐  /static/ → files from the "static" volume  │
 │  │  nginx   │                                             │
 │  └────┬─────┘                                             │
 │       │ everything else → http://web:8000                 │
 │       ▼                                                   │
 │  ┌──────────────────────┐                                 │
 │  │  web                 │                                 │
 │  │  Gunicorn (3 workers)│                                 │
 │  │  └─ Django           │                                 │
 │  └────┬───────────┬─────┘                                 │
 │       │           │                                       │
 │       ▼           ▼                                       │
 │  ┌─────────┐ ┌─────────┐                                  │
 │  │   db    │ │  redis  │                                  │
 │  │Postgres │ │  cache  │                                  │
 │  │ (data)  │ │ + locks │                                  │
 │  └─────────┘ └─────────┘                                  │
 └───────────────────────────────────────────────────────────┘
```

- **Postgres** stores everything permanent: users, events, seats, bookings. It's the source of truth.
- **Redis** stores temporary things: cached API responses and seat hold locks. If Redis is emptied, nothing
  important is lost, because Postgres still has the real data.

### What happens inside Django for one request

```
1. RequestLoggingMiddleware   → gives the request an ID, starts a timer
2. Django URL router          → finds the view for /api/v1/...
3. JWTAuthentication          → reads "Authorization: Bearer <token>" → request.user
                                 (missing or invalid token on a protected endpoint → 401)
4. Permission classes         → is this user allowed?   (no → 403)
5. Throttle                   → too many requests?      (yes → 429; login/register only)
6. The view                   → validates input with a serializer, reads/writes Postgres and Redis
7. Serializer                 → turns the Python objects into JSON
8. RequestLoggingMiddleware   → adds X-Request-ID and X-Response-Time headers, writes one log line
```

---

## 6. Data model

```
 ┌──────────┐        ┌───────────┐        ┌──────────┐        ┌──────────┐
 │   User   │1──────*│  Booking  │*──────1│   Seat   │*──────1│  Event   │
 └──────────┘        └───────────┘        └──────────┘        └──────────┘
                                                                  │*
 User 1──────────────────────────────────────────────────────* created_by
```

### User (`auth_service/models.py`, table `users`)

| Field | Notes |
|---|---|
| `email` | Used to log in instead of a username. Unique, ignoring case (`Alice@x.com` = `alice@x.com`). |
| `password` | Stored as a hash, never in plain text |
| `first_name`, `last_name` | Optional |
| `groups` | The user's role (`admin` or `users`) |
| `is_superuser` | Can do everything |

**Why a custom User model?** Django's default user logs in with a username. This one uses email. A custom user model
has to be set up before the first migration (`AUTH_USER_MODEL = "auth_service.User"`), because changing it later
is very hard.

### Event

| Field | Notes |
|---|---|
| `name`, `venue`, `starts_at` | |
| `created_by` | The admin who created it (set from the token, not from the request body) |
| `created_at`, `updated_at` | Set automatically |

- **Constraint:** `UniqueConstraint(venue, starts_at)`. A venue can't have two events at the same time. This also
  stops a double-clicked "create" button from making two copies.
- **Index:** on `starts_at`, because events are sorted by it.

### Seat

| Field | Notes |
|---|---|
| `event` | The event it belongs to. Deleting the event deletes its seats (`CASCADE`). |
| `seat_number` | Text: `"1"`, `"2"`, … |
| `status` | `available` or `booked`. "held" is **not** stored here; it comes from an active Booking. |

- **Constraint:** `UniqueConstraint(event, seat_number)`. No duplicate seat numbers within one event.
- Seats are created automatically when an event is created (`seat_count` in the request), using one
  `bulk_create` query inside a transaction.

### Booking

| Field | Notes |
|---|---|
| `user`, `seat` | Who booked which seat. Both use `PROTECT`: you can't delete a user or seat that has bookings. |
| `status` | `held` → `confirmed`, or `held` → `expired` |
| `expires_at` | When the hold ends (created time + 10 minutes) |
| `idempotency_key` | The key from the confirm request. Unique. |
| `created_at`, `confirmed_at` | |

**Constraint: `unique_active_booking_per_seat`.** This is a **partial** unique constraint: a seat can have only ONE
booking whose status is `held` or `confirmed`. Expired bookings are ignored by the rule, so they can be kept as
history. In SQL it becomes:

```sql
CREATE UNIQUE INDEX ... ON events_booking (seat_id) WHERE status IN ('held', 'confirmed');
```

This is the database's own guarantee against double-booking. Even if the application code had a bug, Postgres would
reject the second booking.

**Booking lifecycle:**

```
            hold                 confirm (in time)
  (none) ─────────► held ─────────────────────────► confirmed
                      │
                      │ 10 minutes pass
                      ▼
                   expired   (the seat can be held again)
```

---

## 7. Authentication (JWT)

### What is a JWT?

A JSON Web Token is a signed string that says "this is user 5". The server signs it with a secret key when you log
in. On later requests, the server only checks the signature; it doesn't look anything up in the database. That's why
JWT is called **stateless**.

### Two tokens

| Token | Lifetime | Used for |
|---|---|---|
| **access** | 15 minutes | Sent with every request: `Authorization: Bearer <access>` |
| **refresh** | 7 days | Only sent to `/auth/refresh/` to get a new access token |

**Why two?** If an access token is stolen, it stops working within 15 minutes. The refresh token is long-lived but
sent rarely, so it's exposed less.

### The flow

```
POST /auth/login/  {email, password}
   ← {access, refresh, user}

GET /events/list_events/   Authorization: Bearer <access>
   ← 200 ...

(15 minutes later)
GET /events/list_events/   Authorization: Bearer <access>
   ← 401 token expired

POST /auth/refresh/  {refresh}
   ← {access: NEW, refresh: NEW}     the old refresh token is now blacklisted

POST /auth/logout/  {refresh}
   ← 205                              the refresh token is blacklisted, so it can't be used again
```

### Extra protections
- **Refresh rotation + blacklist:** every refresh returns a *new* refresh token and blacklists the old one. A stolen
  refresh token can only be used once.
- **Logout** blacklists the refresh token in the database (`token_blacklist` app).
- **Throttling:** login and register allow 5 requests per minute per IP. This slows down password guessing.
- **The same error for wrong email and wrong password** ("Invalid credentials."), so attackers can't find out
  which emails are registered.
- **Password validation:** Django's validators reject short, common, all-number passwords, and passwords too
  similar to the email.
- **Auth endpoints skip JWT checking**, so an expired token left in the header can't block login or refresh.

---

## 8. Roles and permissions

### The roles

There are two roles, stored as Django **Groups**:

| Role | Who | Can do |
|---|---|---|
| `admin` | Staff | Create, update and delete events; see everyone's bookings; manage user roles |
| `users` | Customers | View events and seats; hold seats; confirm their own bookings |
| superuser | The owner | Everything (made with `createsuperuser`) |

- Groups are created by the data migration `0002_seed_groups.py`, so every database (dev, test, production) gets the
  same roles automatically.
- **Register always puts new users in `users`.** The register serializer only accepts email, name and password, so
  nobody can sign up as an admin.
- Only an admin can change someone's role (`PATCH /auth/users/{id}/role/`). An admin can't change their own role,
  so the last admin can't lock everyone out by accident.

### How permissions are checked

```
User ──belongs to──► Group ──has──► Permissions (view_event, add_event, …)
```

Django creates four permissions for every model automatically: `add_`, `change_`, `delete_`, `view_`. Each group is
given a set of them.

### The permission classes (`auth_service/permissions.py`)

| Class | Rule | Used on |
|---|---|---|
| `IsAuthenticated` | Must be logged in. This is the **global default**, so every endpoint is protected unless it opts out. | Everything |
| `ModelPermissions` | The HTTP method decides which permission is needed: GET→`view`, POST→`add`, PATCH→`change`, DELETE→`delete` | Events |
| `IsAdminRole` | `admin` group or superuser | User management |
| `IsUserRole` | `users` group. Admins don't book seats. | Hold, confirm |
| `IsOwnerOrAdmin` | Object-level: the booking belongs to you, or you're an admin | (available for single bookings) |

### 401 vs 403

| Code | Meaning | Example |
|---|---|---|
| **401 Unauthorized** | "I don't know who you are" | No token, or an expired token |
| **403 Forbidden** | "I know who you are, but you can't do this" | A customer tries to create an event |

### Example: `POST /events/create_event/`

| Caller | Check | Result |
|---|---|---|
| No token | not logged in | 401 |
| Customer (`users`) | has `events.add_event`? No | 403 |
| Admin (`admin`) | has `events.add_event`? Yes | 201 |

---

## 9. API reference

Base URL: `http://localhost/api/v1`

Unless a row below says otherwise, send `Authorization: Bearer <access token>` and `Content-Type: application/json`.

Errors always look like `{"detail": "message"}`, or `{"field": ["message"]}` for validation errors.

### 9.1 Auth

#### `POST /auth/register/`: create an account
No token needed. Throttled to 5 per minute.

Request:
```json
{
  "email": "alice@test.com",
  "first_name": "Alice",
  "last_name": "Smith",
  "password": "Str0ngPass!23",
  "confirm_password": "Str0ngPass!23"
}
```
Response `201`:
```json
{ "id": 7, "email": "alice@test.com", "first_name": "Alice", "last_name": "Smith", "role": "users" }
```
Errors: `400` email already used, passwords don't match, or the password is too weak. `429` too many requests.

#### `POST /auth/login/`
No token needed. Throttled to 5 per minute.

Request:
```json
{ "email": "alice@test.com", "password": "Str0ngPass!23" }
```
Response `200`:
```json
{
  "access": "eyJhbGciOi...",
  "refresh": "eyJhbGciOi...",
  "user": { "id": 7, "email": "alice@test.com", "first_name": "Alice", "last_name": "Smith", "role": "users" }
}
```
Errors: `401` invalid credentials. `429` too many attempts.

#### `POST /auth/refresh/`
No token needed. Request: `{ "refresh": "<refresh token>" }`

Response `200`: `{ "access": "<new>", "refresh": "<new>" }`. The old refresh token stops working.

Errors: `401` the token is invalid, expired or already used.

#### `POST /auth/logout/`
Request: `{ "refresh": "<refresh token>" }` → `205` (no body). The refresh token is blacklisted.

#### `GET /auth/me/`
Response `200`: the logged-in user (the same shape as register's response).

#### `GET /auth/users/` (admin only)
Response `200`: a list of all users with their roles.

#### `PATCH /auth/users/{id}/role/` (admin only)
Request: `{ "role": "admin" }` or `{ "role": "users" }`

Response `200`: the updated user. The user's previous role is replaced, not added to.

Errors: `400` unknown role, or you tried to change your own role. `403` not an admin. `404` no such user.

### 9.2 Events

#### `GET /events/list_events/`
Who: any logged-in user. **Cached for 5 minutes.**

Response `200`:
```json
[
  {
    "id": 3,
    "name": "Comedy Show",
    "venue": "Hall A",
    "starts_at": "2026-12-01T19:00:00Z",
    "created_by": 1,
    "created_at": "2026-10-06T10:00:00Z",
    "updated_at": "2026-10-06T10:00:00Z"
  }
]
```

#### `POST /events/create_event/` (admin)
Request:
```json
{ "name": "Comedy Show", "venue": "Hall A", "starts_at": "2026-12-01T19:00:00Z", "seat_count": 100 }
```
- `seat_count` (1–1000) is required when creating. It creates seats `"1"` to `"100"` in the same transaction: either
  the event and all its seats are saved, or nothing is.
- `created_by` is taken from the token. The client can't set it.

Response `201`: the event. Errors: `400` missing `seat_count`, or the venue already has an event at that time.

#### `GET /events/{id}/get_event/`
Response `200`: one event. `404` if it doesn't exist.

#### `PATCH /events/{id}/update_event/` (admin)
Send only the fields you want to change, e.g. `{ "name": "New name" }`. `seat_count` can't be changed (`400`).

#### `DELETE /events/{id}/delete_event/` (admin)
`204` deleted (its seats are deleted with it). `409` if the event has bookings, because bookings protect their seats.

#### `GET /events/{id}/seats/`
Who: any logged-in user. **Cached for 30 seconds.**

Response `200`:
```json
[
  { "id": 71, "seat_number": "1",  "status": "available" },
  { "id": 72, "seat_number": "2",  "status": "held" },
  { "id": 73, "seat_number": "3",  "status": "booked" },
  { "id": 80, "seat_number": "10", "status": "available" }
]
```
- The status is worked out in this order: `booked` (stored on the seat) → `held` (there's an active, unexpired hold)
  → `available`.
- Sorted 1, 2, … 10 (by length, then text), not 1, 10, 2 the way plain text sorting would.

### 9.3 Seats and bookings

#### `POST /events/{event_id}/seats/{seat_id}/hold/` (users group)
Holds the seat for 10 minutes. No body.

Response `201`:
```json
{
  "booking_id": 4,
  "event_id": 3,
  "event_name": "Comedy Show",
  "seat_id": 75,
  "seat_number": "5",
  "status": "held",
  "expires_at": "2026-10-06T14:02:40Z"
}
```
| Error | When |
|---|---|
| `403` | You're not in the `users` group (e.g. an admin) |
| `404` | The seat doesn't exist or doesn't belong to that event |
| `409` | The seat is already booked, or someone else is holding it |

#### `POST /bookings/{id}/confirm/` (users group)
Turns your hold into a confirmed booking. No body.

**Required header:** `Idempotency-Key: <a unique string, max 64 chars>`. You make this key yourself (a UUID is a
good choice). Use a new key for each new confirm attempt, and the **same** key when retrying the same attempt.

Response `200`:
```json
{
  "id": 4,
  "user_email": "alice@test.com",
  "event_id": 3,
  "event_name": "Comedy Show",
  "seat": 75,
  "seat_number": "5",
  "status": "confirmed",
  "created_at": "2026-10-06T13:52:40Z",
  "confirmed_at": "2026-10-06T13:55:47Z"
}
```
(`expires_at` is only shown while a booking is `held`.)

| Response | When |
|---|---|
| `200` | Confirmed. Also returned for a **retry with the same key** (same body, nothing changes) |
| `400` | The `Idempotency-Key` header is missing or too long |
| `403` | You're not in the `users` group |
| `404` | The booking doesn't exist, or it isn't yours |
| `409` | Already confirmed (with a different key), the hold expired, or the key was already used for another booking |

#### `GET /bookings/me/`
Who: any logged-in user. Customers get their own bookings; admins get everyone's. Newest first.

Response `200`: a list of bookings (the same shape as confirm's response).

### 9.4 Status codes used

| Code | Meaning in this API |
|---|---|
| 200 | OK |
| 201 | Created (register, create event, hold) |
| 204 | Deleted |
| 205 | Logged out |
| 400 | Bad input |
| 401 | Not logged in, or bad token |
| 403 | Logged in but not allowed |
| 404 | Not found (or not yours) |
| 409 | Conflict: seat taken, already confirmed, hold expired, event has bookings |
| 429 | Too many requests |

---

## 10. The booking flow and concurrency

### The problem

A popular show goes on sale and 20 people click "book seat 5" at the same moment. If the code did this:

```
1. read seat 5 → it's free
2. create a booking for seat 5
```

then all 20 requests could finish step 1 before any of them reaches step 2. They'd all see "free" and all create a
booking. This is called a **race condition**: the result depends on which request runs first.

The project solves it in **layers**, so that if one layer fails, another one catches the problem.

### Layer 1: Redis lock (hold)

In `seat_viewset.py`:

```python
locked = cache.add(hold_key(seat.id), request.user.id, HOLD_TTL)   # HOLD_TTL = 600 s
```

`cache.add` sends this Redis command:

```
SET ticket:1:seat:75:hold <user_id> NX EX 600
```

- `NX` = only set the key if it does **N**ot e**X**ist yet.
- `EX 600` = the key deletes itself after 600 seconds (10 minutes).

Redis runs commands **one at a time**, so when 20 requests arrive together, exactly one `SET NX` succeeds. The other
19 get `False` and receive `409`. Checking and setting happen in one step, so nothing can sneak in between: this is
what **atomic** means.

**Why Redis?** It works in memory and is very fast. The 19 losing requests are rejected without touching Postgres,
so the database stays free under heavy load.

### Layer 2: Database constraint (hold)

After winning the lock, the booking is created in a transaction:

```python
with transaction.atomic():
    # free this seat's old expired holds first (see "Lazy expiry" below)
    Booking.objects.filter(seat=seat, status=HELD, expires_at__lte=now).update(status=EXPIRED)
    booking = Booking.objects.create(user=..., seat=seat, status=HELD, expires_at=...)
```

If Postgres already has an active booking for this seat (for example, Redis restarted and lost its keys), the
partial unique constraint raises an `IntegrityError`. The view then gives the Redis lock back and returns `409`.
Redis is fast, but **Postgres is the source of truth**.

### Full hold flow

```
POST /events/3/seats/75/hold/
        │
        ▼
 seat exists in this event? ── no ──► 404
        │
        ▼
 seat.status == booked? ── yes ──► 409 "already booked"
        │
        ▼
 Redis SET NX EX 600 ── fails ──► 409 "already held"
        │ succeeds
        ▼
 BEGIN
   mark this seat's expired holds as "expired"
   INSERT booking (held) ── IntegrityError ──► release Redis key ──► 409
 COMMIT
        │
        ▼
 delete the cached seat list for event 3
        │
        ▼
 201 { booking_id, expires_at, ... }
```

### Layer 3: Row lock (confirm)

In `booking_viewset.py`:

```python
with transaction.atomic():
    booking = (Booking.objects.select_related("seat")
               .select_for_update()
               .filter(pk=pk, user=request.user).first())
    ...
    booking.status = CONFIRMED
    booking.save()
    seat.status = BOOKED
    seat.save()
```

- **`select_for_update()`** adds `FOR UPDATE` to the SQL. Postgres **locks** the booking and seat rows until the
  transaction ends. If a second confirm for the same booking arrives, it **waits** at this line. When the first one
  commits, the second one continues and reads the *updated* row, sees `confirmed`, and doesn't confirm again.
- **`transaction.atomic()`** makes the two writes all-or-nothing. You never end up with a confirmed booking on a
  seat that isn't marked `booked`.
- **`filter(user=request.user)`**: you can only confirm your own booking. Someone else's booking gives 404, which
  doesn't even reveal that it exists.

### Layer 4: Idempotency key (confirm)

**Idempotent** means doing something twice has the same effect as doing it once.

**The problem:** a user clicks "Confirm" and the network times out. The app doesn't know whether the server
received the request, so it retries. Without protection, a retry could book or charge twice.

**The solution:** the client creates a unique key for each confirm *attempt* and sends it as the `Idempotency-Key`
header. A retry sends the **same** key.

```
 attempt 1:  key = "a1b2"  ──► server confirms, saves "a1b2" on the booking ──► 200 ✗ (response lost)
 retry:      key = "a1b2"  ──► server: "a1b2" already used for this booking ──► returns the same 200
```

How the server handles the key:

| Situation | Result |
|---|---|
| No key | `400` |
| Key never seen before | Confirm normally and store the key on the booking |
| Key already used for **this** booking by **you** | `200` with the same data, nothing changes |
| Key already used for a **different** booking | `409` |
| Two requests with the same key at the exact same time | The row lock makes one wait; it then sees the booking confirmed with its key → `200` |

**Why does the client create the key, not the server?** If the server created a new key for each request, a retry
would get a new key and look like a brand-new request. The key's whole job is to say "this is the same attempt as
before". Stripe's payment API uses the same pattern.

### Full confirm flow

```
POST /bookings/4/confirm/   Idempotency-Key: a1b2
        │
        ▼
 header missing/too long? ── yes ──► 400
        │
        ▼
 key already used? ── for this booking ──► 200 (same data)
        │          └─ for another booking ─► 409
        ▼
 BEGIN
   SELECT booking + seat ... FOR UPDATE         (row lock)
   not found / not yours            ──► 404
   already confirmed                ──► 409  (200 if it's the same key)
   expired                          ──► mark expired ──► 409
   booking → confirmed, seat → booked, save the key
 COMMIT
        │
        ▼
 delete the Redis hold key and the cached seat list
        │
        ▼
 200 { status: "confirmed", ... }
```

### Lazy expiry (without a background job)

A hold must end after 10 minutes. Usually a background job (like Celery Beat) would mark old holds as `expired`
every minute. This project doesn't use one; expiry is handled **lazily**, at the moment it matters:

| Place | What it does |
|---|---|
| Redis | The lock key deletes itself after 10 minutes (`EX 600`) |
| `GET /events/{id}/seats/` | Only counts a hold as active if `expires_at > now`, so expired holds show as `available` |
| Hold | Marks that seat's expired holds as `expired` before creating a new one |
| Confirm | Refuses an expired hold and marks it `expired` |

**Trade-off:** if nobody touches a seat again, its old booking stays `held` in the database even after expiring.
Users never see wrong seat availability, but `/bookings/me/` may show a stale `held` booking. A scheduled cleanup
job would fix this.

---

## 11. Caching

### Pattern: cache-aside

```
 GET /events/list_events/
        │
        ▼
  key in Redis? ── yes ──► return it (Postgres isn't touched)
        │ no
        ▼
  query Postgres ──► save the result in Redis with a TTL ──► return it
```

The application manages the cache itself: it reads the cache first and fills it on a miss. This is the most common
caching pattern.

### Cache keys

| Key (in code) | Stored in Redis as | TTL | Deleted when |
|---|---|---|---|
| `events:list` | `ticket:1:events:list` | 5 min | An event is created, updated or deleted |
| `events:{id}:seats` | `ticket:1:events:3:seats` | 30 s | A seat in that event is held or confirmed, or the event is deleted |

`ticket` is the `KEY_PREFIX` from settings, and `1` is Django's cache version number.

### Design decisions

- **Delete on write (invalidation):** when data changes, the key is deleted, and the next read rebuilds it from
  Postgres. This is simpler and safer than trying to update the cached JSON.
- **The delete happens after a successful write only.** For example, a failed delete (409) doesn't clear the cache,
  because nothing changed.
- **Short TTL for seats (30 s):** seat status changes often. Even if an invalidation were missed, the data could be
  at most 30 seconds old.
- **One key per event:** holding a seat in event 3 only clears event 3's seat cache. Other events stay cached.
- **The cache isn't trusted for correctness.** The hold and confirm endpoints always check Postgres and Redis locks
  directly. A stale cache can only make a seat *look* available for a few seconds; the hold would then return 409.

### Avoiding slow queries (N+1)

- The seat list works out "is this seat held?" with **one** SQL query, using `Exists()` as a subquery, instead of
  one extra query per seat.
- Bookings use `select_related("user", "seat__event")`, which loads booking + user + seat + event with a single JOIN.

### Looking at the cache

```bash
docker compose exec redis redis-cli
> KEYS *                         # list all keys
> TTL ticket:1:events:list       # seconds left before it expires
> MONITOR                        # watch every command live (Ctrl+C to stop)
```

---

## 12. Request logging middleware

`config/middleware.py` → `RequestLoggingMiddleware`, registered at the end of `MIDDLEWARE` in settings.

### What it does on every request

**Before the view:**
1. Reads the `X-Request-ID` header from the client. If it's valid (letters, digits and dashes, max 64 characters),
   it's reused. Otherwise a new random ID is generated.
2. Saves it as `request.request_id`, so other code can use it.
3. Starts a timer.

**After the view:**
1. Adds the `X-Request-ID` and `X-Response-Time` headers to the response.
2. Writes one log line:

```
2026-10-06 13:55:47 INFO request: POST /api/v1/bookings/4/confirm/ 200 alice@test.com:users 12.40ms 3f9c2a7b...
                                  method  path                      status user:role       time    request id
```

### Why

- **Request ID:** when a user reports a problem, they (or the frontend) can share the ID from the response header.
  Searching the logs for it shows exactly what happened. In a system with several services, the same ID is passed
  along so one request can be followed everywhere.
- **Validating the incoming ID** stops people from injecting fake or broken lines into the logs.
- **Logging after the view:** DRF checks the JWT inside the view, so only *after* the view does `request.user`
  contain the real user.
- **Response time** helps spot slow endpoints.

**How middleware works:** Django wraps the view in every middleware in the `MIDDLEWARE` list, like layers of an
onion. Code before `self.get_response(request)` runs on the way in; code after it runs on the way out.

---

## 13. Docker setup

### Dockerfile (builds the `web` image)

```dockerfile
FROM python:3.12-slim                         # small official Python image
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
                                              # no .pyc files; print logs immediately
WORKDIR /app
RUN pip install --no-cache-dir --upgrade pip
COPY requirements.txt .                       # copy ONLY requirements first...
RUN pip install --no-cache-dir -r requirements.txt
COPY . .                                      # ...then the code
RUN useradd -m appuser && mkdir -p /app/staticfiles /app/media && chown -R appuser /app
USER appuser                                  # don't run as root
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60"]
```

- **Layer caching:** Docker caches each step. Requirements are installed *before* the code is copied, so editing
  code doesn't reinstall all the packages, which makes rebuilds fast.
- **Non-root user:** if an attacker breaks into the app, they aren't root inside the container.
- **`PYTHONUNBUFFERED=1`:** logs show up in `docker compose logs` immediately.
- **`.dockerignore`** keeps `.env` (secrets), `.venv` and caches out of the image.

### docker-compose.yml

| Service | Image | Ports | Notes |
|---|---|---|---|
| `db` | postgres:16 | host `5433` → `5432` | Data in the `pgdata` volume. Healthcheck: `pg_isready` every 5 s |
| `redis` | redis:7-alpine | none | Healthcheck: `redis-cli ping` every 5 s |
| `web` | built from the Dockerfile | `expose: 8000` (internal only) | Runs migrate → collectstatic → gunicorn. Waits for db and redis to be **healthy**. |
| `nginx` | nginx:1.27-alpine | host `80` → `80` | The only public entry point |

**Volumes:**

| Volume | Holds | Shared by |
|---|---|---|
| `pgdata` | Database files (survive restarts; deleted only by `down -v`) | db |
| `static` | Admin CSS/JS from collectstatic | web (writes), nginx (reads) |
| `media` | Uploaded files | web, nginx |

### Key Docker concepts used

- **Healthchecks + `depends_on: condition: service_healthy`:** a container being *started* doesn't mean the app
  inside is *ready*. Postgres needs a few seconds to accept connections. Without this, Django would try to migrate
  too early and crash.
- **Docker DNS:** containers on the same Compose network find each other by service name. Django connects to
  `db:5432` and `redis:6379`; Nginx connects to `web:8000`.
- **`expose` vs `ports`:** `ports` opens a port on your computer; `expose` only makes it reachable inside the Docker
  network. Gunicorn is only reachable through Nginx.
- **Host port 5433 for Postgres:** port 5432 was already taken by a Postgres installed directly on the machine.
- **Environment override:** `.env` says `POSTGRES_HOST=localhost` (for tools on your machine). Compose sets
  `POSTGRES_HOST=db` for the web container, because inside Docker, "localhost" means the container itself.

---

## 14. Nginx

`nginx/default.conf`:

```nginx
upstream django {
    server web:8000;                    # the Gunicorn container (Docker DNS)
}

server {
    listen 80;
    server_name localhost 127.0.0.1;
    client_max_body_size 20M;           # max upload size
    gzip on;                            # compress responses
    gzip_types text/css application/javascript application/json image/svg+xml;

    location /static/ {                 # admin CSS/JS: served directly from the volume
        alias /app/staticfiles/;
        expires 30d;                    # browsers may cache them for 30 days
        access_log off;
    }

    location /media/ {
        alias /app/media/;
    }

    location / {                        # everything else → Django
        proxy_pass http://django;
        proxy_set_header Host $host;                                   # original domain
        proxy_set_header X-Real-IP $remote_addr;                       # real client IP
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;   # chain of IPs
        proxy_set_header X-Forwarded-Proto $scheme;                    # http or https
        proxy_redirect off;
    }
}
```

### Why put Nginx in front of Gunicorn?

| Reason | Explanation |
|---|---|
| Static files | Nginx sends files much faster than Python, and Django doesn't have to handle them at all |
| Slow clients | Nginx buffers slow uploads and downloads, so Gunicorn's workers aren't kept busy waiting |
| One entry point | HTTPS, gzip, upload limits and (later) load balancing across several `web` containers all live in one place |
| Security | Gunicorn isn't exposed to the internet directly |

**Proxy headers:** without them, Django would think every request came from Nginx's IP over plain http.
`X-Forwarded-Proto` together with Django's `SECURE_PROXY_SSL_HEADER` tells Django when the original request used
HTTPS.

**Why Gunicorn instead of `manage.py runserver`?** `runserver` is a single-process development server and isn't made
for real traffic. Gunicorn runs several worker processes (3 here), so it can handle several requests in parallel.

---

## 15. Security

| Measure | Where |
|---|---|
| Every endpoint needs login unless it opts out (`IsAuthenticated` default) | settings `REST_FRAMEWORK` |
| Role-based permissions on every write | `permissions.py` |
| Users can only see and confirm their own bookings | `booking_viewset.py` queryset filters |
| Clients can't set protected fields (role, `created_by`, booking fields) | serializers (`read_only_fields`, limited `fields`) |
| Short-lived access tokens, refresh rotation, blacklist on logout | Simple JWT settings |
| Login/register throttling (5/min) | `ScopedRateThrottle` |
| Same error for wrong email or password | login view |
| Password strength validation | `AUTH_PASSWORD_VALIDATORS` |
| Email unique regardless of case | DB constraint on `Lower(email)` |
| Secrets in `.env`, not in code or the image | `.env`, `.dockerignore` |
| Container runs as a non-root user | Dockerfile |
| Only Nginx is public | compose `expose` vs `ports` |
| HTTPS redirect, secure cookies and HSTS when `DJANGO_SECURE_HTTPS=True` | settings |
| Incoming request IDs validated before logging | middleware |
| 404 (not 403) for other users' bookings, so IDs aren't revealed | booking confirm |

---

## 16. Testing the API by hand

### With Postman

1. `POST {{url}}/auth/login/` with a JSON body → copy `access` from the response.
2. On each next request: **Authorization** tab → type **Bearer Token** → paste the token.
3. For confirm: **Headers** tab → add `Idempotency-Key` with a value like `test-123`.

### A full booking run with curl

```bash
API=http://localhost/api/v1

# Log in as a customer
TOKEN=$(curl -s -X POST $API/auth/login/ -H "Content-Type: application/json" \
  -d '{"email":"alice@test.com","password":"Str0ngPass!23"}' | jq -r .access)

# See the seats of event 3
curl -s $API/events/3/seats/ -H "Authorization: Bearer $TOKEN" | jq

# Hold seat 75
curl -s -X POST $API/events/3/seats/75/hold/ -H "Authorization: Bearer $TOKEN" | jq

# Confirm booking 4 (run it twice with the same key: same result, no duplicate)
curl -s -X POST $API/bookings/4/confirm/ -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: test-123" | jq

# My bookings
curl -s $API/bookings/me/ -H "Authorization: Bearer $TOKEN" | jq
```

### Checking the race-condition protection

Fire 20 hold requests at one seat at the same time, from 20 different user tokens. Exactly one should return `201`.

```bash
for t in "${TOKENS[@]}"; do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST $API/events/3/seats/76/hold/ \
    -H "Authorization: Bearer $t" &
done; wait
```

### Checking the data in Postgres

```bash
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "select id, user_id, seat_id, status, idempotency_key from events_booking order by id;"'
```

---

## 17. Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| A new endpoint returns Django's "Page not found" page | The container is running old code (there's no live code mount) | `docker compose up -d --build web` |
| `web` keeps restarting | A migration or startup error | `docker compose logs web` |
| `could not translate host name "db"` | Running Django outside Docker with `POSTGRES_HOST=db` | Run commands through `docker compose exec web ...` |
| Port 5432 already in use | A local Postgres is running | The compose file already uses host port 5433 |
| A new migration file is missing from the project | It was created inside a container and lost | Use the makemigrations command with the `-v` mount (section 3) |
| `401` on every request | The access token expired (15 min) | Log in again, or call `/auth/refresh/` |
| `403` on hold/confirm as admin | Only the `users` group books seats | Use a customer account |
| `429` on login/register | Throttle limit (5/min) | Wait a minute |
| `/` returns 404 | There's no homepage | Use `/api/v1/...` or `/admin/` |
| The seat list looks a few seconds out of date | 30-second cache | Expected; hold/confirm always check the real data |

---

## 18. Known limitations and next steps

| Not done | Plan |
|---|---|
| Background jobs (Celery) | Add a worker for booking-confirmation emails and a scheduled job to mark expired holds |
| Automated tests | pytest tests for role 403s, idempotent confirm, hold conflicts, and the 20-parallel-holds race |
| Pagination | `/events/list_events/` and `/bookings/me/` return everything; add DRF pagination |
| HTTPS | Terminate TLS in Nginx (e.g. Let's Encrypt), then set `DJANGO_SECURE_HTTPS=True` |
| Production settings | `DJANGO_DEBUG=False`, don't publish the Postgres port, keep secrets in a secrets manager |
| Scaling | Run several `web` containers behind Nginx (the app is stateless: JWT + Redis + Postgres); add Postgres read replicas |
| Cancel booking | An endpoint to cancel a hold or a booking and free the seat |
