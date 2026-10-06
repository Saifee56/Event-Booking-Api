from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User

# Groups (roles) are managed in Django's built-in "Authentication and Authorization > Groups" admin


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    # Django's UserAdmin is built around "username"; these override it for email login
    ordering = ["email"]
    list_display = ["email", "first_name", "last_name", "is_staff", "is_active"]
    list_filter = ["groups", "is_staff", "is_superuser", "is_active"]
    search_fields = ["email", "first_name", "last_name"]
    filter_horizontal = ["groups"]

    fieldsets = [
        (None, {"fields": ["email", "password"]}),
        ("Personal info", {"fields": ["first_name", "last_name"]}),
        ("Role", {"fields": ["groups"]}),
        ("Status", {"fields": ["is_active", "is_staff", "is_superuser"]}),
        ("Important dates", {"fields": ["last_login", "date_joined"]}),
    ]
    add_fieldsets = [
        (None, {
            "classes": ["wide"],
            "fields": ["email", "groups", "password1", "password2"],
        }),
    ]
