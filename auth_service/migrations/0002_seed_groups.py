from django.contrib.auth.management import create_permissions
from django.db import migrations

# Must match permissions.ADMIN_GROUP / USERS_GROUP; hardcoded because migrations
# must not import app code that can change later.
# Codenames are Django's auto-created model permissions for the "events" app.
GROUP_PERMISSIONS = {
    "admin": [
        "view_event", "add_event", "change_event", "delete_event",
        "view_seat",
        "view_booking",
    ],
    "users": [
        "view_event",
        "view_seat",
    ],
}


def seed_groups(apps, schema_editor):
    # Django normally creates model permissions (add_event, ...) in a post_migrate
    # signal, i.e. AFTER all migrations. Create them now so we can assign them.
    for app_config in apps.get_app_configs():
        app_config.models_module = True
        create_permissions(app_config, apps=apps, verbosity=0, using=schema_editor.connection.alias)
        app_config.models_module = None

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    for group_name, codenames in GROUP_PERMISSIONS.items():
        group, _ = Group.objects.get_or_create(name=group_name)
        perms = Permission.objects.filter(content_type__app_label="events", codename__in=codenames)
        # Fail loudly on a typo instead of silently granting less
        if perms.count() != len(codenames):
            raise RuntimeError(f"Missing permissions for group {group_name!r}: {codenames}")
        group.permissions.set(perms)


def delete_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=GROUP_PERMISSIONS.keys()).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("auth_service", "0001_initial"),
        # The events models must exist before their permissions can be created
        ("events", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_groups, delete_groups),
    ]
