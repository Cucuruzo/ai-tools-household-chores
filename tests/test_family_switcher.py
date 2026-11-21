import re

import pytest
from django.test import Client
from django.urls import reverse

from chores.models import Member

HTMX = {"HTTP_HX_REQUEST": "true"}
FAMILY = "/family/"


@pytest.fixture
def client():
    return Client()


def make(name, role="adult", avatar="😀"):
    return Member.objects.create(name=name, role=role, avatar=avatar)


def set_active(client, member):
    session = client.session
    session["active_member_id"] = member.pk
    session.save()


def nav(html):
    match = re.search(r'<nav id="profile-switcher".*?</nav>', html, re.S)
    assert match, "no #profile-switcher in response"
    # CSRF tokens are re-masked on every render; ignore them when comparing.
    return re.sub(r'(name="csrfmiddlewaretoken" value=")[^"]*', r"\1", match.group(0))


def entries(html):
    """Names shown in the switcher buttons, in order."""
    return re.findall(r'<span class="avatar">(.*?)</span>\s*(?:<strong>)?(.*?)(?:</strong>)?\s*</button>', nav(html), re.S)


def post_add(client, name="Zed", **extra):
    data = {"name": name, "role": "adult", "avatar": "😀", **extra}
    return client.post(reverse("member_add"), data, **HTMX)


# --- Wiring ------------------------------------------------------------------


@pytest.mark.django_db
def test_family_page_wires_oob_on_add_move_delete(client):
    make("Ann")
    make("Bob")
    html = client.get(reverse("family")).content.decode()
    assert html.count('hx-select-oob="#profile-switcher"') == 1 + 1 + 1 + 2  # add, down, up, 2 removes


@pytest.mark.django_db
def test_switcher_markup_not_oob_in_normal_page(client):
    make("Ann")
    assert "hx-swap-oob" not in client.get(reverse("family")).content.decode()


# --- Add ---------------------------------------------------------------------


@pytest.mark.django_db
def test_add_response_carries_switcher_with_new_member_last(client):
    make("Ann")
    html = post_add(client, "Zed").content.decode()
    assert [n for _, n in entries(html)] == ["Ann", "Zed"]
    assert 'name="next" value="/family/"' in nav(html)


@pytest.mark.django_db
def test_add_first_member_replaces_add_your_family(client):
    html = post_add(client, "Ann").content.decode()
    n = nav(html)
    assert "Who are you?" in n
    assert "Add your family" not in n
    assert "Ann" in n
    assert 'aria-current="true"' not in n


@pytest.mark.django_db
def test_failed_add_leaves_switcher_unchanged(client):
    make("Ann")
    before = nav(client.get(reverse("family")).content.decode())
    for data in ({"name": ""}, {"name": "x" * 51}, {"name": "Z", "avatar": "X"}, {"name": "Z", "role": "alien"}):
        payload = {"role": "adult", "avatar": "😀", **data}
        response = client.post(reverse("member_add"), payload, **HTMX)
        html = response.content.decode()
        assert 'class="errorlist"' in html
        assert nav(html) == before
    assert Member.objects.count() == 1


@pytest.mark.django_db
def test_add_without_js_redirects_and_header_lists_member(client):
    response = client.post(reverse("member_add"), {"name": "Ann", "role": "adult", "avatar": "😀"})
    assert response.status_code == 302 and response["Location"] == FAMILY
    assert "Ann" in nav(client.get(FAMILY).content.decode())


# --- Edit --------------------------------------------------------------------


def edit(client, m, **data):
    payload = {"name": m.name, "role": m.role, "avatar": m.avatar, **data}
    return client.post(reverse("member_edit", args=[m.pk]), payload, **HTMX)


@pytest.mark.django_db
def test_edit_appends_oob_switcher_with_new_name_and_avatar(client):
    ann, bob = make("Ann"), make("Bob", avatar="🐱")
    response = edit(client, ann, name="Annie", avatar="🦊")
    html = response.content.decode()
    assert html.count('id="profile-switcher"') == 1
    n = nav(html)
    assert 'hx-swap-oob="true"' in n
    assert [x for x in entries(html)] == [("🦊", "Annie"), ("🐱", "Bob")]
    assert 'name="next" value="/family/"' in n
    assert f'id="member-{ann.pk}"' in html


@pytest.mark.django_db
def test_edit_active_member_stays_highlighted(client):
    ann, bob = make("Ann"), make("Bob")
    set_active(client, ann)
    n = nav(edit(client, ann, name="Annie", avatar="🦊").content.decode())
    assert n.count('aria-current="true"') == 1
    assert re.search(r'aria-current="true">\s*<span class="avatar">🦊</span> <strong>Annie</strong>', n)
    assert "Who are you?" not in n


@pytest.mark.django_db
def test_edit_duplicate_names_changes_only_one(client):
    a, b = make("Sam"), make("Sam")
    html = edit(client, b, name="Sammy").content.decode()
    assert [n for _, n in entries(html)] == ["Sam", "Sammy"]


@pytest.mark.django_db
def test_failed_edit_has_no_switcher_and_keeps_form_open(client):
    ann = make("Ann")
    response = edit(client, ann, name="")
    html = response.content.decode()
    assert 'id="profile-switcher"' not in html
    assert 'class="errorlist"' in html
    assert '<div class="member-view" hidden' in html


@pytest.mark.django_db
def test_edit_cancel_markup_does_not_touch_header(client):
    # Cancel is client-side only (resets the form, no request), so no edit POST happens.
    ann = make("Ann")
    html = client.get(reverse("family")).content.decode()
    assert "this.form.reset()" in html
    assert Member.objects.get(pk=ann.pk).name == "Ann"


@pytest.mark.django_db
def test_edit_without_js_redirects_to_family(client):
    ann = make("Ann")
    response = client.post(reverse("member_edit", args=[ann.pk]), {"name": "Annie", "role": "kid", "avatar": "🦊"})
    assert response.status_code == 302 and response["Location"] == FAMILY
    ann.refresh_from_db()
    assert (ann.name, ann.avatar) == ("Annie", "🦊")
    assert entries(client.get(FAMILY).content.decode()) == [("🦊", "Annie")]


# --- Move --------------------------------------------------------------------


@pytest.mark.django_db
def test_move_reorders_switcher_and_keeps_highlight(client):
    ann, bob = make("Ann"), make("Bob")
    set_active(client, bob)
    response = client.post(reverse("member_move", args=[bob.pk]) + "?dir=up", **HTMX)
    html = response.content.decode()
    assert [n for _, n in entries(html)] == ["Bob", "Ann"]
    n = nav(html)
    assert n.count('aria-current="true"') == 1
    assert "<strong>Bob</strong>" in n
    assert 'name="next" value="/family/"' in n


@pytest.mark.django_db
def test_move_without_js_redirects(client):
    ann, bob = make("Ann"), make("Bob")
    response = client.post(reverse("member_move", args=[bob.pk]) + "?dir=up")
    assert response.status_code == 302 and response["Location"] == FAMILY
    assert [n for _, n in entries(client.get(FAMILY).content.decode())] == ["Bob", "Ann"]


# --- Delete ------------------------------------------------------------------


@pytest.mark.django_db
def test_delete_non_active_keeps_active_highlighted(client):
    ann, bob = make("Ann"), make("Bob")
    set_active(client, ann)
    html = client.post(reverse("member_delete", args=[bob.pk]), **HTMX).content.decode()
    n = nav(html)
    assert [x for _, x in entries(html)] == ["Ann"]
    assert "<strong>Ann</strong>" in n and "Who are you?" not in n
    assert 'name="next" value="/family/"' in n


@pytest.mark.django_db
def test_delete_active_shows_who_are_you_without_highlight(client):
    ann, bob = make("Ann"), make("Bob")
    set_active(client, ann)
    html = client.post(reverse("member_delete", args=[ann.pk]), **HTMX).content.decode()
    n = nav(html)
    assert "Who are you?" in n
    assert 'aria-current="true"' not in n
    assert [x for _, x in entries(html)] == ["Bob"]


@pytest.mark.django_db
def test_delete_last_active_member_shows_add_your_family(client):
    ann = make("Ann")
    set_active(client, ann)
    n = nav(client.post(reverse("member_delete", args=[ann.pk]), **HTMX).content.decode())
    assert "Add your family" in n
    assert "Who are you?" not in n


@pytest.mark.django_db
def test_delete_duplicate_name_removes_only_that_one(client):
    a, b = make("Sam"), make("Sam")
    html = client.post(reverse("member_delete", args=[a.pk]), **HTMX).content.decode()
    assert len(entries(html)) == 1
    assert f'/profile/{b.pk}/' in nav(html) or str(b.pk) in nav(html)


@pytest.mark.django_db
def test_delete_without_js_redirects(client):
    ann, bob = make("Ann"), make("Bob")
    response = client.post(reverse("member_delete", args=[ann.pk]))
    assert response.status_code == 302 and response["Location"] == FAMILY
    assert [n for _, n in entries(client.get(FAMILY).content.decode())] == ["Bob"]


# --- Switching after a sync --------------------------------------------------


@pytest.mark.django_db
def test_switching_from_synced_header_lands_on_family(client):
    ann, bob = make("Ann"), make("Bob")
    html = edit(client, ann, name="Annie").content.decode()
    action = re.search(r'action="([^"]+)"', nav(html)).group(1)
    nxt = re.search(r'name="next" value="([^"]+)"', nav(html)).group(1)
    response = client.post(action, {"next": nxt})
    assert response.status_code == 302 and response["Location"] == FAMILY
    assert client.session["active_member_id"] == ann.pk
