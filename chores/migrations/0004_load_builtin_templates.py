import datetime
import json
from pathlib import Path

from django.db import migrations

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "chore_templates.json"

# Only plain columns are copied, so the fixture keeps working when later
# migrations add fields (they get their defaults on the historical model).
FIELDS = [
    "title", "instructions", "start_date", "due_time", "assignment_mode",
    "eligibility", "recurrence", "recurrence_day", "points",
]


def load_builtins(apps, schema_editor):
    Chore = apps.get_model("chores", "Chore")
    entries = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for entry in entries:
        data = {name: entry["fields"][name] for name in FIELDS}
        data["start_date"] = datetime.date.fromisoformat(data["start_date"])
        if data["due_time"]:
            data["due_time"] = datetime.time.fromisoformat(data["due_time"])
        # Idempotent: never create a second copy of a built-in.
        if Chore.objects.filter(is_builtin=True, title=data["title"]).exists():
            continue
        Chore.objects.create(is_template=True, is_builtin=True, **data)


def remove_builtins(apps, schema_editor):
    Chore = apps.get_model("chores", "Chore")
    Chore.objects.filter(is_builtin=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("chores", "0003_chore_is_builtin"),
    ]

    operations = [
        migrations.RunPython(load_builtins, remove_builtins),
    ]
