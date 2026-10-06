from rest_framework.permissions import DjangoModelPermissions, BasePermission

# Must match the group names created in the seed migration
ADMIN_GROUP = "admin"
USERS_GROUP = "users"


def in_group(user, group_name):
    return user.is_authenticated and user.groups.filter(name=group_name).exists()


def is_admin(user):
    # Superusers pass every check, same as Django's own user.has_perm()
    return user.is_authenticated and (user.is_superuser or in_group(user, ADMIN_GROUP))


class ModelPermissions(DjangoModelPermissions):
    """
    HTTP method -> Django's auto-created model permission.
    DRF's default map lets any logged-in user GET; here GET also needs "view".

        GET/HEAD/OPTIONS -> events.view_event
        POST             -> events.add_event
        PUT/PATCH        -> events.change_event
        DELETE           -> events.delete_event

    The model comes from the view's queryset, so the same class works for any model.
    Anonymous -> 401, logged in without the permission -> 403.
    """

    perms_map = {
        "GET": ["%(app_label)s.view_%(model_name)s"],
        "OPTIONS": ["%(app_label)s.view_%(model_name)s"],
        "HEAD": ["%(app_label)s.view_%(model_name)s"],
        "POST": ["%(app_label)s.add_%(model_name)s"],
        "PUT": ["%(app_label)s.change_%(model_name)s"],
        "PATCH": ["%(app_label)s.change_%(model_name)s"],
        "DELETE": ["%(app_label)s.delete_%(model_name)s"],
    }


class IsAdminRole(BasePermission):
    """Admin group or superuser only (list users, change roles)."""

    def has_permission(self, request, view):
        return is_admin(request.user)


class IsUserRole(BasePermission):
    """Users group only (hold seat, confirm booking). Admins don't book."""

    def has_permission(self, request, view):
        return in_group(request.user, USERS_GROUP)


class IsOwnerOrAdmin(BasePermission):
    """
    Object-level: a user can only access their own object (e.g. a booking).
    Only runs on detail views / get_object(); list views must filter the queryset themselves.
    """

    def has_permission(self, request, view):
        return request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        return obj.user_id == request.user.id or is_admin(request.user)
