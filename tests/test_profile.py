import re

import pytest
from django.test import Client
from django.urls import reverse

from chores.models import Member
from chores.profile import get_active_member

FAMILY = "/family/"


@pytest.fixture
def client():
    return Client()


def make(name, avatar="😀"):
    return Member.objects.create(name=name, role="adult", avatar=avatar)


def switch(client, member, **kwargs):
    return client.post(reverse("profile_switch", args=[member.pk]), **kwargs)


def set_session(client, value):
    session = client.session
    session["active_member_id"] = value
    session.save()


def page(client, url=FAMILY):
    return client.get(url).content.decode()


def switcher(html):
    return re.search(r'<nav id="profile-switcher".*?</nav>', html, re.S).group(0)


def current_buttons(html):
    return re.findall(r'<form method="post" action="/profile/(\d+)/">(?:(?!</form>).)*aria-current="true"', switcher(html), re.S)


# --- Display ---------------------------------------------------------------


@pytest.mark.django_db
def test_switcher_lists_members_in_order_as_post_forms(client):
    make("Zed", "🦊")
    Member.objects.filter(name="Zed").update(order=5)
    make("Amy", "👧")
    Member.objects.filter(name="Amy").update(order=1)
    nav = switcher(page(client))
    assert nav.index("Amy") < nav.index("Zed")
    assert "👧" in nav and "🦊" in nav
    assert nav.count('<form method="post"') == 2
    assert nav.count("csrfmiddlewaretoken") == 2
    assert nav.count('<button type="submit"') == 2
    assert 'name="next" value="/family/"' in nav


@pytest.mark.django_db
def test_no_active_member_shows_who_are_you(client):
    make("Ann")
    nav = switcher(page(client))
    assert "Who are you?" in nav
    assert "aria-current" not in nav
    assert 'class="profile active"' not in nav


@pytest.mark.django_db
def test_no_members_shows_add_your_family_link(client):
    nav = switcher(page(client))
    assert '<a href="/family/">Add your family</a>' in nav
    assert "Who are you?" not in nav


@pytest.mark.django_db
def test_member_name_is_escaped(client):
    make("<b>x</b>")
    nav = switcher(page(client))
    assert "&lt;b&gt;x&lt;/b&gt;" in nav
    assert "<b>x</b>" not in nav


@pytest.mark.django_db
def test_highlight_follows_session(client):
    ann, bob = make("Ann"), make("Bob")
    set_session(client, bob.pk)
    html = page(client)
    assert current_buttons(html) == [str(bob.pk)]
    assert switcher(html).count('aria-current="true"') == 1
    assert "Who are you?" not in switcher(html)


@pytest.mark.django_db
def test_duplicate_names_both_shown_and_highlighted_separately(client):
    first, second = make("Sam", "🐶"), make("Sam", "🐱")
    nav = switcher(page(client))
    assert "🐶" in nav and "🐱" in nav
    switch(client, second)
    assert current_buttons(page(client)) == [str(second.pk)]
    switch(client, second)
    switch(client, first)
    assert current_buttons(page(client)) == [str(first.pk)]


# --- Switching -------------------------------------------------------------


@pytest.mark.django_db
def test_switch_sets_session_and_redirects_via_next(client):
    ann = make("Ann")
    response = switch(client, ann, data={"next": "/family/?x=1"})
    assert response.status_code == 302
    assert response["Location"] == "/family/?x=1"
    assert client.session["active_member_ids"] == [ann.pk]
    assert current_buttons(page(client)) == [str(ann.pk)]


@pytest.mark.django_db
def test_switch_to_already_active_member_clears_it(client):
    ann = make("Ann")
    switch(client, ann, data={"next": FAMILY})
    response = switch(client, ann, data={"next": FAMILY})
    assert response.status_code == 302
    assert response["Location"] == FAMILY
    assert "active_member_id" not in client.session and "active_member_ids" not in client.session
    nav = switcher(page(client))
    assert "Who are you?" in nav
    assert "aria-current" not in nav and "aria-pressed" not in nav


@pytest.mark.django_db
def test_switch_redirects_via_referer_when_no_next(client):
    ann = make("Ann")
    response = switch(client, ann, HTTP_REFERER="http://testserver/family/")
    assert response.status_code == 302
    assert response["Location"] == "http://testserver/family/"


@pytest.mark.django_db
def test_switch_falls_back_to_root(client):
    ann = make("Ann")
    response = switch(client, ann)
    assert response.status_code == 302
    assert response["Location"] == "/"


@pytest.mark.django_db
@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/"])
def test_external_next_is_blocked(client, target):
    ann = make("Ann")
    response = switch(client, ann, data={"next": target})
    assert response["Location"] == "/"
    assert client.session["active_member_ids"] == [ann.pk]


@pytest.mark.django_db
@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/"])
def test_external_referer_is_blocked(client, target):
    ann = make("Ann")
    response = switch(client, ann, HTTP_REFERER=target)
    assert response["Location"] == "/"
    assert client.session["active_member_ids"] == [ann.pk]


@pytest.mark.django_db
def test_unknown_id_is_404_and_session_unchanged(client):
    ann = make("Ann")
    set_session(client, ann.pk)
    response = client.post(reverse("profile_switch", args=[999]), {"next": FAMILY})
    assert response.status_code == 404
    assert client.session["active_member_id"] == ann.pk


@pytest.mark.django_db
def test_get_is_405_and_session_unchanged(client):
    ann = make("Ann")
    response = client.get(reverse("profile_switch", args=[ann.pk]))
    assert response.status_code == 405
    assert "active_member_id" not in client.session and "active_member_ids" not in client.session


@pytest.mark.django_db
@pytest.mark.parametrize("pk", [2**63, 10**30])
def test_huge_id_is_404_and_session_unchanged(client, pk):
    ann = make("Ann")
    set_session(client, ann.pk)
    response = client.post(f"/profile/{pk}/", {"next": FAMILY})
    assert response.status_code == 404
    assert client.session["active_member_id"] == ann.pk


@pytest.mark.django_db
def test_max_pk_is_still_a_valid_lookup(client):
    make("Ann")
    set_session(client, 2**63 - 1)
    assert client.get(FAMILY).status_code == 200
    assert "active_member_id" not in client.session and "active_member_ids" not in client.session


@pytest.mark.django_db
def test_non_numeric_id_is_404(client):
    assert client.post("/profile/abc/").status_code == 404


# --- Stale or invalid session ---------------------------------------------


@pytest.mark.django_db
def test_deleted_active_member_clears_key_and_shows_who_are_you(client):
    ann, bob = make("Ann"), make("Bob")
    switch(client, bob)
    bob.delete()
    response = client.get(FAMILY)
    assert response.status_code == 200
    nav = switcher(response.content.decode())
    assert "Who are you?" in nav
    assert "aria-current" not in nav
    assert "active_member_id" not in client.session and "active_member_ids" not in client.session


@pytest.mark.django_db
def test_deleted_last_member_shows_add_your_family(client):
    ann = make("Ann")
    switch(client, ann)
    ann.delete()
    nav = switcher(page(client))
    assert "Add your family" in nav
    assert "active_member_id" not in client.session and "active_member_ids" not in client.session


@pytest.mark.django_db
@pytest.mark.parametrize(
    "value",
    ["abc", "", 1.5, True, None, 0, -1, 2**63, 10**30, "1" + "0" * 30],
    ids=["str", "empty", "float", "bool", "none", "zero", "negative",
         "2**63", "10**30", "huge-digit-string"],
)
def test_non_integer_session_value_is_treated_as_stale(client, value):
    # pk=1 so that int(1.5) / int(True) would hit a real member if coerced.
    Member.objects.create(pk=1, name="Ann", role="adult", avatar="😀")
    set_session(client, value)
    response = client.get(FAMILY)
    assert response.status_code == 200
    nav = switcher(response.content.decode())
    assert "Who are you?" in nav
    assert "aria-current" not in nav
    assert "active_member_id" not in client.session and "active_member_ids" not in client.session


@pytest.mark.django_db
def test_digit_string_session_value_is_accepted(client):
    ann = make("Ann")
    set_session(client, str(ann.pk))
    assert current_buttons(page(client)) == [str(ann.pk)]


# --- Helper ----------------------------------------------------------------


@pytest.mark.django_db
def test_get_active_member_helper(rf):
    from django.contrib.sessions.backends.db import SessionStore

    ann = make("Ann")
    request = rf.get("/")
    request.session = SessionStore()
    assert get_active_member(request) is None
    request.session["active_member_ids"] = [ann.pk]
    assert get_active_member(request) == ann
    request.session["active_member_ids"] = [999]
    assert get_active_member(request) is None
    assert "active_member_ids" not in request.session
    for bad in (1.5, True, None, 0, -1, 2**63, "1" + "0" * 30):
        request.session["active_member_ids"] = [bad]
        assert get_active_member(request) is None
        assert "active_member_ids" not in request.session
