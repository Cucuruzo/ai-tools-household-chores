# Household Chore Manager — MVP Specification

## 1. Goal

Build a simple web app for one household with adults and kids to:

- plan recurring and one-off chores
- assign chores fairly
- track what is done, overdue, or skipped
- make household contribution visible with lightweight points and streaks

The main focus is **weekly planning and completion tracking**. Gamification should stay simple.

---

## 2. Users

The app supports one household.

Each family member has:

- name
- role: `Adult` or `Kid`
- avatar/icon

There is no authentication in the MVP.

A simple profile switcher selects the active family member.

Family members can be added, edited, and removed.

If a member is removed, their active chores become unassigned.

---

## 3. Chores

Each chore has:

- title
- optional short instructions
- due date — required
- due time — optional
- assignment mode
- recurrence
- point value
- status

### Assignment modes

A chore can be:

- **Fixed** — assigned to one person
- **Rotating** — automatically rotates among eligible family members
- **Unassigned** — planned, but not yet assigned to anyone

For a **fixed** chore, eligibility is not shown; the selected assignee is the only relevant person.

A fixed or one-off chore may be created without an assignee and remain **Unassigned** until someone assigns it later.

For a **rotating** chore, choose an eligibility group:

- `Kids only`
- `Adults only`
- `Everyone`

If a rotating chore has no eligible family members, it becomes **Unassigned / Needs eligible member** until an eligible member exists.

A planned occurrence may also be manually changed to **Unassigned** without deleting the chore or recurring rule. It remains visible on the dashboard until it is assigned, completed, skipped, or otherwise changed.

For the MVP, each chore occurrence has **zero or one assignee**.

### Rotation fairness rule

For rotating chores, the rotation **advances after every scheduled occurrence**, including when that occurrence is skipped.

This prevents one person from stalling the rotation or permanently avoiding later turns.

---

## 4. Recurrence

Supported chore types:

- one-off / ad hoc
- daily
- weekly
- monthly

Recurring chores keep past completion history.

Future recurring assignments are **projected**, not permanently stored in advance. Rotation should be calculated from the recurring chore's rotation state and current eligible members.

When the relevant week becomes current, the occurrence can be materialized with its concrete assignee. Simply viewing a future week should not mutate data.

The app should show projected recurring occurrences for the **current month**.

When editing or deleting a recurring chore, ask whether the change applies to:

- this occurrence only
- this and future occurrences

---

## 5. Weekly Planning

The main planning experience is the **weekly dashboard**.

Recurring chores automatically populate the week based on their recurrence rules.

The dashboard defaults to **This Week** and groups chores into:

- Overdue
- Today
- Upcoming

Filters:

- family member
- day
- week
- month

Users can mark a chore **Done** directly from the dashboard.

---

## 6. Monthly Calendar

The app includes a **monthly calendar**.

It shows chores by due date.

Clicking a date starts creating a new chore for that date.

---

## 7. Chore Statuses

Stored statuses:

- `To do`
- `Done`
- `Skipped`

**Overdue is derived, not stored.** A chore is overdue when its due date/time has passed and its stored status is still `To do`.

Rules:

- overdue chores stay visible until completed or skipped
- overdue chores can still be completed later
- late completion still gives full points
- skipped chores give no points
- skipped chores remain in history

A completed chore can be undone.

Undoing completion removes the awarded points and restores the previous progress state.

---

## 8. Points and Progress

Points are lightweight and represent chore effort:

- `5 points` — small chore
- `10 points` — medium chore
- `20 points` — bigger chore

Kids and adults use the same scale.

The app should show a **family contribution view** for:

- this week
- this month

The purpose is visibility, not competition.

Points are a **record of contribution and effort**, not the fairness mechanism. Rotation handles fairness, so point totals may be uneven in a given week.

Avoid winner/loser language or ranked competition.

---

## 9. Streaks and Badges

Keep streaks simple and secondary.

Track only a **simple per-chore streak**.

Skipping an occurrence ends that chore's current streak.

Badges can be simple visual milestones based on:

- streaks
- total chores completed

Examples:

- `7-Day Streak`
- `Chore Hero`
- `50 Chores Completed`

No complex reward economy is needed.

---

## 10. Templates

When creating a chore, users can start from:

- built-in chore templates
- custom family templates

Example built-in templates:

- Take out the trash
- Load the dishwasher
- Clean the bathroom
- Change bed sheets

Templates may prefill:

- title
- recurrence
- eligibility
- assignee / rotation mode
- point value

Users can edit the values before saving.

Custom templates are shared with the household.

A separate Templates screen is not required for the MVP; templates can appear inside the chore creation flow.

---

## 11. History

Keep history simple.

Users should be able to view:

- completed chores
- skipped chores

History may be a lightweight view or filter rather than a large standalone feature.

---

## 12. Permissions

Any family member can create chores.

The UI may treat adults as allowed to edit or delete any chore, but this is an **honor-system convention**, not a security boundary.

Because there is no authentication and users can switch profiles freely, the MVP should not implement security logic around adult-only actions.

---

## 13. Technical Scope

The MVP should be:

- a responsive web app
- accessible on the local home network
- backed by a simple shared database
- persistent between sessions
- designed for one household
- usable comfortably on desktop and mobile

Real-time multi-user synchronization is not required.

---

## 14. Explicitly Out of Scope

Do not add:

- real authentication
- multiple households
- chore tags, rooms, or other organizational grouping
- chore priorities
- search
- photos or attachments
- real push/email notifications
- competitive leaderboard
- complex reward systems
- chore swapping
- multi-person chore completion
- real-time collaboration

---

## 15. Core MVP Screens

1. **Weekly Dashboard**
2. **Monthly Calendar**
3. **Create / Edit Chore**
4. **Family Management**
5. **Contribution / Progress View**
6. **Simple History View**

---

## 16. Core User Flows

### Create a chore

1. Select active family member
2. Click **Add Chore** or click a calendar date
3. Enter title and optional instructions
4. Set due date and optional time
5. Choose assignment mode:
   - fixed person
   - rotating among eligible members
   - unassigned
6. If rotating, choose eligibility
7. Choose recurrence
8. Choose 5 / 10 / 20 points
9. Save

### Complete a chore

1. Find it on the weekly dashboard
2. Click **Done**
3. Update status
4. Award points
5. Update simple streak/progress data

### Skip a chore

1. Mark the chore **Skipped**
2. Award no points
3. Keep it visible in history

---

## 17. MVP Success Criteria

The MVP is complete when a family can:

- manage family members
- create one-off and recurring chores
- leave a chore unassigned and assign it later
- remove an assignee from a planned occurrence without deleting the chore
- assign recurring chores to a fixed person or rotate them fairly
- see the week's chores at a glance
- see chores in a monthly calendar
- mark chores done or skipped, undo completion, and see overdue chores derived automatically
- keep completion history
- earn simple 5 / 10 / 20 point values
- see lightweight streaks, badges, and family contribution
- use built-in/custom chore templates during creation
- keep data after restarting the app
- access the app from devices on the local network
