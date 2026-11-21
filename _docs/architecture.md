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
        base.html                  # shell: Pico CSS, style.css, HTMX, profile switcher, site nav
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
| end_date         | DateField (nullable) — last date the chore is projected on; set by "delete this and future" (#5), never moved later; without history the chore row is deleted only when nothing before D remains (no stored row, no projected date) (#42); a "this only" delete that leaves nothing to show up to `end_date` deletes the chore row too (#43) |

### ChoreOccurrence

A materialized instance of a chore on a specific date. Created when the week
becomes current or when a one-off / ad-hoc chore is added.

| Field       | Type                                         |
|-------------|----------------------------------------------|
| id          | AutoField (PK)                               |
| chore       | ForeignKey(Chore, on_delete=CASCADE)         |
| title       | CharField(100) — snapshot of the chore's title when stored (#39) |
| instructions | TextField (blank) — snapshot of the chore's instructions when stored (#39) |
| member      | ForeignKey(Member, nullable, on_delete=SET_NULL) — can be unassigned |
| date        | DateField                                    |
| due_time    | TimeField (nullable)                         |
| status      | CharField (choices: `todo`, `done`, `skipped`) |
| done_at     | DateTimeField (nullable) — completion time, stored in UTC (#12) |
| is_override | BooleanField (default False) — edited for this occurrence only: set by #13 assign and #20 this occurrence only; never changed by a later this-and-future edit |
| points      | IntegerField — copied from the chore when stored |
| is_cancelled | BooleanField (default False) — "delete this occurrence only" (#5); the row stays so it is never recreated, and it is hidden everywhere, unless the chore has an `end_date` and nothing is left to show, then the chore and its rows are deleted (#43) |
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

### OccurrenceException

A per-date change to a **projected** (not yet stored) occurrence (#31). It is
applied when projecting and used up when the week is materialized. #22 (delete
a projected date) reuses it, and #38 lets a date that was not moved carry its
own values (`own_values`, migration `0012_occurrenceexception_own_values`).

| Field        | Type                                         |
|--------------|----------------------------------------------|
| id           | AutoField (PK)                               |
| chore        | ForeignKey(Chore, on_delete=CASCADE, related_name="exceptions") |
| date         | DateField                                    |
| is_cancelled | BooleanField (default False): the date is not shown, and is stored with `is_cancelled=True` when the week is materialized. Written by "This occurrence only" on a Planned card (#22, `services.cancel_projected_date`); an override on the same row stays but no longer matters |
| is_override  | BooleanField (default False): `member` replaces the projected member |
| member       | ForeignKey(Member, nullable, on_delete=SET_NULL): None means Unassigned, so deleting the member leaves an override to Unassigned |
| moved_from   | DateField (nullable): set only on an **extra** (#48), a date moved into a later week. It is the date the occurrence was first moved from |
| own_values   | CharField(6, blank, default "", choices `edited` / `kept`, #38): `""` no values of its own; `"edited"` written by "This occurrence only" on a Planned date (always also an override); `"kept"` written by "this and future" from a later Planned date, holding the values the date showed before (an override only when an assignment change froze its member) |
| title        | CharField(100, blank, default ""): read only on an extra or a date with `own_values`, its own title |
| instructions | TextField (blank, default ""): read only on an extra or a date with `own_values`, its own instructions |
| due_time     | TimeField (nullable): read only on an extra or a date with `own_values`, its own due time |
| points       | IntegerField (nullable): read only on an extra or a date with `own_values`, its own points |

**Unique constraint:** (chore, date), `unique_exception_per_chore_date`

A stored row on the same date always wins. An exception on a date that is not
on the chore's rule is ignored. "This and future" from D (`views._delete_from`,
every caller: stored card, edit page, Planned card) deletes the chore's
exceptions dated ≥ D; earlier ones stay (#22).

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
7. A date cancelled or reassigned in advance (`OccurrenceException`, #31)
   still takes its turn. `RotationState.last_member` becomes that date's
   **rotation** member, never the override member, and no other date's member
   changes.

Exception: undoing a delete (#24) re-inserts a `RotationState` row that the
delete cascaded away, with its old id and `last_member`; it never advances or
edits an existing row.

## Occurrence Projection Strategy

- Future recurring occurrences are **projected on read**, not stored.
- When displaying a future week/month, the system calculates what would appear
  based on recurrence rules and current rotation state, without writing to the DB.
- When a week becomes **current** (the user views the current week's dashboard),
  occurrences for that week are **materialized** as `ChoreOccurrence` records.
- Already-materialized occurrences are never overwritten (preserving manual edits).
- Viewing a future week shows projected data; only the current week materializes.
- `OccurrenceException` rows are **applied on read**. Projection loads them in
  one query bounded to `[max(start, current Monday), end]`. A cancelled date
  is left out, and an override date gets the exception's member and
  `is_override=True` (a key on every projected dict). Window and turn counting
  do not change, so the query count stays constant.
- An **extra** (`moved_from` set, #48) is returned with its own title,
  instructions, due time, points and member, and `is_override=True`. Off
  the rule it **takes no turn** and is returned when
  `max(start, current Monday, latest stored row + 1 day) <= T <= min(end,
  end_date)` (`start_date` does not limit it). On a rule date (only after a
  later rule change) it replaces that date, which keeps its turn. A
  cancelled extra is not returned; for a one-off chore `moved_from` is
  ignored. Every projected dict has an `instructions` key: the chore's, or
  the extra's own (Planned cards show it).
- A rule date whose exception has **`own_values`** (`"edited"` / `"kept"`,
  #38) is applied like an extra on a rule date: it shows the exception's
  title, instructions, due time and points and keeps its turn. Its member is
  the override member when `is_override`, else the rotation member, and
  `is_override` in the dict is True only for an override. Off the rule it is
  ignored (only `moved_from` puts a date on the list of extras off the rule),
  and a one-off ignores it. The dict keys and the query count do not change.
- They are **consumed at materialization**: `materialize_week` stores an
  override as a row with that member and `is_override=True`, and a cancelled
  date as a row with `is_cancelled=True`. Each one still calls
  `advance_rotation`. An extra is stored by `_store_extra`: a todo row with
  its own values and `is_override=True`, taking a turn only on a rule date
  (a cancelled extra off the rule stores nothing). A date with
  `own_values` is stored by `_store_extra` too, with the projected
  `is_override`: an "edited" date (or a frozen "kept" one) as an override, a
  "kept" date that is not an override with `is_override=False`, so it stays a
  follower row for later edits. Then every exception
  dated on or before that Sunday is deleted in the same transaction.
- `services.projected_occurrence(chore, day, today)` is the only way views
  decide "D is a projected date of this chore".

## Edit scope (#20)

Editing a **recurring** chore (`daily` / `weekly` / `monthly`) asks for a
scope. One-off chores ignore `scope` and `occurrence` and save in place,
keeping their row in sync (see [Editing a one-off (#35)](#editing-a-one-off-35)).

- **In place, no split.** "This and future" edits the existing `Chore` row,
  so its pk, streak (#16), `RotationState` (#7) and `template_source` carry
  on. Points are a snapshot on each row (#15), so past totals never change.
- **Cutoff D**: the date of the row given as `?occurrence=<pk>` (the card's
  title link carries it for stored cards of recurring chores), else today in
  the viewer's zone (#26). Same rule as #5's delete. A Planned card of a
  recurring chore links to `?<filters>&planned=YYYY-MM-DD` (#38), and D is
  that date; see [Editing a Planned date (#38)](#editing-a-planned-date-38).
- **This occurrence only** (`scope=this`): a stored `todo` row only; it
  changes `title`, `instructions`, `due_time` and `points` with one
  conditional UPDATE that also sets `is_override=True`
  (`services.update_occurrence`, #39). Equal values write nothing. Done /
  skipped rows need an Undo first (400). The member is changed on the card
  (#13); a new date moves the row (see
  [Moving one occurrence (#40)](#moving-one-occurrence-40)).
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
    due time and points, the chore's title and instructions (when they
    differ, #39), and a new member from `advance_rotation` when an
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
  rows, Once → repeating is a plain in-place save (exceptions are left as
  they are), and a switch to Once follows
  [Editing a one-off (#35)](#editing-a-one-off-35).
- **Accepted costs**: each stored row keeps a snapshot of the chore's
  `title` and `instructions` (#39), so a rename from D changes only follower
  rows; rows before D and done, skipped, cancelled or override rows keep the
  old title. Planned cards and dots, and `/progress/`, show the chore's
  current title. After a recurrence change, older rows still count in
  the same streak (6 daily `done` rows and 1 weekly `done` row show 7 in a
  row). Stored cards and history show the row's own instructions, and Planned
  cards the chore's current ones (#47). An extra (#48) keeps its member,
  title, instructions, due time and points through later edits: "this and
  future" never writes an extra; from a Planned date it only pins the dates before D (#38).

## Editing a Planned date (#38)

The edit page with `planned=D` (`views._edit_planned`): D must be a
projected date of the chore after this Sunday for the viewer's today (#26),
else 404 "Not a planned date of this chore" (GET and POST). `occurrence` and
`planned` together are a 400. A one-off ignores `planned`. The scope note
reads "Changes apply from Thu 10 Dec. …" and is followed by
`#edit-from-today` (the same page without `planned`). GET writes nothing.

- **This occurrence only** (`services.edit_projected_date`): Title,
  Instructions, Due time and Points, no Date field (a Planned date moves on
  its card, #48; a posted `date` is ignored). Equal values write nothing. An
  extra only gets its four values; any other date's exception gets them,
  `own_values="edited"`, and, when it was not yet an override,
  `is_override=True` with the member D shows (a #31 override keeps its
  member). No `ChoreOccurrence` or `RotationState` is written. Redirect to
  `?<filters>&planned=D`, "Occurrence saved".
- **This and future from D** (`apply_future_edit`, in place):
  1. A rule change (`start_date`, `recurrence`, `recurrence_day`, or a
     switch to Once) is refused (`FutureEditRefused`): "The schedule can
     only change from today. Use Edit from today." as a non-field error,
     status 200, nothing written.
  2. When a value field (`VALUE_FIELDS`, compared with the chore as stored)
     or an assignment field changed, the projected dates in [next Monday,
     D - 1] are pinned (`_keep_planned_dates`): more than `MAX_KEPT_DATES`
     (366) rule dates there are refused first ("More than 366 planned dates
     come before …"). The current week is materialized, then each date gets
     the values it shows with `own_values="kept"` (an extra or a date that
     already has `own_values` keeps what it has) and, after an assignment
     change, `is_override=True` with the member it shows (unless it is
     already an override).
  3. In every "this and future" edit (from today, a stored row or a Planned
     date) the chore's `"kept"` exceptions dated on or after D give way
     (`_clear_kept`): deleted when neither override nor cancelled, else
     `own_values=""`.
  4. The chore is saved in place; redirect to `?<filters>&planned=D`.
- **Accepted cost**: after an assignment change from D, the new pool's
  rotation counts every date from the window start, so a member can get two
  dates in a row (Emma on Thu 3 and Thu 10 in the Bins example); the frozen
  overrides keep their member through later edits, like a #13 reassignment.

## Moving one occurrence (#40)

"This occurrence only" has a Date field (the row's date, no `min`/`max`).
An empty or unchanged date is the #39 save above. Any other date calls
`services.move_occurrence` instead of `update_occurrence`:

- **One transaction**, in order: `materialize_week(current Monday, today)`,
  so every rule date of the week that can still happen has a row; a lock
  on the chore, then on the row (todo, not cancelled; else a lost race:
  nothing is written, 404 or 400 "Undo first"); the limit checks; one
  UPDATE of the row (`date`, `due_time`, `points`, `title`, `instructions`,
  `is_override=True`, same pk); then a **tombstone** on the old date: a
  cancelled todo row with the row's old member, due time, points, title
  and instructions. The UPDATE comes first because it frees `(chore, S)`.
- **Limits** (`MoveRefused`, a form error under Date, status 200, nothing
  written): the target must be in `[today, min(31 Dec 2099, end_date)]`
  ("Pick a day from Wed 18 Nov to Thu 31 Dec 2099."; an `end_date` before today
  gives "This chore has ended, so this occurrence can't move.") and must
  have no row of this chore except a cancelled to-do row, which is deleted
  first (#49; a #24 `cancel` undo of that row is then refused) ("This chore
  already has an occurrence on Thu 19 Nov."). The source can be any date, including an
  overdue row from an earlier week. A daily chore is always refused.
- **Rotation**: the date keeps its turn and member; `advance_rotation` is
  not called and `RotationState` is not written.
- The tombstone is shown nowhere and is never projected or materialized
  again, so the projection window never moves back before the source.
  The moved row is an override, so "this and future" never touches it.
  Streaks, history, contribution and the calendar count it on its new
  date only.
- An `IntegrityError` (a concurrent insert) rolls the attempt back and it
  is retried once from a fresh read. `today` is the viewer's local date,
  read once by the view (`scope_today`).
- A target after this Sunday is
  [Moving into a later week (#48)](#moving-into-a-later-week-48).
- Out of scope: moving back onto a date the occurrence left (#49).

## Moving into a later week (#48)

A future week has no rows, so a move into it is a pair of exceptions: a
cancel on the source date S and an **extra** on the target date T
(`moved_from` set, `is_override=True`, its own title, instructions, due
time and points, and the member S showed at move time, frozen). The extra
takes no rotation turn; S keeps its turn (rule 7).

- **Planned date** (`POST /dashboard/move/planned/<id>/`,
  `services.move_projected_date`): S must be a projected date after this
  Sunday (else 404), and T in `[next Monday, min(end_date, 31 Dec 2099)]`.
  S gets `is_cancelled=True` as `cancel_projected_date` writes it, and the
  extra copies S's projected values. If S is itself an extra, that row only
  gets `date=T`, and `moved_from` stays. A one-off chore is a 400.
- **Stored row** (edit page, "This occurrence only", T after this Sunday):
  the row keeps its pk, date and values and only gets `is_cancelled=True`
  (it is its own tombstone). The extra has `moved_from=S`, the row's member
  and the posted values. The edit page redirect has no `occurrence=`.
- **Taken**: T is refused when the chore has a row, any exception or a
  projected date on T ("This chore already has an occurrence on Thu 26
  Nov."). Dates in messages are `D j M`, plus ` Y` outside today's year.
- What the future week shows is what is stored when it becomes current.
  Nothing writes `RotationState` or a `ChoreOccurrence`; an `IntegrityError`
  rolls the attempt back and it is retried once.
- Out of scope: into the current week or back onto the source (#51), onto a
  date with another exception (#53).

## Editing a one-off (#35)

Saving the edit form of a chore that is `once` after the save (a one-off
edit, or a repeating chore switched to Once without rows) runs
`services.apply_one_off_edit(chore, old, today)`. `today` is read once per
request (`scope_today` when a scope exists, else the viewer's local date) and
the service never reads the clock. *Old date* / *new date* are `start_date`
before / after the save; *due by Sunday* means on or before the current
Sunday (past weeks included). *The row* is the chore's only
`ChoreOccurrence`; *the exception* is its `OccurrenceException` on the old
date (only an override can exist for a one-off).

| Before the save | New date due by Sunday | New date in a future week |
|---|---|---|
| Row `todo`, not cancelled | Same row (same pk) gets the new date, the chore's `due_time` and `points`. Member and `is_override` are kept, except after an assignment change | Row deleted. If it `is_override` or the chore rotates (and no assignment field changed), `OccurrenceException(new date, is_override=True, member=row.member)` is written; otherwise projection gives the same member |
| Row `done` / `skipped` / cancelled | Row never written; other fields are saved on the chore only. A changed date is a form error | same |
| No row | Row created with `materialize_one_off`. With the exception and no assignment change, the row then gets its member and `is_override=True`, and the exception is deleted (the date still takes its rotation turn, as in `materialize_week`) | No row. The exception, if kept, moves to the new date |

- **Assignment change** (any of `ASSIGNMENT_FIELDS`): the form's latest
  choice wins. A todo row gets the new member (one `advance_rotation` turn
  for rotating) and `is_override=False`, and the exception is deleted.
  Without one, a todo row's member is never touched.
- Exceptions of the chore on any **other date** than the old date are
  deleted, so none comes back if the date moves back.
- **Equal values write nothing**: an unchanged re-save writes only the
  `Chore` row, no occurrence, exception or rotation row.
- **History**: changing the date while the row is done, skipped or
  cancelled is a form error on `start_date` ("Undo Done or Skip on the
  dashboard before changing the date of this chore."), checked in
  `ChoreForm.clean` and again under the lock (`OneOffEditRefused`, the whole
  save rolls back).
- **`end_date` never changes.** A one-off with `end_date` and a todo row
  (deleted with history, then Undo) may move to this week or earlier; a move
  into a future week would hide it and is refused ("This chore was deleted,
  so it can only move to this week or earlier.").
- Crafted data with more than one row: only the chore is saved.
- One transaction after `Chore.objects.select_for_update()`, as
  `apply_future_edit`. An `IntegrityError` (a concurrent store of the same
  date) rolls the attempt back, turns included, and is retried once.
- Title and instructions are a snapshot on the row (#39): a todo row due by
  Sunday gets the chore's new values, and a done, skipped or cancelled row
  keeps the ones it had when stored.

## Undo a delete (#24)

Every delete through `chore_delete` that wrote something keeps the prior
state in one slot per browser, `request.session["chore_delete_undo"]`
(`chores/undo.py`), written after the delete commits and replacing any
earlier slot; the page only carries its random 32-hex token. The Undo button
next to "Chore deleted" posts it to `/chores/undo-delete/`, which works for
600 seconds (UTC) and puts back exactly what that delete changed, with the
original ids, or nothing (400). Four kinds, the last two decided by whether
the chore row still exists after the delete:

- `cancel`: a stored row deleted on its own goes back to `is_cancelled=False`.
- `planned`: a Planned date deleted on its own; its exception is deleted, or
  an override that was there gets `is_cancelled=False` again.
- `trim`: the chore stays; `end_date` goes back and the removed to-do rows and
  exceptions are re-inserted. Refused after an edit of the chore or when a
  row / exception on those dates was stored since.
- `chore`: the chore row is gone; it is re-inserted with every row,
  exception and its `RotationState`. FKs to removed members or templates
  come back NULL. Also after a "this only" delete that leaves the chore
  nothing to show (#43), with the state from before that cancel.

## Screens and URL Design

```
/                                  GET    redirect to dashboard
/dashboard/                        GET    weekly dashboard (default: this week)
/dashboard/?week=2026-W37          GET    dashboard for a specific week
/dashboard/?member=<id>            GET    filtered by member
/dashboard/toggle/<id>/            POST   mark done / undo / skip
/dashboard/assign/<id>/            POST   assign or reassign occurrence
/dashboard/assign/planned/<id>/    POST   assign a projected date of chore <id> (fields `date`, `assignee`; #31)
/dashboard/move/planned/<id>/  POST  move a projected date of chore <id> (fields date, target; #48)

/calendar/                         GET    monthly calendar (default: this month)
/calendar/?month=2026-09           GET    calendar for a specific month

/chores/create/                    GET    chore creation form
/chores/create/?date=2026-09-08    GET    chore creation with pre-filled date
/chores/create/?date=2026-09-08&recurrence=once&return_to=dashboard[&week=&member=&day=]
                                   GET    "+ Add Chore" from the dashboard (#19): back to that view after saving
/chores/create/?date=…&return_to=calendar[&month=YYYY-MM]  GET  "+" on a calendar day (#36): back to that month after saving
/chores/<id>/edit/                 GET    edit chore form (recurring: cutoff D = today)
/chores/<id>/edit/?occurrence=<pk> GET    edit a recurring chore from one stored row (#20): D = its date,
                                          plus the "This occurrence only" form for a todo row;
                                          bad pk, another chore's row or a cancelled row: 404
/chores/<id>/edit/                 POST   save chore edits; recurring: scope=this|future (#20, missing = future,
                                          anything else 400), occurrence=<pk> as on GET
/chores/<id>/delete/               POST   delete chore (with scope: this/future), from a stored row (`occurrence=<pk>`)
                                          or a projected date (`date=YYYY-MM-DD`, #22); both given: 400; a bad `date`: 400
                                          `Invalid date`; a `date` that is not a projected date of the chore for the
                                          viewer's today: 404
/chores/undo-delete/               POST   undo the last delete (#24)
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
| Dashboard · Calendar · Family · Contribution · Progress |
| · History · Templates                                  |
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
| Assign a projected date | Change dropdown  | `hx-post` (fields `date`, `assignee`) | `#planned-<chore>-<YYYY-MM-DD>` (`_chore_card.html` with `card_only`); on a member-filter mismatch `HX-Retarget: #dashboard-list` + `_dashboard_list.html` |
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
