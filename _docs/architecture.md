# Architecture — Household Chore Manager

## Chosen Approach

**Django Monolith + HTMX** — server-rendered with HTMX for partial page updates,
delivering a responsive multi-screen experience without a JavaScript framework.

## Tech Stack

| Layer         | Technology                                    |
|---------------|-----------------------------------------------|
| Backend       | Django 4.2 (Python 3.9)                       |
| Interactivity | HTMX                                          |
| CSS           | Pico CSS (classless, minimal)                 |
| Database      | SQLite (single household, no scale concerns)  |
| Deployment    | Single process (`manage.py runserver` / gunicorn) |

## Project Structure

```
ai-tools-household-chores/
  manage.py
  ai_tools_household_chores/       # Django project config
    settings.py
    urls.py
    wsgi.py
    asgi.py
  chores/                          # main Django app
    models.py
    services.py                    # rotation, occurrence projection, streaks
    forms.py
    views.py
    urls.py
    admin.py
    migrations/
    templatetags/
      chores_tags.py
    templates/
      chores/
        base.html                  # shell: Pico CSS, HTMX, profile switcher
        dashboard.html             # weekly dashboard (main screen)
        calendar.html              # monthly calendar
        chore_form.html            # create / edit chore (full page)
        family.html                # family management screen
        contribution.html          # contribution / progress view
        history.html               # history view
        _chore_card.html           # partial — single chore row on dashboard
        _dashboard_list.html       # partial — chore list (overdue/today/upcoming)
        _calendar_grid.html        # partial — month grid
        _member_row.html           # partial — single member in family list
        _template_picker.html      # partial — chore template chooser
    static/
      chores/
        style.css
    fixtures/
      chore_templates.json         # built-in chore templates
  _docs/
```

## Data Model

### Member

| Field   | Type                                    |
|---------|-----------------------------------------|
| id      | AutoField (PK)                          |
| name    | CharField                               |
| role    | CharField (choices: `adult`, `kid`)     |
| avatar  | CharField (emoji or icon identifier)    |
| order   | PositiveIntegerField (rotation order)   |

On deletion: active chore occurrences assigned to this member become unassigned.

### Chore

| Field            | Type                                                     |
|------------------|----------------------------------------------------------|
| id               | AutoField (PK)                                           |
| title            | CharField (max_length=100)                               |
| instructions     | TextField (blank, optional)                              |
| start_date       | DateField (required) — due date of a one-off chore, first date of a recurring one |
| due_time         | TimeField (nullable) — optional time of day              |
| assignment_mode  | CharField (choices: `fixed`, `rotating`, `unassigned`)   |
| eligibility      | CharField (choices: `kids`, `adults`, `everyone`, nullable) — only for `rotating` |
| assigned_to      | ForeignKey(Member, nullable) — only for `fixed`          |
| recurrence       | CharField (choices: `once`, `daily`, `weekly`, `monthly`) |
| recurrence_day   | IntegerField (nullable) — day of week (0=Mon) for weekly, day of month for monthly |
| points           | IntegerField (choices: 5, 10, 20)                        |
| is_template      | BooleanField (default False)                             |
| is_builtin       | BooleanField (default False) — built-in template shipped with the app (read-only) |
| template_source  | ForeignKey('self', nullable, SET_NULL) — template (built-in or household) it was created from |

### ChoreOccurrence

A materialized instance of a chore on a specific date. Created when the week
becomes current or when a one-off / ad-hoc chore is added.

| Field       | Type                                         |
|-------------|----------------------------------------------|
| id          | AutoField (PK)                               |
| chore       | ForeignKey(Chore)                            |
| member      | ForeignKey(Member, nullable) — can be unassigned |
| date        | DateField                                    |
| due_time    | TimeField (nullable)                         |
| status      | CharField (choices: `todo`, `done`, `skipped`) |
| done_at     | DateTimeField (nullable)                     |
| is_override | BooleanField (default False) — edited for this occurrence only |

**Unique constraint:** (chore, date)

Overdue is **derived**: status is `todo` and date/time is in the past.

### RotationState

| Field       | Type                           |
|-------------|--------------------------------|
| id          | AutoField (PK)                 |
| chore       | OneToOneField(Chore)           |
| last_member | ForeignKey(Member, nullable)   |

### ChoreTemplate

Built-in and custom household chore templates, stored as `Chore` records with
`is_template=True`. No separate model needed — templates are just chores that
are never scheduled directly.

### Badge

| Field       | Type                           |
|-------------|--------------------------------|
| id          | AutoField (PK)                 |
| member      | ForeignKey(Member)             |
| badge_type  | CharField (e.g. `streak_7`, `hero`, `completed_50`) |
| awarded_at  | DateTimeField (auto_now_add)   |

## Rotation Logic

1. For each `rotating` chore occurrence being materialized:
   - Look up `RotationState.last_member` for the chore.
   - Get eligible members by `chore.eligibility`, ordered by `Member.order`.
   - Pick the next member after `last_member` (wrap around).
   - Update `RotationState.last_member`.
2. Rotation **advances on every scheduled occurrence**, including skipped ones.
3. `fixed` chores always use `chore.assigned_to`.
4. `unassigned` chores get no member until manually assigned.
5. Manual reassignment on a single occurrence does not affect `RotationState`.
6. If no eligible members exist, the occurrence stays unassigned.

## Occurrence Projection Strategy

- Future recurring occurrences are **projected on read**, not stored.
- When displaying a future week/month, the system calculates what would appear
  based on recurrence rules and current rotation state, without writing to the DB.
- When a week becomes **current** (the user views the current week's dashboard),
  occurrences for that week are **materialized** as `ChoreOccurrence` records.
- Already-materialized occurrences are never overwritten (preserving manual edits).
- Viewing a future week shows projected data; only the current week materializes.

## Screens and URL Design

```
/                                  GET    redirect to dashboard
/dashboard/                        GET    weekly dashboard (default: this week)
/dashboard/?week=2026-W37          GET    dashboard for a specific week
/dashboard/?member=<id>            GET    filtered by member
/dashboard/toggle/<id>/            POST   mark done / undo / skip
/dashboard/assign/<id>/            POST   assign or reassign occurrence

/calendar/                         GET    monthly calendar (default: this month)
/calendar/?month=2026-09           GET    calendar for a specific month

/chores/create/                    GET    chore creation form
/chores/create/?date=2026-09-08    GET    chore creation with pre-filled date
/chores/<id>/edit/                 GET    edit chore form
/chores/<id>/edit/                 POST   save chore edits
/chores/<id>/delete/               POST   delete chore (with scope: this/future)
/chores/create/                    POST   save new chore

/family/                           GET    family management screen
/family/add/                       POST   add member
/family/<id>/edit/                 POST   edit member
/family/<id>/move/?dir=up|down     POST   move member up/down in rotation order
/family/<id>/delete/               POST   remove member

/profile/<id>/                     POST   switch active member

/contribution/                     GET    contribution / progress view
/contribution/?period=week         GET    this week's contribution
/contribution/?period=month        GET    this month's contribution

/history/                          GET    history view (completed + skipped)
```

## Screen Layouts

### Weekly Dashboard (main screen)

```
+-------------------------------------------------------+
| [Profile switcher: avatar + name]     Household Chores |
+-------------------------------------------------------+
| < This Week (Sep 8 - 14, 2026) >    [+ Add Chore]     |
| Filters: [Member v] [Day v]                           |
+-------------------------------------------------------+
| OVERDUE                                                |
|   [ ] Take out trash — Dad — Sep 6 — 10 pts           |
+-------------------------------------------------------+
| TODAY                                                  |
|   [ ] Load dishwasher — Emma — 5 pts                  |
|   [ ] Clean bathroom — unassigned — 20 pts            |
+-------------------------------------------------------+
| UPCOMING                                               |
|   Wed: Vacuum living room — Mom — 10 pts              |
|   Fri: Change bed sheets — rotating — 20 pts          |
+-------------------------------------------------------+
| [Contribution] [Calendar] [Family] [History]           |
+-------------------------------------------------------+
```

### Monthly Calendar

```
+-------------------------------------------------------+
| < September 2026 >                                     |
+-------------------------------------------------------+
| Mon  Tue  Wed  Thu  Fri  Sat  Sun                      |
|  1    2    3    4    5    6    7                        |
| ...  ...  ...  ...  ...  ...  ...                      |
+-------------------------------------------------------+
```

Each date cell shows chore count / icons. Clicking a date opens chore creation
with that date pre-filled.

## HTMX Interaction Patterns

| Action                  | Trigger          | HTMX Attribute         | Swaps                  |
|-------------------------|------------------|------------------------|------------------------|
| Mark chore done/skip    | Click button     | `hx-post`              | `_chore_card.html`     |
| Undo completion         | Click undo       | `hx-post`              | `_chore_card.html`     |
| Navigate week           | Click < or >     | `hx-get`               | `_dashboard_list.html` |
| Filter by member/day    | Change dropdown  | `hx-get`               | `_dashboard_list.html` |
| Assign/reassign         | Submit dropdown  | `hx-post`              | `_chore_card.html`     |
| Navigate month          | Click < or >     | `hx-get`               | `_calendar_grid.html`  |
| Add/edit/remove member  | Submit form      | `hx-post`              | `_member_row.html`     |
| Pick template           | Click template   | `hx-get`               | pre-fills chore form   |

## Points and Streaks

### Points
- Each chore has a fixed point value: 5, 10, or 20.
- Points awarded on completion, removed on undo.
- Skipped chores give no points.
- Late completion gives full points.
- Contribution view aggregates by member for current week / month.

### Streaks
- Per-chore streak: consecutive scheduled occurrences completed (not skipped).
- Skipping breaks the streak.
- Calculated on read from `ChoreOccurrence` history.

### Badges
- Awarded automatically when thresholds are met.
- Examples: `7-Day Streak`, `Chore Hero`, `50 Chores Completed`.
- Stored in `Badge` model so they persist even if the streak breaks.

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| No auth | Single household, honor system per spec |
| Profile switcher in header | Lightweight active-member selection without login |
| SQLite | Single household, no concurrent write pressure |
| Projected future occurrences | Avoids storing unbounded future data; rotation calculated on the fly |
| Materialize on current week | Gives concrete records for status tracking and manual edits |
| Templates as Chore records | Reuses the same model; `is_template=True` flag distinguishes them |
| Derived overdue status | No cron job needed; calculated from date + status on each request |
