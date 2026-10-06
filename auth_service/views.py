from django.contrib.auth import authenticate
from django.contrib.auth.models import update_last_login
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User
from .permissions import IsAdminRole
from .serializers import (
    ChangeRoleSerializer,
    LoginSerializer,
    RefreshTokenSerializer,
    RegisterSerializer,
    UserSerializer,
)


class AuthViewSet(viewsets.GenericViewSet):
    """
    POST /auth/register/  -> create account
    POST /auth/login/     -> get access + refresh tokens
    POST /auth/refresh/   -> new access + refresh; old refresh is blacklisted
    POST /auth/logout/    -> blacklist the refresh token
    GET  /auth/me/        -> logged-in user's profile + role
    """

    permission_classes = [AllowAny]
    # Skip JWT auth here so an expired token in the header can't block login/refresh
    authentication_classes = []
    # Only actions that set throttle_scope below are rate limited (None = no limit)
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = None

    @action(detail=False, methods=["post"], serializer_class=RegisterSerializer, throttle_scope="auth_register")
    def register(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], serializer_class=LoginSerializer, throttle_scope="auth_login")
    def login(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = authenticate(
            request,
            email=serializer.validated_data["email"].lower(),
            password=serializer.validated_data["password"],
        )
        # None for wrong email, wrong password, or inactive user: same message for all
        if user is None:
            return Response({"detail": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED)

        refresh = RefreshToken.for_user(user)  # also saved in the OutstandingToken table
        update_last_login(None, user)
        return Response({
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": UserSerializer(user).data,
        })

    @action(detail=False, methods=["post"], serializer_class=RefreshTokenSerializer)
    def refresh(self, request):
        # simplejwt's serializer: rejects blacklisted tokens, and because of
        # ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION it blacklists the old one
        serializer = TokenRefreshSerializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError:
            return Response({"detail": "Invalid or expired refresh token."}, status=status.HTTP_401_UNAUTHORIZED)
        return Response(serializer.validated_data)

    @action(detail=False, methods=["post"], serializer_class=RefreshTokenSerializer)
    def logout(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            RefreshToken(serializer.validated_data["refresh"]).blacklist()
        except TokenError:
            return Response({"detail": "Invalid or expired refresh token."}, status=status.HTTP_401_UNAUTHORIZED)
        return Response(status=status.HTTP_205_RESET_CONTENT)

    @action(
        detail=False,
        methods=["get"],
        serializer_class=UserSerializer,
        authentication_classes=[JWTAuthentication],
        permission_classes=[IsAuthenticated],
    )
    def me(self, request):
        return Response(self.get_serializer(request.user).data)


class UserViewSet(viewsets.GenericViewSet):
    """
    GET   /auth/users/            -> all users with their role (admin only)
    PATCH /auth/users/{id}/role/  -> change a user's role     (admin only)
    """

    queryset = User.objects.order_by("id")
    serializer_class = UserSerializer
    permission_classes = [IsAdminRole]

    def list(self, request):
        return Response(self.get_serializer(self.get_queryset(), many=True).data)

    @action(detail=True, methods=["patch"], serializer_class=ChangeRoleSerializer)
    def role(self, request, pk=None):
        user = self.get_object()
        # Stops an admin from demoting themselves and leaving no admin behind
        if user == request.user:
            return Response({"detail": "You can't change your own role."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # One group per user: replace, don't add
        user.groups.set([serializer.validated_data["role"]])
        return Response(UserSerializer(user).data)
