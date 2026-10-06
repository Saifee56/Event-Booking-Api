from datetime import timedelta

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from auth_service.permissions import IsUserRole

from .models import Booking, Seat
from .views import seats_key


HOLD_TTL = 60 * 10  # 10 minutes; the Redis lock and Booking.expires_at both use this


def hold_key(seat_id):
    # Stored in Redis as "ticket:1:seat:{id}:hold" because of KEY_PREFIX
    return f"seat:{seat_id}:hold"


class SeatViewSet(viewsets.GenericViewSet):
    """
    POST /events/{event_id}/seats/{seat_id}/hold/  -> hold a seat for HOLD_TTL seconds (users group only)
    """

    permission_classes = [IsAuthenticated, IsUserRole]
    # Name the id in the URL "seat_id" instead of "pk", and only accept numbers
    lookup_url_kwarg = "seat_id"
    lookup_value_regex = r"\d+"

    def get_queryset(self):
        # Only seats of the event in the URL. A seat from another event is
        # simply "not found", so get_object() returns 404 on a mismatch.
        # select_related: seat + event in one query
        return Seat.objects.select_related("event").filter(
            event_id=self.kwargs["event_id"]
        )

    @action(detail=True, methods=["post"])
    def hold(self, request, event_id=None, seat_id=None):
        seat = self.get_object()  # 404 if the event or seat doesn't exist, or they don't match

        # Already sold: no point in taking a lock
        if seat.status == Seat.Status.BOOKED:
            return Response(
                {"detail": "Seat is already booked."},
                status=status.HTTP_409_CONFLICT,
            )

        # The lock: SET seat:{id}:hold {user_id} NX EX HOLD_TTL
        # Atomic in Redis, so only ONE request can get True
        key = hold_key(seat.id)
        locked = cache.add(key, request.user.id, HOLD_TTL)
        if not locked:
            return Response(
                {"detail": "Seat is already held."},
                status=status.HTTP_409_CONFLICT,
            )

        # Same expiry as the Redis key, so Redis and Postgres agree on when the hold ends
        expires_at = timezone.now() + timedelta(seconds=HOLD_TTL)

        try:
            with transaction.atomic():
                # No Celery: free this seat's old holds here, so the unique constraint allows a new one
                Booking.objects.filter(
                    seat=seat, status=Booking.Status.HELD, expires_at__lte=timezone.now()
                ).update(status=Booking.Status.EXPIRED)
                booking = Booking.objects.create(
                    user=request.user,
                    seat=seat,
                    status=Booking.Status.HELD,
                    expires_at=expires_at,
                )
        except IntegrityError:
            # unique_active_booking_per_seat said no (e.g. Redis lost the key,
            # but the DB still has an active hold). Give the lock back.
            cache.delete(key)
            return Response(
                {"detail": "Seat is already held."},
                status=status.HTTP_409_CONFLICT,
            )

        # Seat status changed, so the cached seat list for this event is stale
        cache.delete(seats_key(seat.event_id))

        return Response(
            {
                "booking_id": booking.id,
                "event_id": seat.event_id,
                "event_name": seat.event.name,
                "seat_id": seat.id,
                "seat_number": seat.seat_number,
                "status": booking.status,
                "expires_at": booking.expires_at,
            },
            status=status.HTTP_201_CREATED,
        )