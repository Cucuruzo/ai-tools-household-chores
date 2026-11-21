from django.db import models
from django.db.models import Max

AVATAR_CHOICES = [
    (emoji, emoji)
    for emoji in [
        "😀", "😎", "🤓", "🥳", "👩", "👨", "👧", "👦",
        "👵", "👴", "🐱", "🐶", "🦊", "🐼", "🐸", "🦄",
    ]
]


class Member(models.Model):
    class Role(models.TextChoices):
        ADULT = "adult", "Adult"
        KID = "kid", "Kid"

    name = models.CharField(max_length=50)
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.ADULT)
    avatar = models.CharField(max_length=8, choices=AVATAR_CHOICES)
    order = models.PositiveIntegerField(blank=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.avatar} {self.name}"

    def save(self, *args, **kwargs):
        if self.order is None:
            current_max = Member.objects.aggregate(m=Max("order"))["m"]
            self.order = 0 if current_max is None else current_max + 1
        super().save(*args, **kwargs)


class Chore(models.Model):
    class AssignmentMode(models.TextChoices):
        FIXED = "fixed", "Fixed"
        ROTATING = "rotating", "Rotating"
        UNASSIGNED = "unassigned", "Unassigned"

    class Eligibility(models.TextChoices):
        KIDS = "kids", "Kids only"
        ADULTS = "adults", "Adults only"
        EVERYONE = "everyone", "Everyone"

    class Recurrence(models.TextChoices):
        ONCE = "once", "Once"
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"

    POINTS_CHOICES = [(5, "5"), (10, "10"), (20, "20")]

    title = models.CharField(max_length=100)
    instructions = models.TextField(blank=True)
    start_date = models.DateField()
    due_time = models.TimeField(null=True, blank=True)
    assignment_mode = models.CharField(
        max_length=10, choices=AssignmentMode.choices, default=AssignmentMode.UNASSIGNED
    )
    eligibility = models.CharField(
        max_length=10, choices=Eligibility.choices, null=True, blank=True
    )
    assigned_to = models.ForeignKey(
        Member, null=True, blank=True, on_delete=models.SET_NULL, related_name="fixed_chores"
    )
    recurrence = models.CharField(
        max_length=10, choices=Recurrence.choices, default=Recurrence.ONCE
    )
    # Day of week (0=Mon..6=Sun) for weekly, day of month (1..31) for monthly.
    recurrence_day = models.IntegerField(null=True, blank=True)
    points = models.IntegerField(choices=POINTS_CHOICES, default=10)
    is_template = models.BooleanField(default=False)
    # Built-in templates ship with the app (data migration) and stay read-only (#23).
    is_builtin = models.BooleanField(default=False)
    template_source = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="derived_chores"
    )
    # Last date the chore may be projected on, set by "delete this and future" (#5).
    end_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return self.title


class ChoreOccurrence(models.Model):
    """A stored (materialized) instance of a chore on one date (#9)."""

    class Status(models.TextChoices):
        TODO = "todo", "To do"
        DONE = "done", "Done"
        SKIPPED = "skipped", "Skipped"

    chore = models.ForeignKey(Chore, on_delete=models.CASCADE, related_name="occurrences")
    # Snapshot of the chore's title / instructions when the row was stored (#39).
    title = models.CharField(max_length=100)
    instructions = models.TextField(blank=True)
    member = models.ForeignKey(
        Member, null=True, blank=True, on_delete=models.SET_NULL, related_name="occurrences"
    )
    date = models.DateField()
    due_time = models.TimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.TODO)
    done_at = models.DateTimeField(null=True, blank=True)
    is_override = models.BooleanField(default=False)
    points = models.IntegerField()
    # "Delete this occurrence only" (#5): the row stays so it is never recreated.
    is_cancelled = models.BooleanField(default=False)
    # Who is credited with the points (#12); ``member`` stays the assignee.
    completed_by = models.ForeignKey(
        Member, null=True, blank=True, on_delete=models.SET_NULL, related_name="completed_occurrences"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["chore", "date"], name="unique_occurrence_per_chore_date"),
        ]

    def __str__(self):
        return f"{self.chore} on {self.date}"

    def save(self, *args, **kwargs):
        # Safety net for rows made outside create_occurrence (admin, test helpers).
        if self._state.adding and not self.title:
            self.title = self.chore.title
            self.instructions = self.chore.instructions
        super().save(*args, **kwargs)


class RotationState(models.Model):
    """Who had the last turn of a rotating chore. Written only by
    services.advance_rotation (one turn per stored occurrence, #9)."""

    chore = models.OneToOneField(Chore, on_delete=models.CASCADE, related_name="rotation_state")
    last_member = models.ForeignKey(
        Member, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    def __str__(self):
        return f"{self.chore} → {self.last_member or '—'}"


class OccurrenceException(models.Model):
    """A per-date change to a projected (not yet stored) occurrence (#31).

    Applied on read by projection and consumed by ``materialize_week``. A
    stored row on the same date always wins; an exception on a date that is
    not on the chore's rule is ignored. Either kind still takes its rotation
    turn (rotation rule 7). An "extra" (``moved_from`` set, #48) is a date
    moved into a later week: an override with its own title, instructions,
    due time and points that takes no rotation turn. A date with
    ``own_values`` set (#38) shows its own values on a rule date and keeps
    its turn.
    """

    chore = models.ForeignKey(Chore, on_delete=models.CASCADE, related_name="exceptions")
    date = models.DateField()
    is_cancelled = models.BooleanField(default=False)
    # ``member`` replaces the projected member (None: Unassigned).
    is_override = models.BooleanField(default=False)
    member = models.ForeignKey(
        Member, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Set only on an extra (#48): the date the occurrence was first moved from.
    moved_from = models.DateField(null=True, blank=True)
    # A date that was not moved but carries its own values (#38): "edited" by
    # "This occurrence only" on a Planned date (also an override), or "kept"
    # by "this and future" from a later date (the values it showed before).
    own_values = models.CharField(
        max_length=6, blank=True, default="", choices=[("edited", "Edited"), ("kept", "Kept")]
    )
    # Read on an extra or a date with ``own_values``: its own values, a
    # snapshot like a stored row's (#39).
    title = models.CharField(max_length=100, blank=True, default="")
    instructions = models.TextField(blank=True, default="")
    due_time = models.TimeField(null=True, blank=True)
    points = models.IntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["chore", "date"], name="unique_exception_per_chore_date"),
        ]

    def __str__(self):
        return f"{self.chore} on {self.date} (exception)"
