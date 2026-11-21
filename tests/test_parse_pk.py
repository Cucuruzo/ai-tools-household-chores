"""_parse_pk and _family_pk_param never feed an over-long digit string to int() (#33)."""
import re
import sys

import pytest
from django.test import Client

import chores.profile
from chores import forms, template_views, views
from chores.models import Chore, ChoreOccurrence
from chores.profile import MAX_PK, _parse_pk
from tests.test_dashboard import WED, at, cards, member, page
from tests.test_delete import hidden, row, state, weekly
from tests.test_profile import current_buttons, set_session, switch
from tests.test_toggle import once, toggle

LIMIT = 4300  # Python 3.11+ default for sys.get_int_max_str_digits()
LONG = [pytest.param("1" * 4301, id="4301"), pytest.param("1" * 10000, id="10000")]
REAL_LIMIT = hasattr(sys, "set_int_max_str_digits")


def strict_int(value=0, *args, **kwargs):
    """``int`` as Python 3.11+ behaves: ValueError for strings over 4300 characters."""
    if isinstance(value, str) and len(value) > LIMIT:
        raise ValueError("Exceeds the limit (4300 digits) for integer string conversion")
    return int(value, *args, **kwargs)


@pytest.fixture
def client():
    return Client()


def real_is_valid_pk(pk):
    # Same rule as chores.profile.is_valid_pk. With ``int`` patched in that module,
    # the original's ``type(pk) is int`` compares against the fake and rejects every
    # pk, so every importer gets this copy, bound to the builtin int.
    return type(pk) is int and 1 <= pk <= MAX_PK


def simulate_311(monkeypatch):
    monkeypatch.setattr(chores.profile, "int", strict_int, raising=False)
    for module in (chores.profile, views, forms, template_views):
        monkeypatch.setattr(module, "is_valid_pk", real_is_valid_pk)


@pytest.fixture
def strict(monkeypatch):
    simulate_311(monkeypatch)


@pytest.fixture(params=["python-3.9-int", "simulated-3.11-int"])
def int_mode(request, monkeypatch):
    """Run with the interpreter's own int and with a 3.11-style limit."""
    if request.param == "simulated-3.11-int":
        simulate_311(monkeypatch)
    return request.param


@pytest.fixture
def family(db):
    return member("Mom", role="adult", order=0), member("Emma", order=1)


# --- _parse_pk values ---------------------------------------------------------------------

CASES = [
    (1, 1), (MAX_PK, MAX_PK), ("1", 1), (str(MAX_PK), MAX_PK), ("0", None), ("", None),
    (str(MAX_PK + 1), None), ("1" * 20, None), ("0001", 1), ("0" * 30 + "1", 1),
    ("0" * 30 + str(MAX_PK), MAX_PK), ("0" * 5000, None), (" 1", None), ("1 ", None),
    ("-1", None), ("+1", None), ("1.0", None), ("١", None), (True, None), (1.0, None),
    (None, None), (0, None), (-1, None), (MAX_PK + 1, None),
]


@pytest.mark.parametrize("raw, expected", CASES, ids=[repr(c[0])[:40] for c in CASES])
def test_parse_pk_values_are_unchanged(int_mode, raw, expected):
    result = _parse_pk(raw)
    assert result == expected
    assert type(result) is type(expected)


def test_parse_pk_bound_comes_from_max_pk():
    assert len(str(MAX_PK)) == 19
    assert _parse_pk("9" * 19) is None
    assert _parse_pk("9" * 20) is None
    assert _parse_pk("0" + str(MAX_PK)) == MAX_PK


@pytest.mark.parametrize("raw", ["1" * 4301, "1" * 10000, "9" * 20, "0" * 5000 + "1" * 4301])
def test_oversized_strings_are_invalid(int_mode, raw):
    assert _parse_pk(raw) is None


def test_int_never_sees_the_unstripped_string(strict):
    # 3.11+ counts leading zeros against the limit too.
    assert _parse_pk("0" * 5000 + "1") == 1
    assert _parse_pk("0" * 5000) is None


@pytest.mark.skipif(not REAL_LIMIT, reason="sys.set_int_max_str_digits needs Python 3.11+")
@pytest.mark.parametrize("raw, expected", [
    ("1" * 4301, None), ("1" * 10000, None), ("0" * 5000 + "1" * 4301, None),
    ("0" * 5000 + "1", 1), ("0" * 5000, None),
])
def test_real_int_limit(raw, expected):
    old = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(LIMIT)
    try:
        assert _parse_pk(raw) == expected
    finally:
        sys.set_int_max_str_digits(old)


# --- Dashboard ?member= --------------------------------------------------------------------


@pytest.mark.parametrize("value", LONG)
def test_dashboard_member_filter(client, family, int_mode, value):
    mom, emma = family
    row(weekly(), "2026-11-18", member=mom)
    row(weekly("Mop"), "2026-11-18", member=emma)
    plain = page(client)
    assert len(cards(plain)) == 2
    body = page(client, data={"member": value})
    assert cards(body) == cards(plain)
    assert '<option value="all" selected>Everyone</option>' in body
    assert 'name="member"' not in re.sub(r"<select.*?</select>", "", body, flags=re.S)


@pytest.mark.parametrize("value", LONG)
def test_dashboard_member_filter_keeps_week_and_day(client, family, int_mode, value):
    body = page(client, data={"member": value, "week": "2026-W46", "day": "3"})
    assert 'name="week" value="2026-W46"' in body
    assert re.search(r'<option value="3" selected>', body)
    assert '<option value="all" selected>Everyone</option>' in body


# --- Session active_member_id --------------------------------------------------------------


@pytest.mark.parametrize("url", ["/dashboard/", "/calendar/", "/family/"])
@pytest.mark.parametrize("value", LONG)
def test_session_value_is_a_signed_out_viewer(client, family, int_mode, url, value):
    set_session(client, value)
    with at(WED):
        response = client.get(url)
    assert response.status_code == 200
    body = response.content.decode()
    assert "Who are you?" in body
    assert 'aria-current="true"' not in re.search(r'<nav id="profile-switcher".*?</nav>', body, re.S).group(0)
    assert "active_member_id" not in client.session and "active_member_ids" not in client.session


def test_switcher_still_works_after_a_bad_session_value(client, family, int_mode):
    mom, _ = family
    set_session(client, "1" * 4301)
    assert client.get("/dashboard/").status_code == 200
    assert switch(client, mom).status_code == 302
    assert current_buttons(client.get("/family/").content.decode()) == [str(mom.pk)]


# --- Family ?edit= / ?remove= --------------------------------------------------------------


@pytest.mark.parametrize("name", ["edit", "remove"])
@pytest.mark.parametrize("value", LONG)
def test_family_param_oversized_is_404(client, family, int_mode, name, value):
    assert client.get("/family/", {name: value}).status_code == 404


@pytest.mark.parametrize("name", ["edit", "remove"])
def test_family_param_unchanged_behaviour(client, family, int_mode, name):
    mom, _ = family
    for good in (str(mom.pk), "0" * 30 + str(mom.pk)):
        assert client.get("/family/", {name: good}).status_code == 200
    for bad in ("0", "abc", "-1", str(MAX_PK + 1), "9999", "0" * 5000, ""):
        assert client.get("/family/", {name: bad}).status_code == 404
    assert client.get(f"/family/?{name}=1&{name}=2").status_code == 404
    assert client.get("/family/").status_code == 200


def test_family_edit_and_remove_together_is_400(client, family):
    assert client.get("/family/?edit=1&remove=1").status_code == 400


# --- POST callers --------------------------------------------------------------------------


@pytest.mark.parametrize("value", LONG)
def test_toggle_member_field_falls_back_to_everyone(client, family, int_mode, value):
    o = once()
    response = toggle(client, o.pk, "done", htmx=True, member=value)
    assert response.status_code == 200
    assert "member" not in hidden(response.content.decode())
    o.refresh_from_db()
    assert o.status == "done"


@pytest.mark.parametrize("value", LONG)
def test_assign_field_is_400_and_changes_nothing(client, family, int_mode, value):
    o = once()
    with at(WED):
        response = client.post(f"/dashboard/assign/{o.pk}/", {"assignee": value})
    assert response.status_code == 400
    o.refresh_from_db()
    assert o.member is None and not o.is_override


@pytest.mark.parametrize("value", LONG)
def test_chore_delete_occurrence_field_is_404(client, int_mode, value, db):
    c = weekly()
    row(c, "2026-11-18")
    before = state()
    with at(WED):
        response = client.post(f"/chores/{c.pk}/delete/", {"occurrence": value, "scope": "this"})
    assert response.status_code == 404
    assert state() == before
    assert Chore.objects.filter(pk=c.pk).exists()
    assert ChoreOccurrence.objects.count() == 1
