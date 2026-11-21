"""Dashboard filters survive the chore edit page (#27)."""
import html
import re

import pytest
from django.contrib.messages import get_messages
from django.test import Client

from chores.models import Chore, Member
from tests.test_chores import payload
from tests.test_dashboard import WED, at, chore, d, member, occ, page
from tests.test_delete import HOSTILE, hidden, weekly
from tests.test_toggle import HTMX

HREF = re.compile(r'<p class="title">\s*<strong><a href="([^"]*)"')
BACK = re.compile(r'<a id="back-to-dashboard" href="([^"]*)">Back to dashboard</a>')
FORM = re.compile(r'<form id="chore-form"[^>]*>(.*?)</form>', re.S)
DELETE = re.compile(r'<details class="delete-confirm">.*?<form[^>]*>(.*?)</form>', re.S)
FULL = "week=2026-W46&member={m}&day=4"


@pytest.fixture
def client():
    return Client()


def links(body):
    return [html.unescape(h) for h in HREF.findall(body)]


def link_of(body):
    [found] = links(body)
    return found


def back(body):
    found = BACK.findall(body)
    return html.unescape(found[0]) if found else None


def edit(client, pk, query="", when=WED):
    with at(when):
        return client.get(f"/chores/{pk}/edit/{query}")


def one_member():
    return member("Ana", order=1)


# --- Card title link -----------------------------------------------------------------


@pytest.mark.django_db
def test_card_link_carries_full_filters(client):
    m = one_member()
    c = occ("Dishes", "2026-11-12", member=m).chore
    body = page(client, week="2026-W46", data={"member": m.pk, "day": "4"})
    assert link_of(body) == f"/chores/{c.pk}/edit/?" + FULL.format(m=m.pk)


@pytest.mark.django_db
def test_card_link_unfiltered_is_unchanged(client):
    c = occ("Dishes", "2026-11-18").chore
    assert link_of(page(client)) == f"/chores/{c.pk}/edit/"


@pytest.mark.django_db
def test_card_link_current_week_explicit_is_dropped(client):
    c = occ("Dishes", "2026-11-18").chore
    body = page(client, data={"week": "2026-W47", "day": "3"})
    assert link_of(body) == f"/chores/{c.pk}/edit/?day=3"


@pytest.mark.django_db
def test_card_link_keeps_unassigned(client):
    c = occ("Dishes", "2026-11-18").chore
    body = page(client, data={"member": "unassigned"})
    assert link_of(body) == f"/chores/{c.pk}/edit/?member=unassigned"


@pytest.mark.django_db
@pytest.mark.parametrize("data", [
    {"week": "2026-W99"}, {"member": "abc"}, {"member": "99999"}, {"day": "9"},
    {"week": "//evil.example", "member": "1\r\nX", "day": HOSTILE},
])
def test_card_link_drops_invalid_values(client, data):
    c = occ("Dishes", "2026-11-18").chore
    body = page(client, data=data)
    assert link_of(body) == f"/chores/{c.pk}/edit/"
    assert "evil.example" not in body and HOSTILE not in body


@pytest.mark.django_db
def test_projected_card_has_the_link(client):
    c = weekly("Trash", weekday=3)
    body = page(client, week="2026-W48", data={"day": "4"})
    assert link_of(body) == f"/chores/{c.pk}/edit/?week=2026-W48&day=4"


@pytest.mark.django_db
def test_toggle_swap_has_the_same_link_as_a_fresh_load(client):
    m = one_member()
    occurrence = occ("Dishes", "2026-11-12", member=m)
    data = {"week": "2026-W46", "member": str(m.pk), "day": "4"}
    with at(WED):
        response = client.post(
            f"/dashboard/toggle/{occurrence.pk}/", {**data, "action": "done"}, **HTMX)
    assert response.status_code == 200
    fresh = page(client, week="2026-W46", data={"member": m.pk, "day": "4"})
    assert link_of(response.content.decode()) == link_of(fresh)
    assert "week=2026-W46" in link_of(fresh)


@pytest.mark.django_db
def test_assign_swap_without_filters_has_the_same_link(client):
    m = one_member()
    pk = occ("Dishes", "2026-11-18").pk
    with at(WED):
        response = client.post(f"/dashboard/assign/{pk}/", {"assignee": str(m.pk)}, **HTMX)
    assert response.status_code == 200
    assert link_of(response.content.decode()) == link_of(page(client))


# --- Edit page: back link and hidden inputs ---------------------------------------------


@pytest.mark.django_db
def test_back_link_filtered(client):
    m = one_member()
    c = weekly()
    body = edit(client, c.pk, "?" + FULL.format(m=m.pk)).content.decode()
    assert back(body) == "/dashboard/?" + FULL.format(m=m.pk)
    assert body.index('id="back-to-dashboard"') < body.index('id="chore-form"')


@pytest.mark.django_db
def test_back_link_unassigned(client):
    c = weekly()
    body = edit(client, c.pk, "?member=unassigned&day=2").content.decode()
    assert back(body) == "/dashboard/?member=unassigned&day=2"


@pytest.mark.django_db
@pytest.mark.parametrize("query", ["", "?week=2026-W99&member=abc&day=9", "?member=99999"])
def test_back_link_without_valid_values(client, query):
    c = weekly()
    body = edit(client, c.pk, query).content.decode()
    assert back(body) == "/dashboard/"
    assert hidden(FORM.search(body).group(1)) == {}
    assert hidden(DELETE.search(body).group(1)) == {}


@pytest.mark.django_db
@pytest.mark.parametrize("query", [
    "?week=//evil.example&member=1%0d%0aX&day=%3Cscript%3E",
    "?week=https://evil.example/&member=http://evil.example",
])
def test_hostile_values_never_echoed(client, query):
    c = weekly()
    response = edit(client, c.pk, query)
    body = response.content.decode()
    assert back(body) == "/dashboard/"
    assert "evil" not in body and "<script>alert" not in body and "%0d" not in body


@pytest.mark.django_db
def test_hidden_inputs_on_both_forms(client):
    m = one_member()
    c = weekly()
    body = edit(client, c.pk, "?" + FULL.format(m=m.pk)).content.decode()
    expected = {"week": "2026-W46", "member": str(m.pk), "day": "4"}
    assert hidden(FORM.search(body).group(1)) == expected
    assert hidden(DELETE.search(body).group(1)) == expected


@pytest.mark.django_db
def test_hidden_inputs_only_when_set(client):
    c = weekly()
    body = edit(client, c.pk, "?member=unassigned").content.decode()
    assert hidden(FORM.search(body).group(1)) == {"member": "unassigned"}
    assert hidden(DELETE.search(body).group(1)) == {"member": "unassigned"}


# --- Save and delete -----------------------------------------------------------------


@pytest.mark.django_db
def test_save_redirect_keeps_query_and_message_and_back_link(client):
    m = one_member()
    c = weekly()
    data = payload(title="Trash 2", start_date="2026-11-04", recurrence="weekly", recurrence_day="2")
    data.update({"week": "2026-W46", "member": str(m.pk), "day": "4"})
    response = client.post(f"/chores/{c.pk}/edit/", data)
    assert response.status_code == 302
    assert response["Location"] == f"/chores/{c.pk}/edit/?" + FULL.format(m=m.pk)
    follow = client.get(response["Location"])
    body = follow.content.decode()
    assert "Chore saved" in body
    assert back(body) == "/dashboard/?" + FULL.format(m=m.pk)
    c.refresh_from_db()
    assert c.title == "Trash 2"


@pytest.mark.django_db
def test_save_redirect_without_values_is_unchanged(client):
    c = weekly()
    data = payload(title="Trash 2", start_date="2026-11-04", recurrence="weekly", recurrence_day="2")
    response = client.post(f"/chores/{c.pk}/edit/", data)
    assert response["Location"] == f"/chores/{c.pk}/edit/"


@pytest.mark.django_db
def test_save_redirect_drops_hostile_values(client):
    c = weekly()
    data = payload(title="Trash 2", start_date="2026-11-04", recurrence="weekly", recurrence_day="2")
    data.update({"week": "//evil.example", "member": "1\r\nX", "day": "9"})
    response = client.post(f"/chores/{c.pk}/edit/", data)
    assert response["Location"] == f"/chores/{c.pk}/edit/"


@pytest.mark.django_db
def test_invalid_save_keeps_link_and_inputs(client):
    m = one_member()
    c = weekly()
    data = payload(title="", start_date="2026-11-04", recurrence="weekly", recurrence_day="2")
    data.update({"week": "2026-W46", "member": str(m.pk), "day": "4"})
    response = client.post(f"/chores/{c.pk}/edit/", data)
    assert response.status_code == 200
    body = response.content.decode()
    assert "errorlist" in body
    assert back(body) == "/dashboard/?" + FULL.format(m=m.pk)
    expected = {"week": "2026-W46", "member": str(m.pk), "day": "4"}
    assert hidden(FORM.search(body).group(1)) == expected
    assert hidden(DELETE.search(body).group(1)) == expected


@pytest.mark.django_db
def test_delete_from_edit_page_lands_on_filtered_dashboard(client):
    c = chore("Once", start_date="2026-11-18")
    body = edit(client, c.pk, "?member=unassigned&day=2").content.decode()
    fields = hidden(DELETE.search(body).group(1))
    with at(WED):
        response = client.post(f"/chores/{c.pk}/delete/", {**fields, "scope": "future"})
    assert response.status_code == 302
    assert response["Location"] == "/dashboard/?member=unassigned&day=2"
    assert not Chore.objects.filter(pk=c.pk).exists()


# --- Create and template edit are unchanged ------------------------------------------------


@pytest.mark.django_db
def test_create_has_no_back_link_or_inputs(client):
    one_member()
    body = client.get("/chores/create/?week=2026-W46&member=unassigned&day=4").content.decode()
    assert back(body) is None and "back-to-dashboard" not in body
    assert hidden(FORM.search(body).group(1)) == {}


@pytest.mark.django_db
def test_create_redirect_ignores_filters(client):
    data = payload()
    data.update({"week": "2026-W46", "member": "unassigned", "day": "4"})
    response = client.post("/chores/create/", data)
    assert response.status_code == 302
    assert re.fullmatch(r"/chores/\d+/edit/", response["Location"])


@pytest.mark.django_db
def test_template_edit_has_no_back_link_or_inputs(client):
    t = Chore.objects.create(title="Tpl", start_date=d("2020-01-01"), is_template=True)
    body = client.get(f"/chores/templates/{t.pk}/edit/?week=2026-W46&member=unassigned").content.decode()
    assert "back-to-dashboard" not in body
    assert hidden(FORM.search(body).group(1)) == {}
