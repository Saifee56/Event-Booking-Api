from django.db import models
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db.models.functions import Lower
# Create your models here.
# Roles = Django's built-in Groups ("admin", "users"), seeded in migration 0002.
# AbstractUser already gives User: groups, user_permissions, has_perm().


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if not extra_fields["is_staff"] or not extra_fields["is_superuser"]:
            raise ValueError("Superuser must have is_staff=True and is_superuser=True")
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    username=None
    email=models.EmailField(unique=True)

    USERNAME_FIELD="email"
    REQUIRED_FIELDS=[]

    objects=UserManager()

    class Meta:
        db_table="users"
        constraints=[
            models.UniqueConstraint(Lower("email"),name="unique_user_email_ci")
        ]
