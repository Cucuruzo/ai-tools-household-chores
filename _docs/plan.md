# Household Chores Tool — Specification

## Overview

A single-screen web app for a household of adults and kids to distribute
shared chores fairly and track completion. Fairness is handled by simple
rotation (no scoring). The app supports both recurring chores, planned on a
weekly template, and ad-hoc one-offs.

## Goals

- Distribute chores fairly across household members.
- Track what got done, by whom, and when.
- Keep planning low-effort week to week.

## Users

- One household containing **adults** and **kids**.
- Every member is tagged as either an adult or a kid.

## Core Concepts

### Members

- Each member has a name and a type (`adult` or `kid`).
- The family list is fully manageable: add, edit, remove members.

### Chores

- Each chore has a name and an **eligibility category** that determines who
  can be assigned it:
  - **Kids-only** — rotates among the kids.
  - **Adults-only** — rotates among the adults.
  - **Specific person** — fixed to one named member (no rotation).
- Chores are fully manageable: add, edit, remove.
- A chore is either **recurring** (appears on the weekly plan) or a
  **one-off** (added ad-hoc as needed).

### Fairness — Simple Rotation

- No points, no effort-weighting, no scoring.
- Each recurring chore cycles through its eligible members **in order**.
- Next time the chore comes up, the next eligible member is assigned.
- Specific-person chores don't rotate — they're always the same person.

## Features

### Weekly Planner

- A **recurring template** defines which chores land on which days of the week.
- The template auto-fills each new week by default.
- Any single week can be **edited without changing the template**
  (e.g. skip, move, or reassign a chore just for this week).
- The whole week is visible at a glance, with rotation filling in who's up.

### One-Offs

- Added ad-hoc and shown alongside the week's chores.
- (Open question — see below.)

### Tracking

- Mark a chore **done**.
- Log **who** completed it and **when**.
- Maintain **streaks and history**.

### Management

- **Manage chores:** add / edit / remove, set category and recurrence.
- **Manage family:** add / edit / remove members, set adult/kid type.

## Form Factor

- Single-screen web app.

## Open Questions

These are small and can be decided during the build:

1. **Streaks** — counted per-person, per-chore, or household-wide?
2. **One-offs** — do they join the rotation, or get manually assigned when added?

## Out of Scope (for now)

- Scoring, points, or effort-weighting.
- Reminders / notifications.
- Multiple households or accounts.
- Mobile-native app or shared-display mode.
