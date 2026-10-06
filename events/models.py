from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


def default_expires_at():
    return timezone.now() + timedelta(minutes=10)


class Event(models.Model):
    name = models.CharField(max_length=100)
    venue = models.CharField(max_length=100)
    starts_at = models.DateTimeField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="events_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # A venue can't host two events at the same time (also blocks double submits)
            models.UniqueConstraint(
                fields=["venue", "starts_at"],
                name="unique_event_per_venue_time",
            ),
        ]
        indexes = [
            models.Index(fields=["starts_at"], name="event_starts_at_idx"),
        ]
        ordering = ["starts_at"]

    def __str__(self):
        return f"{self.name} @ {self.venue}"


class Seat(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = "available", "Available"
        BOOKED = "booked", "Booked"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="seats")
    seat_number = models.CharField(max_length=10)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.AVAILABLE,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "seat_number"],
                name="unique_seat_per_event",
            ),
        ]
        indexes = [
            models.Index(fields=["event", "status"], name="seat_event_status_idx"),
        ]
        ordering = ["seat_number"]

    def __str__(self):
        return f"{self.seat_number} ({self.status})"


class Booking(models.Model):
    class Status(models.TextChoices):
        HELD = "held", "Held"
        CONFIRMED = "confirmed", "Confirmed"
        EXPIRED = "expired", "Expired"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="bookings",
    )
    seat = models.ForeignKey(
        Seat,
        on_delete=models.PROTECT,
        related_name="bookings",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.HELD,
    )
    idempotency_key = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
    )
    expires_at = models.DateTimeField(default=default_expires_at)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # One active (held/confirmed) booking per seat; expired rows are kept as history
            models.UniqueConstraint(
                fields=["seat"],
                condition=Q(status__in=["held", "confirmed"]),
                name="unique_active_booking_per_seat",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "expires_at"], name="booking_status_expires_idx"),
            models.Index(fields=["user", "created_at"], name="booking_user_created_idx"),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"Booking {self.pk}: seat {self.seat_id} ({self.status})"
