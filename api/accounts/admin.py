from django.utils.translation import gettext_lazy as _
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth import models as auth_models
from django.contrib import admin
import qsessions.models
import qsessions.admin
from . import models

# Unregister default admin
admin.site.unregister(qsessions.models.Session)
admin.site.unregister(auth_models.Group)


@admin.register(models.Session)
class SessionAdmin(qsessions.admin.SessionAdmin):
    pass


@admin.register(models.Group)
class GroupAdmin(admin.ModelAdmin):
    search_fields = ('name',)
    ordering = ('name',)
    filter_horizontal = ('permissions',)
    fieldsets = ((None, {'fields': ['name', 'permissions']}),)

    def formfield_for_manytomany(self, db_field, request=None, **kwargs):
        if db_field.name == 'permissions':
            qs = kwargs.get('queryset', db_field.remote_field.model.objects)
            kwargs['queryset'] = qs.select_related('content_type')
        return super().formfield_for_manytomany(db_field, request=request, **kwargs)


@admin.register(models.User)
class UserAdmin(BaseUserAdmin):
    readonly_fields = ['created_on', 'updated_on', 'last_login']
    fieldsets = [
        (None, {'fields': ('email', 'password')}),
        (_('Personal info'), {'fields': ('display_name',)}),
        (_('Permissions'), {'fields': ('is_active', 'is_superuser', 'groups', 'user_permissions')}),
        (_('Important dates'), {'fields': ('last_login', 'updated_on', 'created_on')}),
    ]
    filter_horizontal = 'groups', 'user_permissions',
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email', 'display_name', 'password1', 'password2'),
        }),
    )
    list_display = ('email', 'is_superuser', 'created_on', 'is_active')
    list_filter = ('is_superuser', 'is_active')
    search_fields = ('display_name', 'email')
    ordering = ['-id']
