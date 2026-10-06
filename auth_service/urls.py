from rest_framework.routers import DefaultRouter

from .views import AuthViewSet, UserViewSet

router = DefaultRouter()
router.register("auth/users", UserViewSet, basename="users")
router.register("auth", AuthViewSet, basename="auth")

urlpatterns = router.urls
