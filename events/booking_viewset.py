from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from auth_service.permissions import IsUserRole, is_admin

from .models import Booking, Seat
from .seat_viewset import hold_key
from .serializers import BookingSerializer
from .views import seats_key

IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_MAX_LENGTH = 64  # same as Booking.idempotency_key max_length


class BookingViewSet(viewsets.GenericViewSet):
    """
    POST /bookings/{id}/confirm/  -> confirm your own held booking (users group, needs Idempotency-Key header)
    GET  /bookings/me/            -> your bookings; admins see everyone's
    """

    serializer_class = BookingSerializer
    permission_classes = [IsAuthenticated]
    lookup_value_regex = r"\d+"  # /bookings/abc/confirm/ -> 404 instead of a crash on int(pk)

    def get_queryset(self):
        # select_related: booking + user + seat + event in one query (the serializer reads all of them)
        return Booking.objects.select_related("user", "seat__event")

    @action(detail=False, methods=["get"])
    def me(self, request):
        bookings = self.get_queryset()
        # A list has no get_object(), so ownership is enforced by filtering here
        if not is_admin(request.user):
            bookings = bookings.filter(user=request.user)
        return Response(self.get_serializer(bookings, many=True).data)

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated, IsUserRole])
    def confirm(self, request, pk=None):
        key = request.headers.get(IDEMPOTENCY_HEADER, "").strip()
        if not key or len(key) > IDEMPOTENCY_KEY_MAX_LENGTH:
            return Response(
                {"detail": f"{IDEMPOTENCY_HEADER} header is required (max {IDEMPOTENCY_KEY_MAX_LENGTH} chars)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Replay: this key was already used. Same booking -> return the same answer, nothing is changed.
        used = self.get_queryset().filter(idempotency_key=key).first()
        if used is not None:
            if used.pk == int(pk) and used.user_id == request.user.id:
                return Response(self.get_serializer(used).data, status=status.HTTP_200_OK)
            return Response(
                {"detail": f"{IDEMPOTENCY_HEADER} was already used for another booking."},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            with transaction.atomic():
                # select_for_update(): row lock on the booking AND its seat (both tables are in the join).
                # A second confirm for the same booking waits here until the first one commits,
                # then reads the updated row. Filtering by user: someone else's booking is a 404.
                booking = (
                    self.get_queryset()
                    .select_for_update()
                    .filter(pk=pk, user=request.user)
                    .first()
                )
                if booking is None:
                    return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

                if booking.status == Booking.Status.CONFIRMED:
                    # Two requests with the same key at the same time: the loser lands here after the lock
                    if booking.idempotency_key == key:
                        return Response(self.get_serializer(booking).data, status=status.HTTP_200_OK)
                    return Response(
                        {"detail": "Booking is already confirmed."},
                        status=status.HTTP_409_CONFLICT,
                    )

                if booking.status == Booking.Status.EXPIRED or booking.expires_at <= timezone.now():
                    # Mark it expired (if it isn't yet), so the seat can be held again
                    if booking.status != Booking.Status.EXPIRED:
                        booking.status = Booking.Status.EXPIRED
                        booking.save(update_fields=["status"])
                    return Response(
                        {"detail": "Hold has expired. Hold the seat again."},
                        status=status.HTTP_409_CONFLICT,
                    )

                booking.status = Booking.Status.CONFIRMED
                booking.confirmed_at = timezone.now()
                booking.idempotency_key = key
                booking.save(update_fields=["status", "confirmed_at", "idempotency_key"])

                seat = booking.seat
                seat.status = Seat.Status.BOOKED
                seat.save(update_fields=["status"])
        except IntegrityError:
            # unique idempotency_key: another booking took this key at the same moment (rolled back)
            return Response(
                {"detail": f"{IDEMPOTENCY_HEADER} was already used for another booking."},
                status=status.HTTP_409_CONFLICT,
            )

        # After commit: the seat is now booked (in Postgres), so the hold lock and cached seat list are stale
        cache.delete_many([hold_key(seat.id), seats_key(seat.event_id)])

        return Response(self.get_serializer(booking).data, status=status.HTTP_200_OK)
