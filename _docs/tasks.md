# Backlog — Household Chore Manager

## 1. Project skeleton with a passing test
Goal: Confirm the Django project runs and has a working test suite.
Description: Create the `chores` Django app, register it in settings, wire up
a root URL to a placeholder view that returns 200. Add a smoke test that hits
`/` and asserts a 200 response. Run `manage.py test` and make sure it passes.

## 2. Member model and family management screen
Goal: Let users add, edit, and remove household members.
Description: Create the `Member` model (name, role, avatar, order) with a
migration. Build the `/family/` screen with HTMX-powered views for listing,
adding, editing, and deleting members. When a member is deleted, any
`ChoreOccurrence` assigned to them should become unassigned. Write tests for
each CRUD operation and the unassignment cascade.

## 3. Profile switcher
Goal: Let users select the active family member from any screen.
Description: Add a profile switcher to `base.html` that shows member avatars
and names. Store the selected member in the Django session. The switcher should
be accessible from every page via the header. Write a test that verifies
switching profiles updates the session.

## 4. Chore model and create/edit form
Goal: Let users create and edit chores with all fields from the spec.
Description: Create the `Chore` model (title, instructions, assignment_mode,
eligibility, assigned_to, recurrence, recurrence_day, points, is_template,
template_source) with a migration. Build the create and edit forms at
`/chores/create/` and `/chores/<id>/edit/`. The form should show/hide fields
dynamically based on assignment_mode (eligibility for rotating, assigned_to for
fixed). Write tests for creating each type of chore.

## 5. Chore deletion with recurrence scope
Goal: Let users delete a single occurrence or a chore and all future occurrences.
Description: Build the delete flow at `/chores/<id>/delete/` that asks whether
to delete this occurrence only or this and all future occurrences. Deleting a
single occurrence removes or marks the `ChoreOccurrence`; deleting future
occurrences removes the `Chore` record itself. Write tests for both paths.

## 6. Chore templates
Goal: Let users create chores from built-in or custom templates.
Description: Add a fixture with built-in chore templates (stored as `Chore`
records with `is_template=True`). In the chore creation flow, show a template
picker that pre-fills the form. Users can also save a chore as a custom
template. Write tests for loading a built-in template and creating a custom one.

## 7. Rotation logic
Goal: Implement fair chore assignment by cycling through eligible members.
Description: Create the `RotationState` model (chore, last_member). Write a
service function that, given a rotating chore, returns the next eligible member
based on role eligibility and `Member.order`, then updates the state. Rotation
advances on every scheduled occurrence including skipped ones. Handle
wrap-around, empty eligible pool, and fixed/unassigned modes. Write unit tests
for all edge cases.

## 8. Occurrence projection service
Goal: Project future recurring chore occurrences without writing to the DB.
Description: Write a service function that, given a date range, returns
projected `ChoreOccurrence` data for all recurring chores. For rotating chores,
simulate rotation state forward from the last materialized occurrence. For
fixed chores, always use the assigned member. Return lightweight dicts (not
saved model instances). Write tests for daily, weekly, and monthly recurrence
projection.

## 9. Occurrence materialization
Goal: Turn projected occurrences into real DB records for the current week.
Description: Write a service function that materializes occurrences for a given
week by creating `ChoreOccurrence` records. Skip creation for dates that
already have a record (preserving manual edits). Update `RotationState` when
materializing rotating chores. Write tests for first-time materialization,
idempotency, and mixed chore types.

## 10. Weekly dashboard — basic view
Goal: Display the current week's chores grouped by Overdue, Today, Upcoming.
Description: Build the `/dashboard/` view that materializes the current week's
occurrences, then queries and groups them into three sections. Each chore card
shows title, assigned member (or "unassigned"), date, points, and status. Add
week navigation (previous/next) via HTMX. Write tests for correct grouping and
navigation.

## 11. Dashboard filters
Goal: Let users filter the dashboard by member and day.
Description: Add dropdown filters to the dashboard for family member and day of
week. Filters update the chore list via HTMX without a full page reload. Write
tests that verify filtering returns the correct subset of occurrences.

## 12. Mark done, skip, and undo
Goal: Let users change chore status from the dashboard.
Description: Add Done, Skip, and Undo buttons to each chore card. POST to
`/dashboard/toggle/<id>/` updates the `ChoreOccurrence` status, sets/clears
`done_at`, and awards/removes points. Return the updated `_chore_card.html`
partial. Write tests for each transition: todo->done, todo->skipped,
done->todo (undo), and that points are correctly adjusted.

## 13. Assign and reassign from dashboard
Goal: Let users assign an unassigned chore or change the assignee.
Description: Add an assign/reassign dropdown on each chore card. POST to
`/dashboard/assign/<id>/` updates the `ChoreOccurrence.member` without
affecting `RotationState`. Write tests for assigning an unassigned chore and
reassigning an already-assigned one.

## 14. Monthly calendar view
Goal: Show chores on a monthly calendar grid.
Description: Build the `/calendar/` view that renders a month grid. Each date
cell shows the count of chores and small icons/dots. Month navigation
(previous/next) via HTMX. Clicking a date links to `/chores/create/?date=...`.
Use the projection service for future dates. Write tests for correct date
rendering and that clicking a date pre-fills the form.

## 15. Points and contribution view
Goal: Show family contribution summaries for the week and month.
Description: Build the `/contribution/` view that aggregates points from
completed `ChoreOccurrence` records, grouped by member. Support a `period`
query param (`week` or `month`). Display as a simple bar or table — no
ranked leaderboard. Write tests for correct point aggregation across both
periods.

## 16. Streaks and badges
Goal: Track per-chore completion streaks and award milestone badges.
Description: Create the `Badge` model (member, badge_type, awarded_at). Write
a service that calculates per-chore streaks from consecutive completed
occurrences (skipping breaks the streak). Check for badge thresholds (7-day
streak, 50 completed, etc.) after each status change and create `Badge` records.
Display badges on the contribution view. Write tests for streak calculation,
badge awarding, and streak-breaking on skip.

## 17. History view
Goal: Let users browse completed and skipped chores.
Description: Build the `/history/` view showing past `ChoreOccurrence` records
with status done or skipped. Display chore title, member, date, status, and
points. Support basic filtering by member. Write tests for correct history
rendering and filtering.

## 18. Base layout and responsive styling
Goal: Wire all screens together with Pico CSS and responsive design.
Description: Finalize `base.html` with Pico CSS (via CDN), HTMX, the profile
switcher, and navigation links to all screens. Add custom CSS for the dashboard
chore groups, calendar grid, and chore cards. Ensure the layout works on both
desktop and mobile widths. No new backend logic — this task polishes the UI
across all existing screens.

## 19. One-off / ad-hoc chores
Goal: Let users quickly add a one-time chore from the dashboard.
Description: Add an "Add Chore" button on the dashboard that links to the chore
creation form with the date pre-filled and recurrence defaulting to `once`. The
resulting `ChoreOccurrence` appears on the dashboard for its assigned date.
Write tests for creating a one-off and verifying it shows on the correct day.
