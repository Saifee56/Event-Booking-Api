# Event Ticket Booking API: Project Guide for Interviews

## 1. The 30-second pitch

> "I built a ticket booking REST API with Django REST Framework, Postgres, and Redis, running in Docker behind Nginx.
> The hard problem in ticket booking is **two people trying to buy the same seat at the same time**, so I solved it in
> layers: a **Redis lock** when a seat is held, a **database constraint** as a safety net, **`select_for_update` row
> locks** when a booking is confirmed, and an **idempotency key** so a retried request never books twice.
> It also has JWT auth, role-based permissions with Django Groups, Redis caching, and a custom logging middleware
> that gives every request an ID."

**Tech stack:** Python 3.12, Django 6.1, Django REST Framework, Simple JWT, PostgreSQL 16, Redis 7, Gunicorn, Nginx,
Docker Compose.

---

## 2. Architecture: how a request travels

```
 Browser / Postman
        │  http://localhost/api/v1/...
        ▼
 ┌──────────────┐   port 80 (the only port open to the outside)
 │    Nginx     │── /static/ → serves files directly (fast, Django isn't touched)
 └──────┬───────┘
        │ proxy_pass http://web:8000   (Docker DNS resolves "web")
        ▼
 ┌──────────────┐
 │  Gunicorn    │  3 worker processes, each running Django
 │  + Django    │
 └──┬────────┬──┘
    │        │
    ▼        ▼
 ┌──────┐ ┌───────┐
 │Postgres││ Redis │   Postgres = the truth (users, events, seats, bookings)
 │  (db) ││       │   Redis    = fast and temporary (cache + seat locks)
 └──────┘ └───────┘
```

Inside Django, every request goes through:

```
Request → RequestLoggingMiddleware (gives it a request ID, starts a timer)
        → JWTAuthentication            (who are you?        no/bad token → 401)
        → permission classes           (are you allowed?    no → 403)
        → view                         (does the work)
        → middleware again             (adds X-Request-ID + X-Response-Time, writes one log line)
```

---

## 3. Data model

```
User ──< Booking >── Seat >── Event
             │
   status: held → confirmed
                ↘ expired
```

| Model | Key fields | Important rules |
|---|---|---|
| **User** | email (used to log in), groups | Custom user model (`AUTH_USER_MODEL`) that logs in with email, not username |
| **Event** | name, venue, starts_at, created_by | `UniqueConstraint(venue, starts_at)`: one venue can't host two events at the same time |
| **Seat** | event, seat_number, status (available/booked) | `UniqueConstraint(event, seat_number)` |
| **Booking** | user, seat, status (held/confirmed/expired), expires_at, idempotency_key, confirmed_at | **Partial unique constraint**: only ONE held/confirmed booking per seat. `idempotency_key` is unique. |

**Why a partial unique constraint?** Expired bookings are kept as history, so a seat can have many *expired* bookings
but only one *active* one. Even if all my application code had a bug, Postgres itself would refuse a second active
booking. This is the last line of defense.

**`on_delete=PROTECT` on Booking:** an event that has bookings can't be deleted by accident. The API returns
**409** instead.

---

## 4. API endpoints

All endpoints are under `/api/v1/`.

| Method | Endpoint | Who | What it does |
|---|---|---|---|
| POST | `/auth/register/` | anyone | Create an account (always in the `users` role) |
| POST | `/auth/login/` | anyone | Returns an access token and a refresh token |
| POST | `/auth/refresh/` | anyone | Get a new access token |
| POST | `/auth/logout/` | anyone | Blacklists the refresh token |
| GET | `/auth/me/` | logged in | My profile and role |
| GET | `/auth/users/` | admin | List users and their roles |
| PATCH | `/auth/users/{id}/role/` | admin | Change a user's role |
| GET | `/events/list_events/` | logged in | List events (cached) |
| POST | `/events/create_event/` | admin | Create an event and its seats |
| GET | `/events/{id}/get_event/` | logged in | One event |
| PATCH | `/events/{id}/update_event/` | admin | Update an event |
| DELETE | `/events/{id}/delete_event/` | admin | Delete an event (409 if it has bookings) |
| GET | `/events/{id}/seats/` | logged in | Seats with status available/held/booked (cached) |
| POST | `/events/{event_id}/seats/{seat_id}/hold/` | users | Hold a seat for 10 minutes |
| POST | `/bookings/{id}/confirm/` | users | Confirm a held seat (needs the `Idempotency-Key` header) |
| GET | `/bookings/me/` | logged in | My bookings (admins see everyone's) |

---

## 5. Authentication: JWT

- **Login** returns two tokens:
  - **access token** (15 min): sent with every request as `Authorization: Bearer <token>`
  - **refresh token** (7 days): only used to get a new access token
- **Why JWT?** It's stateless. The server checks the token's signature and doesn't need to look up a session in the
  database. This suits APIs and mobile apps.
- **Why a short access token?** If it's stolen, it only works for 15 minutes.
- **Refresh rotation + blacklist:** each refresh issues a new refresh token and blacklists the old one. Logout
  blacklists the refresh token, so it can't be used again.
- **Throttling:** login and register are limited to 5 requests per minute, which slows down password guessing.
- **Register can't choose a role:** the serializer only accepts email, name, and password. A new user always goes in
  the `users` group, so nobody can sign up as an admin.

---

## 6. Roles and permissions

Roles are **Django Groups**: `admin` and `users`. Each group holds Django's built-in model permissions
(`view_event`, `add_event`, ...). The groups are created by a **data migration**, so every new database has the same
roles.

| Permission class | Rule | Used on |
|---|---|---|
| `IsAuthenticated` (global default) | must be logged in | everything, unless it opts out |
| `ModelPermissions` | HTTP method → permission (GET→view, POST→add, PATCH→change, DELETE→delete) | events |
| `IsAdminRole` | admin group or superuser | user management |
| `IsUserRole` | users group | hold, confirm |
| Queryset filter | users only see their own rows | `/bookings/me/`, confirm |

**401 vs 403:** 401 = "I don't know who you are". 403 = "I know who you are, but you can't do this".

**Why Groups and not a `role` text column?** Permissions become data, not code. Giving a role a new ability means
adding a permission in the admin panel, with no code change or deploy. It's also Django's standard built-in system.

**Secure by default:** the global default is `IsAuthenticated`, so if I forget to protect a new endpoint, it's
still protected.

---

## 7. The main problem: booking without double-selling ⭐

This is the part to explain best in an interview.

### Step 1: Hold a seat (`POST /events/{e}/seats/{s}/hold/`)

```
20 users click "hold seat 5" at the same moment
        │
        ▼
 Is the seat already booked in the DB?  ── yes → 409
        │ no
        ▼
 Redis: SET seat:5:hold <user_id> NX EX 600      ← cache.add() in Django
        │
   ┌────┴─────────────────────────┐
   │ 1 request gets True          │ 19 requests get False → 409 "already held"
   ▼                              
 In a DB transaction:
   - mark this seat's expired old holds as "expired"
   - create Booking(status=held, expires_at=now+10min)
        │
        ├─ IntegrityError (DB constraint said no) → give the Redis lock back → 409
        ▼
 Delete the cached seat list → 201
```

- **`NX`** = "only set it if the key does Not eXist". Redis runs commands one at a time, so only one request can
  win. This makes it an **atomic** lock.
- **`EX 600`** = the key deletes itself after 10 minutes, so a hold can never get stuck forever.
- **Why Redis first and not just the DB?** Redis is in memory and very fast. It rejects the 19 losers before they
  ever touch Postgres, which protects the database under heavy traffic.
- **Why also the DB constraint?** Redis is not the source of truth. Redis could restart and lose its keys. The
  partial unique constraint in Postgres is the safety net.

### Step 2: Confirm the booking (`POST /bookings/{id}/confirm/`)

```
 Idempotency-Key header present?  ── no → 400
        │
 Key already used?
   ├─ for THIS booking → return the same 200 again (nothing changes)
   └─ for another booking → 409
        │ new key
        ▼
 BEGIN TRANSACTION
   SELECT ... FROM booking JOIN seat WHERE id=.. AND user=me FOR UPDATE   ← row lock
     not mine / doesn't exist → 404
     already confirmed        → 409 (or 200 if it's the same key)
     hold expired             → mark it "expired" → 409
     booking.status = confirmed, confirmed_at = now, idempotency_key = key
     seat.status = booked
 COMMIT
        ▼
 Delete the Redis hold key and the cached seat list → 200
```

- **`transaction.atomic()`**: the booking update and the seat update both happen, or neither does. You never end
  up with a confirmed booking on a seat that isn't marked booked.
- **`select_for_update()`**: locks the booking and seat rows until the transaction ends. If two confirms arrive at
  the same time, the second one **waits** and then reads the already-updated row, so it can't confirm twice. This
  prevents a **race condition** (two requests read "held" at the same time and both write "confirmed").

### What is an idempotency key?

**Idempotent** means doing something twice has the same effect as doing it once.

Problem: the user clicks "Confirm", the network times out, and the app retries. Did the first request work? Without
protection, the retry could book twice or charge twice.

Solution: the **client** generates a unique key (a UUID) for each attempt and sends it in the `Idempotency-Key`
header. A retry sends the **same** key. The server stores the key with the booking (unique column). If the key comes
again, the server returns the first result without doing the work again. Stripe's payment API works the same way.

**Why must the client make the key?** If the server generated one, every retry would get a new key and look like a
new request.

### Hold expiry without Celery (lazy expiry)

A hold lasts 10 minutes. Instead of a background job, expired holds are cleaned up **when they matter**:

- **The seat list** checks `expires_at > now`, so an expired hold already shows as available.
- **Hold** first marks that seat's expired holds as `expired`, then creates the new hold.
- **Confirm** refuses an expired hold and marks it `expired`.
- **The Redis lock** deletes itself after 10 minutes (`EX 600`).

**Trade-off:** an old booking can still show `held` in the DB until someone touches that seat. A scheduled job
(Celery Beat or cron) would clean these up. I left it out to keep the project simple.

---

## 8. Caching with Redis

**Pattern: cache-aside.**

```
GET /events/list_events/
   ├─ in Redis? → return it (Postgres isn't touched)
   └─ not in Redis → query Postgres → save to Redis with a TTL → return it
```

| Cache key | TTL | Deleted when |
|---|---|---|
| `events:list` | 5 min | an event is created, updated, or deleted |
| `events:{id}:seats` | 30 s | a seat is held or confirmed, or the event is deleted |

- **Invalidation on write:** after a change, the code deletes the key, and the next read rebuilds it. This is
  simpler and safer than updating the cached data in place.
- **Short TTL on seats:** seat status changes often, so 30 seconds limits how stale the data can be even if a
  delete is missed.
- **One key per event:** holding a seat in event 3 only clears event 3's seat cache.
- **`KEY_PREFIX="ticket"`:** keys look like `ticket:1:events:list`, which makes them easy to find in `redis-cli`.
- **N+1 avoided:** the seat list computes "is held" in one SQL query with `Exists()` instead of one query per
  seat, and `select_related` loads booking + seat + event in one query.

---

## 9. Custom middleware: request logging

`RequestLoggingMiddleware` runs on every request:

1. **Before the view:** reuses the client's `X-Request-ID` if it's valid (letters, digits and dashes, max 64
   characters), otherwise generates a UUID. Starts a timer.
2. **After the view:** adds the `X-Request-ID` and `X-Response-Time` response headers and writes one log line:

```
POST /api/v1/bookings/4/confirm/ 200 alice@test.com:users 12.40ms 3f9c2a...
```

- **Why a request ID?** When a user reports an error, you search the logs for one ID and see exactly what happened.
  In microservices, the same ID is passed along to other services so one request can be traced across them.
- **Why validate the incoming ID?** So nobody can inject junk or fake lines into the logs.
- **Why log after the view?** DRF checks the JWT inside the view, so only then does `request.user` hold the real
  user.

---

## 10. Docker and Nginx

**Services in `docker-compose.yml`:**

| Service | Image | Job |
|---|---|---|
| `db` | postgres:16 | Database. Data kept in the `pgdata` volume. |
| `redis` | redis:7-alpine | Cache and locks |
| `web` | built from the Dockerfile | Runs migrate → collectstatic → gunicorn (3 workers) |
| `nginx` | nginx:1.27-alpine | Reverse proxy on port 80; serves static files |

What I learned:

- **Healthchecks + `depends_on: condition: service_healthy`:** `web` waits until Postgres answers `pg_isready` and
  Redis answers `PING`. Otherwise Django would crash on start because the DB isn't ready yet.
- **Docker DNS:** containers reach each other by service name (`db`, `redis`, `web:8000`), not by IP.
- **`expose` vs `ports`:** `web` only exposes 8000 inside the Docker network. Only Nginx's port 80 is published to
  the host, so all traffic must go through Nginx.
- **Non-root user in the Dockerfile:** if the app is hacked, the attacker isn't root inside the container.
- **Layer caching:** `requirements.txt` is copied and installed *before* the code, so changing code doesn't
  reinstall every package.
- **`.dockerignore`:** keeps `.env` (secrets) and `.venv` out of the image.
- **Config from env vars (`.env`):** the same image runs in dev and prod with different settings. Secrets are never
  in the code.
- **Volumes:** the DB data survives `docker compose down`. Only `down -v` deletes it.

**Why Nginx in front of Gunicorn?**
- It serves static files much faster than Python.
- It buffers slow clients, so Gunicorn workers aren't kept busy.
- It's one place for gzip, HTTPS, and (later) load balancing across several `web` containers.
- `X-Forwarded-For` / `X-Forwarded-Proto` headers tell Django the real client IP and whether the request was HTTPS.

**Why Gunicorn and not `runserver`?** `runserver` is a single-process development server. Gunicorn runs several
worker processes for production.

---

## 11. What I learned (short list)

1. **Concurrency is the hard part of booking systems**: race conditions, atomic locks, row locks, transactions.
2. **Defense in depth**: Redis lock + DB constraint + row lock + idempotency key. Each layer covers another
   layer's weak spot.
3. **Redis has two jobs**: a cache (speed) and a distributed lock (correctness).
4. **Cache invalidation**: delete on write, short TTLs for data that changes often.
5. **JWT auth**: access/refresh tokens, rotation, blacklisting, throttling.
6. **RBAC with Django Groups**, and the difference between 401 and 403.
7. **Writing middleware** and why request IDs matter for debugging.
8. **Docker Compose**: multi-container setup, healthchecks, volumes, networks, env config.
9. **Nginx as a reverse proxy**.
10. **DB design**: unique and partial constraints, indexes, `PROTECT` vs `CASCADE`, avoiding N+1 queries.

---

## 12. Likely interview questions and short answers

**Q: Two users click "book" on the same seat at the same millisecond. What happens?**
Both reach the Redis `SET NX`. Redis handles commands one at a time, so exactly one gets the lock. The other gets
409. If Redis somehow failed, the partial unique constraint in Postgres would still allow only one active booking.

**Q: Why not just use the database lock and skip Redis?**
That would work for correctness, but under heavy traffic every request would hit Postgres and wait on locks. Redis
rejects losers in memory, much faster, and keeps the DB free.

**Q: What does `select_for_update` do?**
It adds `FOR UPDATE` to the SQL query, which locks the selected rows until the transaction commits. Other
transactions that try to lock the same rows wait. It must be used inside `transaction.atomic()`.

**Q: What if the user's network drops after they click confirm?**
The app retries with the same `Idempotency-Key`. The server sees the key was already used for that booking and
returns the same result. Nothing is booked twice.

**Q: What if the user never confirms?**
After 10 minutes the Redis lock deletes itself, and the DB hold is marked expired the next time someone tries that
seat or the user tries to confirm. The seat becomes available again.

**Q: How do you keep the cache from showing old data?**
Delete the key on every write that changes the data (invalidation), and use short TTLs as a backup.

**Q: Can a normal user see another user's booking?**
No. `/bookings/me/` filters by the logged-in user, and confirm looks up the booking with `user=request.user`, so
another user's booking returns 404 (which also doesn't reveal that it exists).

**Q: Why return 404 and not 403 for someone else's booking?**
403 would confirm that the booking ID exists. 404 leaks nothing.

**Q: How would you scale this?**
Run more `web` containers behind Nginx (the app is stateless thanks to JWT, Redis, and Postgres). Add Postgres read
replicas, Redis persistence or a Redis cluster, and a task queue for slow work like emails.

---

## 13. Trade-offs and what I'd add next

| Not done (yet) | Why / what I'd do |
|---|---|
| Celery (emails, expiry job) | Kept the project simple. I'd add a worker for the booking email and a Beat job to mark expired holds. |
| Race test script | A script that fires 20 parallel holds at one seat and checks that exactly 1 succeeds. |
| Automated tests (pytest) | Tests for 403 by role, the idempotent replay, and the hold conflict. |
| Pagination | `/bookings/me/` and the event list return everything; I'd add DRF pagination. |
| HTTPS | Nginx would terminate TLS (Let's Encrypt). The settings already support `SECURE_PROXY_SSL_HEADER`. |
| Production hardening | `DEBUG=False`, don't publish the DB port, use a secrets manager. |
