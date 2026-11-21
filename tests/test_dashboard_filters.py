"""Dashboard member and day filters (#11)."""
import html
import re
from urllib.parse import quote

import pytest
from django.test import Client

from chores import dashboard as dash
from chores.models import Chore, ChoreOccurrence, Member
from tests.test_dashboard import (
    WED, cards, chore, get, main, member, no_writes, occ, page, sections, snapshot, spy,
)

HTMX = {"HTTP_HX_REQUEST": "true"}
HOSTILE = "<script>alert(1)</script>"
INVALID_MEMBERS = ["", "abc", "0", "-3", "3.0", " 3", "٣", "99999999999999999999",
                   "Unassigned", "UNASSIGNED", "x" * 10000, HOSTILE, "3\x00"]
INVALID_DAYS = ["", "abc", "0", "8", "12", "mon", "-1", "1.0", " 1", "١", "x" * 10000, HOSTILE, "1\x00"]


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def family(db):
    # Mom is pk 1, Emma pk 2 (order 0 and 1).
    return member("Mom", role="adult", order=0), member("Emma", order=1)


@pytest.fixture
def week(family):
    """A current week (Mon Nov 16 - Sun Nov 22, today Wed) with a bit of everything."""
    mom, emma = family
    occ("Old", "2026-11-02", member=mom)
    occ("MonMom", "2026-11-16", member=mom)
    occ("MonDone", "2026-11-16", status="done", member=emma)
    occ("TodayEmma", "2026-11-18", member=emma)
    occ("TodayNobody", "2026-11-18")
    occ("FriMom", "2026-11-20", member=mom)
    return family


def titles(body_or_sections):
    return {k: [c["title"] for c in v] for k, v in body_or_sections.items()}


def shown(client, **kwargs):
    return titles(sections(page(client, **kwargs)))


def fil(client, member=None, day=None, week=None, **kwargs):
    data = {k: v for k, v in (("member", member), ("day", day)) if v is not None}
    return page(client, week, data=data, **kwargs)


def hrefs(body):
    return [html.unescape(v) for v in re.findall(r'(?:href|hx-get|value)="(/dashboard/[^"]*)"', body)]


def nav_link(body, label):
    m = re.search(rf'<a href="([^"]*)" aria-label="{label}"\s+hx-get="([^"]*)"', body)
    return tuple(html.unescape(g) for g in m.groups())


# --- Parsing ---------------------------------------------------------------------


def test_parse_day():
    assert [dash.parse_day_filter(str(i)) for i in range(1, 8)] == list(range(1, 8))
    assert all(dash.parse_day_filter(v) is None for v in INVALID_DAYS)


def test_dashboard_query_order_and_defaults():
    assert dash.dashboard_query() == ""
    assert dash.dashboard_query("2026-W48", 3, 2) == "?week=2026-W48&member=3&day=2"
    assert dash.dashboard_query(None, "unassigned") == "?member=unassigned"


# --- What the filters show ---------------------------------------------------------


@pytest.mark.django_db
def test_no_filter_baseline(client, week):
    s = shown(client)
    assert s["overdue"] == ["Old", "MonMom"]
    assert s["today"] == ["TodayEmma", "TodayNobody"]
    assert s["upcoming"] == ["FriMom"]
    assert s["done-earlier"] == ["MonDone"]


@pytest.mark.django_db
def test_member_filter_keeps_old_overdue(client, week):
    mom, emma = week
    s = titles(sections(fil(client, member=mom.pk)))
    assert s["overdue"] == ["Old", "MonMom"]
    assert s["today"] == []
    assert s["upcoming"] == ["FriMom"]
    assert "done-earlier" not in s
    s = titles(sections(fil(client, member=emma.pk)))
    assert (s["overdue"], s["today"], s["upcoming"], s["done-earlier"]) == (
        [], ["TodayEmma"], [], ["MonDone"])


@pytest.mark.django_db
def test_unassigned_filter(client, week):
    s = titles(sections(fil(client, member="unassigned")))
    assert s["today"] == ["TodayNobody"]
    assert s["overdue"] == [] and s["upcoming"] == []


@pytest.mark.django_db
def test_deleted_member_rows_are_unassigned(client, week):
    mom, _ = week
    pk = mom.pk
    mom.delete()
    assert shown(client, data={"member": "unassigned"})["overdue"] == ["Old", "MonMom"]
    body = fil(client, member=pk)  # unknown pk: Everyone
    assert '<option value="all" selected>Everyone</option>' in body
    assert titles(sections(body))["upcoming"] == ["FriMom"]


@pytest.mark.django_db
def test_unassigned_includes_needs_eligible_card(client, family):
    occ("Kids job", "2026-11-19", mode="rotating", eligibility="adults")
    Member.objects.filter(role="adult").delete()
    occ("Mine", "2026-11-19", member=Member.objects.get())
    [card] = sections(fil(client, member="unassigned"))["upcoming"]
    assert card["assignee"] == "Unassigned · needs eligible member"


@pytest.mark.django_db
def test_leading_zero_pk_is_normalized(client, week):
    mom, _ = week
    body = fil(client, member=f"0{mom.pk}")
    assert f'<option value="{mom.pk}" selected>' in body
    assert "member=03" not in body and "member=01" not in body
    assert f"member={mom.pk}" in " ".join(hrefs(body))


@pytest.mark.django_db
def test_day_filter_hides_earlier_week_overdue(client, week):
    s = titles(sections(fil(client, day="1")))
    assert s["overdue"] == ["MonMom"]
    assert s["today"] == [] and s["upcoming"] == []
    assert s["done-earlier"] == ["MonDone"]


@pytest.mark.django_db
def test_day_today_and_passed_due_time(client, week):
    mom, _ = week
    occ("Early", "2026-11-18", due="08:00", member=mom)  # passed at 12:00
    s = titles(sections(fil(client, day="3")))
    assert s["overdue"] == ["Early"]
    assert s["today"] == ["TodayEmma", "TodayNobody"]


@pytest.mark.django_db
def test_both_filters_and(client, week):
    mom, _ = week
    s = titles(sections(fil(client, member=mom.pk, day="5")))
    assert s["upcoming"] == ["FriMom"]
    assert s["overdue"] == [] and s["today"] == []
    s = titles(sections(fil(client, member=mom.pk, day="3")))
    assert (s["overdue"], s["today"], s["upcoming"]) == ([], [], [])


@pytest.mark.django_db
def test_nothing_here_in_each_section_and_done_earlier_hidden(client, week):
    body = fil(client, day="7")
    assert body.count('class="nothing-here">Nothing here') == 3
    assert "section-done-earlier" not in body
    assert 'id="no-chores"' not in body


@pytest.mark.django_db
def test_no_chores_this_week_uses_unfiltered_week(client, family):
    mom, _ = family
    body = fil(client, member=mom.pk)
    assert "No chores this week" in body and "Add a chore" in body
    assert 'id="dashboard-filters"' in body
    occ("Other", "2026-11-18")
    body = fil(client, member=mom.pk)
    assert "No chores this week" not in body
    assert body.count("Nothing here") == 3


@pytest.mark.django_db
@pytest.mark.parametrize("wk", ["2026-W46", "2026-W48"])
def test_past_and_future_week_filters(client, week, wk):
    mom, emma = week
    occ("PastMom", "2026-11-10", member=mom)
    occ("PastEmma", "2026-11-10", member=emma)
    occ("FutureMom", "2026-11-25", member=mom)
    occ("FutureNobody", "2026-11-25")
    chore("Weekly", recurrence="weekly", recurrence_day=2, start_date="2026-11-01")
    own = "PastMom" if wk == "2026-W46" else "FutureMom"
    before = snapshot()
    spy_, ctx = no_writes()
    with ctx:
        mine = [c["title"] for c in cards(main(fil(client, member=mom.pk, week=wk)))]
        none = [c["title"] for c in cards(main(fil(client, member="unassigned", week=wk)))]
    assert spy_.writes == [] and snapshot() == before
    assert mine == [own]
    assert "Weekly" not in mine
    assert ("Weekly" in none) == (wk == "2026-W48")
    body = fil(client, member=mom.pk, day="7", week=wk)
    assert 'class="nothing-here">Nothing here' in body and "No chores this week" not in body


@pytest.mark.django_db
def test_future_week_projected_filter_by_day(client, family):
    mom, _ = family
    chore("Weekly", recurrence="weekly", recurrence_day=2, start_date="2026-11-01")
    got = cards(main(fil(client, day="3", week="2026-W48")))
    assert [c["title"] for c in got] == ["Weekly"]
    assert not cards(main(fil(client, day="4", week="2026-W48")))


@pytest.mark.django_db
def test_materialization_unchanged_by_filters(client, family):
    chore("Daily", recurrence="daily", start_date="2026-11-01")
    with spy() as sp:
        get(client, data={"member": "1", "day": "1"})
    sp.assert_called_once()
    assert sp.call_args.args[0].isoformat() == "2026-11-16"
    filtered = ChoreOccurrence.objects.count()
    ChoreOccurrence.objects.all().delete()
    get(client)
    assert ChoreOccurrence.objects.count() == filtered > 0


@pytest.mark.django_db
def test_invalid_week_fallback_keeps_filters(client, week):
    s = titles(sections(fil(client, member=week[0].pk, week="garbage")))
    assert s["upcoming"] == ["FriMom"]


# --- Invalid values --------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("value", INVALID_MEMBERS)
def test_invalid_member_is_everyone(client, week, value):
    base = page(client)
    body = fil(client, member=value)
    assert '<option value="all" selected>Everyone</option>' in body
    assert main(body) == main(base)
    if len(value) > 3 and value.lower() != "unassigned":  # that word is a real option label
        for needle in (value, quote(value, safe=""), html.escape(value)):
            assert needle not in body


@pytest.mark.django_db
@pytest.mark.parametrize("value", INVALID_DAYS)
def test_invalid_day_is_all_days(client, week, value):
    base = page(client)
    body = fil(client, day=value)
    assert main(body) == main(base)
    assert 'name="day"' in body and "day=" not in " ".join(hrefs(body))
    if len(value) > 3:
        assert value not in body and quote(value, safe="") not in body


@pytest.mark.django_db
@pytest.mark.parametrize("param", ["member", "day"])
def test_hostile_value_never_echoed(client, week, param):
    body = page(client, "2026-W48", data={param: HOSTILE, "other": HOSTILE})
    lowered = body.lower()
    for needle in ("alert(1)", "&lt;script", "%3cscript", "<script>alert"):
        assert needle not in lowered
    assert re.search(r'name="next" value="/dashboard/\?week=2026-W48"', body)


@pytest.mark.django_db
def test_repeated_params_last_wins(client, week):
    mom, emma = week
    r = client.get("/dashboard/", {"member": [mom.pk, emma.pk]})
    assert f'<option value="{emma.pk}" selected>' in r.content.decode()
    with_bad = client.get("/dashboard/", {"member": [emma.pk, "abc"]}).content.decode()
    assert '<option value="all" selected>Everyone</option>' in with_bad
    days = client.get("/dashboard/", {"day": ["2", "9"]}).content.decode()
    assert 'value="" selected' in days and 'name="day"' in days
    assert "day=" not in " ".join(hrefs(days))


@pytest.mark.django_db
def test_unrelated_params_ignored(client, week):
    assert main(fil(client, week=None)) == main(page(client, data={"x": "1", "y": ["2", "3"]}))


# --- Form ----------------------------------------------------------------------


def form(body):
    return re.search(r'<form id="dashboard-filters".*?</form>', body, re.S).group(0)


def options(select_html):
    return re.findall(r'<option value="([^"]*)"( selected)?>(.*?)</option>', select_html)


def select(body, name):
    return re.search(rf'<select id="[^"]*" name="{name}">(.*?)</select>', form(body), re.S).group(1)


@pytest.mark.django_db
def test_form_attributes_and_labels(client, week):
    f = form(page(client))
    assert 'method="get" action="/dashboard/"' in f
    assert 'hx-get="/dashboard/"' in f and 'hx-trigger="change"' in f
    assert 'hx-target="#dashboard-list"' in f and 'hx-push-url="true"' in f
    assert '<label for="filter-member">' in f and '<label for="filter-day">' in f
    assert 'type="hidden" name="week"' not in f


@pytest.mark.django_db
def test_member_options_in_member_order(client):
    member("Zed", order=0)
    member("Amy", order=1)
    member("Bob", order=2)
    opts = options(select(page(client), "member"))
    assert [o[2] for o in opts] == ["Everyone", "Zed", "Amy", "Bob", "Unassigned"]
    assert opts[0][0] == "all" and opts[-1][0] == "unassigned"
    assert [o[1] for o in opts] == [" selected", "", "", "", ""]


@pytest.mark.django_db
def test_no_members_options(client):
    assert [o[2] for o in options(select(page(client), "member"))] == ["Everyone", "Unassigned"]


@pytest.mark.django_db
def test_member_names_escaped(client):
    member("<b>Bo</b>")
    body = page(client)
    assert "&lt;b&gt;Bo&lt;/b&gt;" in select(body, "member") and "<b>Bo</b>" not in body


@pytest.mark.django_db
def test_day_options_and_labels(client):
    opts = options(select(page(client), "day"))
    assert [(o[0], o[2]) for o in opts] == [
        ("", "All days"), ("1", "Mon, Nov 16"), ("2", "Tue, Nov 17"), ("3", "Wed, Nov 18"),
        ("4", "Thu, Nov 19"), ("5", "Fri, Nov 20"), ("6", "Sat, Nov 21"), ("7", "Sun, Nov 22")]


@pytest.mark.django_db
def test_day_labels_on_w53(client):
    opts = options(select(page(client, "2026-W53"), "day"))
    assert opts[1][2] == "Mon, Dec 28" and opts[-1][2] == "Sun, Jan 3"


@pytest.mark.django_db
def test_preselected(client, week):
    mom, _ = week
    body = fil(client, member=mom.pk, day="4")
    assert [o[0] for o in options(select(body, "member")) if o[1]] == [str(mom.pk)]
    assert [o[0] for o in options(select(body, "day")) if o[1]] == ["4"]
    body = fil(client, member="unassigned")
    assert [o[0] for o in options(select(body, "member")) if o[1]] == ["unassigned"]


@pytest.mark.django_db
def test_hidden_week_input_only_off_current_week(client, week):
    assert 'name="week" value="2026-W48"' in form(fil(client, week="2026-W48"))
    assert 'name="week"' not in form(fil(client, week="2026-W47"))
    assert 'name="week"' not in form(fil(client, week="nonsense"))


@pytest.mark.django_db
@pytest.mark.parametrize("wk", [None, "2026-W46", "2026-W48", "2026-W53"])
def test_form_on_every_view_even_when_empty(client, wk):
    body = page(client, wk)
    assert 'id="dashboard-filters"' in body
    assert 'name="member"' in body and 'name="day"' in body


@pytest.mark.django_db
def test_noscript_apply_button(client):
    f = form(page(client))
    assert re.search(r"<noscript>\s*<button type=\"submit\">Apply</button>\s*</noscript>", f)


# --- Links -------------------------------------------------------------------------


@pytest.mark.django_db
def test_prev_next_keep_filters(client, week):
    body = page(client, "2026-W47", data={"member": "1", "day": "2"})
    assert nav_link(body, "Previous week") == (
        "/dashboard/?week=2026-W46&member=1&day=2",) * 2
    assert nav_link(body, "Next week") == ("/dashboard/?week=2026-W48&member=1&day=2",) * 2


@pytest.mark.django_db
def test_year_rollover_keeps_filters(client, week):
    body = page(client, "2026-W53", data={"member": "unassigned", "day": "7"})
    assert nav_link(body, "Next week")[0] == "/dashboard/?week=2027-W01&member=unassigned&day=7"
    body = page(client, "2027-W01", data={"member": "unassigned", "day": "7"})
    assert nav_link(body, "Previous week")[0] == "/dashboard/?week=2026-W53&member=unassigned&day=7"


@pytest.mark.django_db
def test_this_week_keeps_filters(client, week):
    body = page(client, "2026-W48", data={"member": "1", "day": "2"})
    assert re.search(r'id="this-week" href="/dashboard/\?member=1&amp;day=2"\s+hx-get="/dashboard/\?member=1&amp;day=2"', body)
    body = page(client, "2026-W48")
    assert re.search(r'id="this-week" href="/dashboard/"\s+hx-get="/dashboard/"', body)


@pytest.mark.django_db
def test_invalid_values_not_carried_into_links(client, week):
    body = page(client, "2026-W48", data={"member": "abc", "day": "2"})
    assert nav_link(body, "Next week") == ("/dashboard/?week=2026-W49&day=2",) * 2
    assert 'id="this-week" href="/dashboard/?day=2"' in body
    body = page(client, "2026-W48", data={"member": "999", "day": "9"})
    assert nav_link(body, "Next week")[0] == "/dashboard/?week=2026-W49"


# --- HTMX / full page ----------------------------------------------------------------


@pytest.mark.django_db
def test_partial_full_and_history_restore(client, week):
    data = {"member": week[0].pk, "day": "1"}
    full = page(client, data=data)
    partial = page(client, data=data, **HTMX)
    restore = page(client, data=data, **HTMX, HTTP_HX_HISTORY_RESTORE_REQUEST="true")
    assert "<html" in full and "<html" in restore
    assert "<html" not in partial
    assert partial.count('<nav id="profile-switcher" hx-swap-oob="true"') == 1
    assert partial.rindex("</div>") < partial.index('id="profile-switcher"')
    assert 'id="dashboard-filters"' in partial and 'id="week-title"' in partial
    assert 'aria-label="Next week"' in partial
    expected = [("MonMom", "overdue")]
    for body in (full, partial, restore):
        s = titles(sections(body))
        assert [(t, k) for k, v in s.items() for t in v if k != "done-earlier"] == expected
        assert [o[0] for o in options(select(body, "day")) if o[1]] == ["1"]


# --- Switcher next ---------------------------------------------------------------------


def next_values(body):
    return re.findall(r'name="next" value="([^"]*)"', body)


@pytest.mark.django_db
def test_switcher_next_is_canonical(client, week):
    body = page(client, "2026-W48", data={"member": "1", "day": "2", "x": "1"})
    values = next_values(body)
    assert len(values) == 2 and set(values) == {"/dashboard/?week=2026-W48&amp;member=1&amp;day=2"}


@pytest.mark.django_db
def test_switcher_next_current_week_and_hostile(client, week):
    assert set(next_values(page(client, data={"member": "unassigned"}))) == {
        "/dashboard/?member=unassigned"}
    assert set(next_values(page(client, "2026-W47", data={"day": "3"}))) == {"/dashboard/?day=3"}
    body = page(client, data={"member": HOSTILE, "day": HOSTILE, "z": "1"})
    assert set(next_values(body)) == {"/dashboard/"}


@pytest.mark.django_db
def test_switching_profile_returns_to_filtered_view(client, week):
    body = page(client, "2026-W48", data={"member": "1"})
    nxt = re.search(r'name="next" value="([^"]*)"', body).group(1).replace("&amp;", "&")
    r = client.post("/profile/1/", {"next": nxt})
    assert r.status_code == 302 and r["Location"] == "/dashboard/?week=2026-W48&member=1"
