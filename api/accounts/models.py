import typing
from django.db import models
from django.contrib.auth.base_user import AbstractBaseUser
from django.utils.translation import gettext_lazy as _
from django.contrib.auth import models as auth_models
from django.contrib.auth.models import UserManager as BaseUserManager, PermissionsMixin
import qsessions.models


class Session(qsessions.models.Session):
    class Meta(qsessions.models.Session.Meta):
        app_label = 'accounts'
        proxy = True


class Group(auth_models.Group):
    class Meta:
        verbose_name = _("group")
        verbose_name_plural = _("groups")
        app_label = 'accounts'
        proxy = True


class UserQueryset(models.QuerySet):
    def active(self) -> typing.Self:
        return self.filter(is_active=True)


class UserManager(BaseUserManager.from_queryset(UserQueryset)):
    def _create_user(self, email, password, **extra_fields):
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_superuser', True)
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')
        return self._create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(_('email address'), unique=True, db_index=True)
    is_active = models.BooleanField(
        _('active'),
        default=True,
        help_text=_('Designates whether this user should be treated as active. Unselect this instead of deleting accounts.'),
    )
    display_name = models.CharField(_('display name'), max_length=256)
    created_on = models.DateTimeField(_('date joined'), auto_now_add=True, editable=False)
    updated_on = models.DateTimeField(auto_now=True, editable=False)

    objects: UserManager = UserManager()

    EMAIL_FIELD = 'email'
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['display_name']

    class Meta:
        app_label = 'accounts'
        default_manager_name = 'objects'
        ordering = ['-id']
        verbose_name = _('user')
        verbose_name_plural = _('users')

    def __str__(self):
        return str(self.email)

    @property
    def slug(self) -> str:
        return self.pk

    @property
    def is_staff(self):
        return self.is_superuser

    @property
    def can_login(self) -> bool:
        return self.is_active

    def clean(self):
        super().clean()
        self.email = self.__class__.objects.normalize_email(self.email).lower()
