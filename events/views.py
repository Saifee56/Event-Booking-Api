from django.core.cache import cache
from django.db.models import Exists, OuterRef, ProtectedError
from django.db.models.functions import Length
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from auth_service.permissions import ModelPermissions

from .models import Booking, Event
from .serializers import EventSerializer, SeatSerializer

# Redis key for the cached event list (stored as "ticket:1:events:list" because of KEY_PREFIX)
EVENT_LIST_KEY = "events:list"
EVENT_LIST_TTL = 60 * 5  # 5 minutes

SEATS_TTL = 30  # seconds; seat status changes often, so keep it short


def seats_key(event_id):
    # One key per event, so a hold on event 3 only clears event 3's seats
    return f"events:{event_id}:seats"


class EventViewSet(viewsets.GenericViewSet):
    """
    GET    /events/list_events/          -> list events           (view_event)
    POST   /events/create_event/         -> create event + seats  (add_event)
    GET    /events/{id}/get_event/       -> one event             (view_event)
    PATCH  /events/{id}/update_event/    -> update event          (change_event)
    DELETE /events/{id}/delete_event/    -> delete event          (delete_event)
    GET    /events/{id}/seats/           -> seats with status     (view_event)

    ModelPermissions checks by HTTP method, so each action must use the matching method.
    """

    # ModelPermissions reads the model from this queryset to build "events.view_event" etc.
    queryset = Event.objects.all()
    serializer_class = EventSerializer
    permission_classes = [ModelPermissions]

    @action(detail=False, methods=["get"])
    def list_events(self, request):
        # Cache-aside: try Redis first, only hit Postgres on a miss
        data = cache.get(EVENT_LIST_KEY)
        if data is None:
            serializer = self.get_serializer(self.get_queryset(), many=True)
            data = serializer.data
            cache.set(EVENT_LIST_KEY, data, EVENT_LIST_TTL)
        return Response(data)

    @action(detail=False, methods=["post"])
    def create_event(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # created_by is read-only in the serializer, so it's set here from the token
        serializer.save(created_by=request.user)
        # The list changed, so drop the cached copy (the next list_events rebuilds it)
        cache.delete(EVENT_LIST_KEY)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def get_event(self, request, pk=None):
        # get_object(): 404 if the id doesn't exist
        event = self.get_object()
        return Response(self.get_serializer(event).data)

    @action(detail=True, methods=["patch"])
    def update_event(self, request, pk=None):
        event = self.get_object()
        # partial=True: only the fields sent are validated and changed
        serializer = self.get_serializer(event, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        cache.delete(EVENT_LIST_KEY)
        return Response(serializer.data)

    @action(detail=True, methods=["delete"])
    def delete_event(self, request, pk=None):
        event = self.get_object()
        try:
            # Seats are deleted with the event (CASCADE), but bookings protect their seat (PROTECT)
            event.delete()
        except ProtectedError:
            return Response(
                {"detail": "Event has bookings and can't be deleted."},
                status=status.HTTP_409_CONFLICT,
            )
        # Only after a successful delete (the 409 path above changes nothing)
        cache.delete_many([EVENT_LIST_KEY, seats_key(pk)])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get"])
    def seats(self, request, pk=None):
        # Cache-aside: on a hit, Postgres isn't touched at all
        key = seats_key(pk)
        data = cache.get(key)
        if data is None:
            event = self.get_object()  # 404 if the event doesn't exist
            # True if the seat has a hold that hasn't expired yet (one SQL query, not one per seat)
            active_hold = Booking.objects.filter(
                seat=OuterRef("pk"),
                status=Booking.Status.HELD,
                expires_at__gt=timezone.now(),
            )
            # seat_number is text, so sort by length first: 1, 2, ..., 10 (not 1, 10, 2)
            seats = event.seats.annotate(is_held=Exists(active_hold)).order_by(
                Length("seat_number"), "seat_number"
            )
            data = SeatSerializer(seats, many=True).data
            cache.set(key, data, SEATS_TTL)
        return Response(data)
