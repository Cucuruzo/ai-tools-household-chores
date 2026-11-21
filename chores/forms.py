from django import forms
from django.utils import timezone

from .models import Chore, Member
from .profile import is_valid_pk

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAY_CHOICES = list(enumerate(WEEKDAYS))
MONTHDAY_CHOICES = [(d, str(d)) for d in range(1, 32)]


def parse_pk(raw):
    """Return an int primary key for a string of ASCII digits in range, else None."""
    if isinstance(raw, str) and raw.isascii() and raw.isdigit():
        pk = int(raw)
        if is_valid_pk(pk):
            return pk
    return None


def find_template(raw):
    """The template Chore for a raw id (query/POST value), or None if invalid."""
    pk = parse_pk(raw)
    if pk is None:
        return None
    return Chore.objects.filter(pk=pk, is_template=True).first()


# Values copied from a template into a new chore form, and from a saved chore
# into a new household template. start_date is not pre-filled from templates.
TEMPLATE_FIELDS = [
    "title", "instructions", "due_time", "assignment_mode", "eligibility",
    "assigned_to", "recurrence", "recurrence_day", "points",
]


class MemberForm(forms.ModelForm):
    class Meta:
        model = Member
        fields = ["name", "role", "avatar"]


class MemberChoiceField(forms.ModelChoiceField):
    """Rejects non-digit and out-of-range ids before they reach the DB.

    On SQLite, ids beyond signed 64-bit raise OverflowError inside the query.
    """

    def label_from_instance(self, member):
        return f"{member.avatar} {member.name}"

    def to_python(self, value):
        if value in self.empty_values:
            return None
        pk = parse_pk(str(value))
        if pk is None:
            raise forms.ValidationError(
                self.error_messages["invalid_choice"], code="invalid_choice"
            )
        return super().to_python(pk)


class RecurrenceDayWidget(forms.Widget):
    """The template renders two selects named ``recurrence_day`` (weekday and
    day of month). With JS the inactive one is disabled, so one value arrives.
    Without JS both arrive in document order; pick the one matching recurrence.
    """

    def value_from_datadict(self, data, files, name):
        values = data.getlist(name) if hasattr(data, "getlist") else [data.get(name)]
        values = [v for v in values if v is not None]
        if len(values) <= 1:
            return values[0] if values else None
        recurrence = data.get("recurrence")
        if recurrence == Chore.Recurrence.WEEKLY:
            return values[0]
        if recurrence == Chore.Recurrence.MONTHLY:
            return values[1]
        return None


class RecurrenceDayField(forms.Field):
    """Lenient parse: anything that isn't an integer counts as missing, so the
    form reports "Choose a day of ..." (or ignores it for once/daily)."""

    widget = RecurrenceDayWidget

    def to_python(self, value):
        if value in self.empty_values:
            return None
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None


class ChoreForm(forms.ModelForm):
    assigned_to = MemberChoiceField(
        queryset=Member.objects.order_by("order", "id"),
        required=False,
        empty_label="— nobody yet —",
        label="Assigned to",
    )
    recurrence_day = RecurrenceDayField(required=False, label="Recurrence day")
    # Non-model fields. `template` never errors: a bad value just means "no template".
    template = forms.Field(required=False, widget=forms.HiddenInput)
    save_as_template = forms.BooleanField(
        required=False, label="Also save as a household template"
    )

    class Meta:
        model = Chore
        fields = [
            "title",
            "instructions",
            "start_date",
            "due_time",
            "assignment_mode",
            "eligibility",
            "assigned_to",
            "recurrence",
            "recurrence_day",
            "points",
        ]
        labels = {"start_date": "Start date", "due_time": "Due time"}
        widgets = {
            "instructions": forms.Textarea(attrs={"rows": 3}),
            "start_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "due_time": forms.TimeInput(format="%H:%M", attrs={"type": "time"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk is None and "start_date" not in self.initial:
            self.initial["start_date"] = timezone.localdate()
        self.fields["eligibility"].choices = [("", "— choose —")] + list(
            Chore.Eligibility.choices
        )

    @classmethod
    def from_template(cls, template):
        """An unbound create form pre-filled from a template (start date = today)."""
        initial = {name: getattr(template, name) for name in TEMPLATE_FIELDS}
        initial["assigned_to"] = template.assigned_to_id
        initial["template"] = str(template.pk)
        return cls(initial=initial)

    def clean_template(self):
        return find_template(self.cleaned_data.get("template"))

    def save_household_template(self, chore):
        """Create a household template copy of a saved chore."""
        values = {name: getattr(chore, name) for name in TEMPLATE_FIELDS}
        return Chore.objects.create(
            start_date=chore.start_date, is_template=True, is_builtin=False,
            template_source=None, **values,
        )

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("assignment_mode")
        recurrence = cleaned.get("recurrence")

        if mode == Chore.AssignmentMode.ROTATING:
            if not cleaned.get("eligibility") and "eligibility" not in self.errors:
                self.add_error("eligibility", "Choose who is eligible")
        else:
            cleaned["eligibility"] = None
        if mode != Chore.AssignmentMode.FIXED:
            cleaned["assigned_to"] = None

        day = cleaned.get("recurrence_day")
        if recurrence == Chore.Recurrence.WEEKLY:
            if day is None or not 0 <= day <= 6:
                self.add_error("recurrence_day", "Choose a day of the week")
        elif recurrence == Chore.Recurrence.MONTHLY:
            if day is None or not 1 <= day <= 31:
                self.add_error("recurrence_day", "Choose a day of the month")
        else:
            cleaned["recurrence_day"] = None
        return cleaned

    # --- template helpers --------------------------------------------------

    def _current(self, name):
        value = self[name].value()
        return "" if value is None else str(value)

    def _day_options(self, choices, recurrence):
        selected = self._current("recurrence_day") if self._current("recurrence") == recurrence else ""
        return [(str(v), label, str(v) == selected) for v, label in choices]

    def weekday_options(self):
        return self._day_options(WEEKDAY_CHOICES, Chore.Recurrence.WEEKLY)

    def monthday_options(self):
        return self._day_options(MONTHDAY_CHOICES, Chore.Recurrence.MONTHLY)

    def _shown(self, name, active):
        return active or bool(self[name].errors)

    def show_eligibility(self):
        return self._shown("eligibility", self._current("assignment_mode") == Chore.AssignmentMode.ROTATING)

    def show_assigned_to(self):
        return self._shown("assigned_to", self._current("assignment_mode") == Chore.AssignmentMode.FIXED)

    def show_weekday(self):
        return self._current("recurrence") == Chore.Recurrence.WEEKLY

    def show_monthday(self):
        return self._current("recurrence") == Chore.Recurrence.MONTHLY
