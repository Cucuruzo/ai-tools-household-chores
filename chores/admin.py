from django.contrib import admin

from .models import Chore, Member, RotationState


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = ["order", "avatar", "name", "role"]
    list_display_links = ["name"]


@admin.register(Chore)
class ChoreAdmin(admin.ModelAdmin):
    list_display = ["title", "start_date", "assignment_mode", "recurrence", "points", "is_template"]
    list_filter = ["assignment_mode", "recurrence", "is_template"]


@admin.register(RotationState)
class RotationStateAdmin(admin.ModelAdmin):
    list_display = ["chore", "last_member"]
