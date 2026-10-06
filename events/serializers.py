from django.db import transaction
from rest_framework import serializers

from .models import Booking, Event, Seat


class EventSerializer(serializers.ModelSerializer):
    # Only used on create: how many seats to generate for the event
    seat_count = serializers.IntegerField(
        write_only=True, required=False, min_value=1, max_value=1000
    )

    class Meta:
        model = Event
        fields = [
            "id",
            "name",
            "venue",
            "starts_at",
            "seat_count",
            "created_by",
            "created_at",
            "updated_at",
        ]
        # created_by is set by the view (perform_create), not by the client
        read_only_fields = ["created_by", "created_at", "updated_at"]

    def validate(self, attrs):
        creating = self.instance is None
        if creating and "seat_count" not in attrs:
            raise serializers.ValidationError({"seat_count": "This field is required."})
        if not creating and "seat_count" in attrs:
            raise serializers.ValidationError(
                {"seat_count": "Seats can't be changed after the event is created."}
            )
        return attrs

    def create(self, validated_data):
        seat_count = validated_data.pop("seat_count")
        # Event and its seats are saved together, or not at all
        with transaction.atomic():
            event = Event.objects.create(**validated_data)
            Seat.objects.bulk_create(
                Seat(event=event, seat_number=str(n)) for n in range(1, seat_count + 1)
            )
        return event


class SeatSerializer(serializers.ModelSerializer):
    # "held" isn't stored on the seat; it comes from an active Booking (is_held is annotated by the view)
    status = serializers.SerializerMethodField()

    class Meta:
        model = Seat
        fields = ["id", "seat_number", "status"]

    def get_status(self, seat):
        if seat.status == Seat.Status.BOOKED:
            return Seat.Status.BOOKED
        if seat.is_held:
            return "held"
        return Seat.Status.AVAILABLE


class BookingSerializer(serializers.ModelSerializer):
    # Flattened, so the client doesn't need extra calls to know which seat/event this is
    user_email = serializers.EmailField(source="user.email", read_only=True)
    event_id = serializers.IntegerField(source="seat.event_id", read_only=True)
    event_name = serializers.CharField(source="seat.event.name", read_only=True)
    seat_number = serializers.CharField(source="seat.seat_number", read_only=True)

    class Meta:
        model = Booking
        fields = [
            "id",
            "user_email",
            "event_id",
            "event_name",
            "seat",
            "seat_number",
            "status",
            "expires_at",
            "created_at",
            "confirmed_at",
        ]
        # Bookings are created by hold and changed by confirm, never written directly
        read_only_fields = fields
    def to_representation(self, booking):
        data = super().to_representation(booking)
        # expires_at only matters while the seat is held
        if booking.status != Booking.Status.HELD:
            data.pop("expires_at")
        return data