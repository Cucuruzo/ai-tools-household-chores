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
    services.py                    # rotation, occurrence projection
    progress.py                    # streaks and badges, computed on read
    progress_views.py              # /progress/ view
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
        progress.html              # streaks and badges
        _chore_card.html           # partial — single chore row on dashboard
        _dashboard_list.html       # partial — chore list (overdue/today/upcoming)
        _calendar_grid.html        # partial — month grid
        _member_row.html           # partial — single member in family list
        _contribution.html         # partial — the contribution section
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
| end_date         | DateField (nullable) — last date the chore is projected on; set by "delete this and future" (#5), never moved later; without history the chore row is deleted only when nothing before D remains (no stored row, no projected date) (#42) |

### ChoreOccurrence

A materialized instance of a chore on a specific date. Created when the week
becomes current or when a one-off / ad-hoc chore is added.

| Field       | Type                                         |
|-------------|----------------------------------------------|
| id          | AutoField (PK)                               |
| chore       | ForeignKey(Chore, on_delete=CASCADE)         |
| member      | ForeignKey(Member, nullable, on_delete=SET_NULL) — can be unassigned |
| date        | DateField                                    |
| due_time    | TimeField (nullable)                         |
| status      | CharField (choices: `todo`, `done`, `skipped`) |
| done_at     | DateTimeField (nullable) — completion time, stored in UTC (#12) |
| is_override | BooleanField (default False) — edited for this occurrence only: set by #13 assign and #20 this occurrence only; never changed by a later this-and-future edit |
| points      | IntegerField — copied from the chore when stored |
| is_cancelled | BooleanField (default False) — "delete this occurrence only" (#5); the row stays so it is never recreated, and it is hidden everywhere |
| completed_by | ForeignKey(Member, nullable, on_delete=SET_NULL) — the member credited with the points (#12); `member` stays the assignee |

**Unique constraint:** (chore, date)

Overdue is **derived**: status is `todo` and date/time is in the past.

### RotationState

| Field       | Type                           |
|-------------|--------------------------------|
| id          | AutoField (PK)                 |
| chore       | OneToOneField(Chore, on_delete=CASCADE) |
| last_member | ForeignKey(Member, nullable, on_delete=SET_NULL) |

Rows are created lazily by the first turn. Only `services.advance_rotation`
writes `last_member`; `services.next_member` is read-only (used by projection).

### ChoreTemplate

Built-in and custom household chore templates, stored as `Chore` records with
`is_template=True`. No separate model needed — templates are just chores that
are never scheduled directly.

### Badges

Badges are computed on read in `chores/progress.py`; there is no model and no
migration. The three rules:

- `streak_7` ("7 in a Row"): the credited member of a `done` row that brings a
  chore's running count to 7 or more.
- `hero` ("Chore Hero"): 10 or more `done` rows credited to the member whose
  `done_at` local date falls in the same Mon-Sun week (any week).
- `completed_50` ("50 Chores Completed"): 50 or more `done` rows credited to
  the member in total.

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

## Edit scope (#20)

Editing a **recurring** chore (`daily` / `weekly` / `monthly`) asks for a
scope. One-off chores ignore `scope` and `occurrence` and save in place
(#35 will sync their row).

- **In place, no split.** "This and future" edits the existing `Chore` row,
  so its pk, streak (#16), `RotationState` (#7) and `template_source` carry
  on. Points are a snapshot on each row (#15), so past totals never change.
- **Cutoff D**: the date of the row given as `?occurrence=<pk>` (the card's
  title link carries it for stored cards of recurring chores), else today in
  the viewer's zone (#26). Same rule as #5's delete. A projected card opens
  the page without `occurrence`, so it is edited "from today" (#38).
- **This occurrence only** (`scope=this`): a stored `todo` row only; it
  changes `due_time` and `points` with one conditional UPDATE that also sets
  `is_override=True` (`services.update_occurrence`). Equal values write
  nothing. Done / skipped rows need an Undo first (400). The member is
  changed on the card (#13); title, instructions and date are #39 / #40.
- **This and future** (`scope=future`, `services.apply_future_edit`), in one
  transaction:
  - every form field is saved on the chore; `end_date`, `template_source`,
    `is_template` and `is_builtin` never change;
  - **start date clamp**: when a rule field (`start_date`, `recurrence`,
    `recurrence_day`) changed, `start_date = max(posted, D)`, so projection
    never puts new-rule dates before D;
  - rows dated before D, and done, skipped, cancelled and **override** rows on
    or after D, are never written or deleted (one flag, no per-field tracking:
    a row reassigned with #13 also keeps its points and due time);
  - **follower rows** (todo, not cancelled, not override, date >= D): after a
    rule change, a row no longer on the new rule (`services.on_rule`) is
    deleted and its rotation turn is not given back; the others get the new
    due time and points, and a new member from `advance_rotation` when an
    assignment field (`assignment_mode`, `eligibility`, `assigned_to`)
    changed;
  - **fill**: after a rule change, every new-rule date of the current week
    from `max(D, current Monday)` without any row (cancelled included) is
    stored with `create_occurrence`. Followers and fill dates go in one pass
    in date order, so turns follow the calendar. Past weeks are never filled;
    future weeks are projected from the day after the latest stored row;
  - an `IntegrityError` from a fill row (a concurrent materialization) rolls
    the whole edit back, turns included, and it is retried once, as
    `materialize_week` does. Any other error leaves everything as before.
- **Unstored dates**: after a rule change, old-rule dates between Monday and
  D-1 that nobody stored are never created (the clamp puts `start_date` at
  D). Without a rule change, unstored dates are projected and stored later
  with the new values.
- **Once ↔ repeating**: not allowed once the chore has any stored row (form
  error on `recurrence`); stop a repeating chore with Delete (#5). Without
  rows it is a plain in-place save.
- **Accepted costs**: `title` and `instructions` live only on `Chore`, so a
  rename also shows on rows dated before D (history, contribution, progress,
  past cards) until #39. After a recurrence change, older rows still count in
  the same streak (6 daily `done` rows and 1 weekly `done` row show 7 in a
  row).

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
/chores/<id>/edit/                 GET    edit chore form (recurring: cutoff D = today)
/chores/<id>/edit/?occurrence=<pk> GET    edit a recurring chore from one stored row (#20): D = its date,
                                          plus the "This occurrence only" form for a todo row;
                                          bad pk, another chore's row or a cancelled row: 404
/chores/<id>/edit/                 POST   save chore edits; recurring: scope=this|future (#20, missing = future,
                                          anything else 400), occurrence=<pk> as on GET
/chores/<id>/delete/               POST   delete chore (with scope: this/future)
/chores/create/                    POST   save new chore
/chores/templates/                 GET    manage household templates
/chores/templates/<id>/edit/       GET    edit template form
/chores/templates/<id>/edit/       POST   save template edits
/chores/templates/<id>/delete/     POST   delete household template

/family/                           GET    family management screen
/family/?edit=<id>                 GET    family screen with that member's edit form open (no-JS Edit link)
/family/?remove=<id>               GET    family screen with that member's remove confirmation (no-JS Remove link), posts to /family/<id>/delete/
                                          id not ASCII digits, out of range, repeated or no such member: 404; edit and remove together: 400
/family/add/                       POST   add member
/family/<id>/edit/                 POST   edit member
/family/<id>/move/?dir=up|down     POST   move member up/down in rotation order
/family/<id>/delete/               POST   remove member

/profile/<id>/                     POST   switch active member

/progress/                         GET    streaks and badges
/contribution/                     GET    contribution / progress view
/contribution/?period=week         GET    this week's contribution
/contribution/?period=month        GET    this month's contribution

/history/                          GET    history view (completed + skipped)
/history/?member=<id>              GET    filtered by assignee or credited member
/history/?page=<n>                 GET    50 per page
```

## Screen Layouts

### Weekly Dashboard (main screen)

```
+-------------------------------------------------------+
| [Profile switcher: avatar + name]     Household Chores |
+-------------------------------------------------------+
| < This Week (Sep 7 - 13, 2026) >    [+ Add Chore]     |
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

Each in-month date cell shows the chore count and up to 3 status dots ("+N" for
the rest). Only months from 12 months before to 12 months after the current month
can be shown; any other `?month=` falls back to the current month. The day number
links to the dashboard week filtered to that day (`/dashboard/?week=…&day=…`), and
a "+" link opens chore creation with that date pre-filled (`?date=YYYY-MM-DD`).

## HTMX Interaction Patterns

| Action                  | Trigger          | HTMX Attribute         | Swaps                  |
|-------------------------|------------------|------------------------|------------------------|
| Mark chore done/skip    | Click button     | `hx-post`              | `#card-<pk>` (the `card-block` wrapper in `_chore_card.html`) |
| Undo completion         | Click undo       | `hx-post`              | `#card-<pk>` (the `card-block` wrapper in `_chore_card.html`) |
| Navigate week           | Click < or >     | `hx-get`               | `_dashboard_list.html` |
| Filter by member/day    | Change dropdown  | `hx-get`               | `_dashboard_list.html` |
| Assign/reassign         | Change dropdown  | `hx-post` (field `assignee`) | `#occurrence-<pk>` (`_chore_card.html` with `card_only`); on a member-filter mismatch `HX-Retarget: #dashboard-list` + `_dashboard_list.html` |
| Navigate month          | Click < or >     | `hx-get`               | `_calendar_grid.html`  |
| Add/edit/remove member  | Submit form      | `hx-post`              | `_member_row.html`     |
| Pick template           | Click template   | `hx-get`               | pre-fills chore form   |
| Switch contribution period | Click link    | `hx-get`               | `_contribution.html`   |

## Points and Streaks

### Points
- Each chore has a fixed point value: 5, 10, or 20.
- Points awarded on completion, removed on undo.
- Skipped chores give no points.
- Late completion gives full points.
- Contribution view aggregates by member for current week / month. It groups by `completed_by` (who was credited, not the assignee) and counts a done row by the local date of `done_at` in the viewer's zone.

### Streaks
- Counted per chore, in that chore's own occurrences (days, weeks or months),
  never per member and never across chores.
- Uses the occurrence `date`, not `done_at`. Only counted rows are used: not
  cancelled, not a template's.
- Newest first: `done` rows count; the first `skipped` row, or the first
  `todo` row dated before today, stops the count. A `todo` dated today or
  later is passed over (it can still be done), and so are dates with no row
  (an unopened week, a cancelled date). Who did it does not matter.
- "Today" is the viewer's local date (`tz` cookie), read once in the view and
  passed in; `chores/progress.py` never reads the clock.
- Calculated on read from `ChoreOccurrence` history; nothing is cached, so an
  undo changes the streak on the next read.

### Badges
- Computed on read, never stored (see the Badges rules in the data model).
- A broken streak keeps its `streak_7` badge, because the earlier run is still
  in the history.
- An undo removes a badge only when it undoes the row that made the milestone;
  doing it again brings the badge back.
- A deleted member's rows lose their credit (SET_NULL), so the badges go with
  the member; nobody else gains one.

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
