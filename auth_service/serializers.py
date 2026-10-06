from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers

from .models import User
from .permissions import ADMIN_GROUP, USERS_GROUP


class UserSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "email", "first_name", "last_name", "role"]

    def get_role(self, obj):
        # Superusers may not be in any group but can do everything
        if obj.is_superuser:
            return ADMIN_GROUP
        group = obj.groups.first()
        return group.name if group else None


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)
    confirm_password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        # Only these fields are accepted: role / is_staff / is_superuser can't be sent by the client
        fields = ["email", "first_name", "last_name", "password", "confirm_password"]
        # Our own validate_email below does a case-insensitive check instead of DRF's default one
        extra_kwargs = {"email": {"validators": []}}

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def validate_password(self, value):
        validate_password(value)  # Django's AUTH_PASSWORD_VALIDATORS
        return value

    def validate(self, attrs):
        if attrs["password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": "Passwords do not match."})
        return attrs

    def create(self, validated_data):
        validated_data.pop("confirm_password")
        # .get() not get_or_create(): a missing group means the seed migration didn't run,
        # and silently creating an empty group would leave the user with no permissions
        group = Group.objects.get(name=USERS_GROUP)
        with transaction.atomic():  # no user is saved without its group
            user = User.objects.create_user(**validated_data)
            user.groups.add(group)
        return user


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class RefreshTokenSerializer(serializers.Serializer):
    refresh = serializers.CharField()


class ChangeRoleSerializer(serializers.Serializer):
    # {"role": "admin"} -> the Group named "admin"; unknown name -> 400
    role = serializers.SlugRelatedField(slug_field="name", queryset=Group.objects.all())
