from rest_framework.routers import DefaultRouter
from .booking_viewset import BookingViewSet
from .seat_viewset import SeatViewSet
from .views import EventViewSet

router = DefaultRouter()
router.register("events", EventViewSet, basename="events")
# Seats live under an event: /events/{event_id}/seats/{seat_id}/hold/
router.register(r"events/(?P<event_id>\d+)/seats", SeatViewSet, basename="event-seats")
router.register("bookings", BookingViewSet, basename="bookings")

urlpatterns = router.urls
