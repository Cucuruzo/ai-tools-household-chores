# Design system

Pico CSS 2.0.6 plus one stylesheet, `chores/static/chores/style.css`, linked right after Pico in `base.html`.

**Rule:** new styles go in `style.css` with Pico variables. Do not add new per-page `<style>` blocks, and do not change card or HTMX markup for styling.

## Tokens (`:root` in `style.css`)

Colour comes only from Pico variables (no hex, `rgb()` or `hsl()`), so light and dark mode both work.

| Token | Value |
|---|---|
| `--chores-gap` | 0.5rem (gap between controls) |
| `--chores-block-gap` | 1.5rem (between card blocks) |
| `--chores-touch` / `--chores-touch-small` | 44px / 24px |
| `--chores-muted` | `--pico-muted-color` |
| `--chores-muted-border` | `--pico-muted-border-color` |
| `--chores-accent-overdue` | `--pico-del-color` |
| `--chores-accent-today` | `--pico-primary` |
| `--chores-accent-upcoming` | `--pico-muted-border-color` |
| `--chores-accent-done` | `--pico-ins-color` |
| `--chores-accent-skipped` | `--pico-muted-color` |
| `--chores-skipped-background` | `--pico-card-sectioning-background-color` |
| `--chores-current` | `--pico-primary` |

## Touch targets

Every visible button, `[role="button"]` (including `summary` and `a`), select and text/number/date/time input is at least 44px tall, and so are the site nav links, the week and month arrows, the period toggle and the history pager links. Calendar cell links (`.day-number`, `.add-chore`) are at least 24px (WCAG 2.5.8). Checkboxes are exempt: their label is the target.

## Header and nav

Every full page: `<header class="container">` holds the clock, then `#profile-switcher`, then `<nav id="site-nav" aria-label="Main">`. The nav comes after the switcher. Links, in order: Dashboard, Calendar, Family, Contribution, Progress, History, Templates. `aria-current="page"` follows `href` and is chosen by `request.resolver_match.url_name` (Family covers `member_*`, Templates covers `template_*`; create and edit pages mark nothing). The current link is bold, `--pico-primary`, with a 3px underline bar. The nav has no `hx-*` attributes and is not in HTMX partials. At 375px it wraps to at most 2 rows; from 768px it is one row.

## Sections (dashboard)

Overdue: heading and 4px left border in `--pico-del-color`. Today: 4px `--pico-primary`. Upcoming and non-current weeks: 4px `--pico-muted-border-color`. Headings always stay visible.

## Card status styles

| Status | Style |
|---|---|
| To do | section accent border |
| Overdue | 4px `--pico-del-color` border (section) |
| Planned | dashed left border (`[id^="planned-"]`) |
| Done | 4px `--pico-ins-color` border, opacity 0.75 (`:has(.credit)`) |
| Skipped | 4px `--pico-muted-color` border, sectioning background, opacity 0.75 (`:has(.title s)` without `.credit`) |

The text status stays visible on every card. `.streak` and `.instructions` are muted. Move and Delete `<details>` use wrapping rows with 44px controls.

## Calendar

Dot colours live in `calendar.html`: to do `--pico-primary`, overdue `--pico-del-color`, done `--pico-ins-color`, skipped `--pico-muted-color`, planned transparent with a 2px `--pico-primary` ring. `#calendar-legend` sits after `#calendar-grid` (never inside it), shown from 576px and hidden below. From 768px the cells are roughly square.

## Empty states

`.empty-state`, `#no-chores`, `#no-members`, `#no-streaks`, `#no-members-badges`: centred muted text, 2px dashed `--pico-muted-border-color` border, Pico radius, 1.25rem 1rem padding. `.nothing-here` is muted italic text with no box.

## Breakpoints

Pico's: 576 / 768 / 1024 / 1280 / 1536.
