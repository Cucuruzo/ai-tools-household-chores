import re

import pytest
from django.test import Client
from django.urls import reverse

from chores.models import Member

HTMX = {"HTTP_HX_REQUEST": "true"}
HUGE = "99999999999999999999"
HOSTILE = ["abc", "0", "-1", "1.5", "", HUGE]


@pytest.fixture
def client():
    return Client()


def make(name, role="adult", avatar="😀"):
    return Member.objects.create(name=name, role=role, avatar=avatar)


@pytest.fixture
def trio():
    return make("Ann"), make("Bob", "kid", "🐱"), make("Cid")


def names():
    return list(Member.objects.values_list("name", flat=True))


def row(html, member):
    m = re.search(rf'<article class="member-row" id="member-{member.pk}">.*?</article>', html, re.S)
    return m.group(0)


# --- Move ------------------------------------------------------------------


@pytest.mark.django_db
def test_move_buttons_are_submit_buttons_in_post_forms(client, trio):
    ann, bob, cid = trio
    html = client.get(reverse("family")).content.decode()
    assert f'action="/family/{ann.pk}/move/?dir=down"' in html
    assert f'action="/family/{cid.pk}/move/?dir=up"' in html
    assert html.count('name="csrfmiddlewaretoken"') >= 5
    assert "move/?dir=up" not in row(html, ann)
    assert "move/?dir=down" not in row(html, cid)


@pytest.mark.django_db
def test_move_down_then_up_without_js(client, trio):
    ann, bob, cid = trio
    r = client.post(reverse("member_move", args=[ann.pk]) + "?dir=down")
    assert r.status_code == 302 and r["Location"] == "/family/"
    assert names() == ["Bob", "Ann", "Cid"]
    page = client.get(r["Location"]).content.decode()
    assert page.index("Bob") < page.index("Ann") < page.index("Cid")
    client.post(reverse("member_move", args=[cid.pk]) + "?dir=up")
    assert names() == ["Bob", "Cid", "Ann"]


@pytest.mark.django_db
@pytest.mark.parametrize("query", ["", "?dir=sideways"])
def test_move_bad_dir_is_400_and_changes_nothing(client, trio, query):
    r = client.post(reverse("member_move", args=[trio[0].pk]) + query)
    assert r.status_code == 400
    assert names() == ["Ann", "Bob", "Cid"]


# --- Edit ------------------------------------------------------------------


@pytest.mark.django_db
def test_edit_link_in_markup(client, trio):
    bob = trio[1]
    html = client.get(reverse("family")).content.decode()
    assert f'href="/family/?edit={bob.pk}"' in html
    assert "event.preventDefault()" in row(html, bob)


@pytest.mark.django_db
def test_edit_page_opens_only_that_row(client, trio):
    ann, bob, cid = trio
    r = client.get(reverse("family"), {"edit": bob.pk})
    html = r.content.decode()
    assert r.status_code == 200
    assert 'id="profile-switcher"' in html
    bob_row, ann_row = row(html, bob), row(html, ann)
    assert re.search(r'<form class="member-edit"\s+method', bob_row)
    assert 'value="Bob"' in bob_row
    assert re.search(r'<option value="kid"[^>]*selected', bob_row)
    assert "🐱" in bob_row
    assert 'member-view" hidden' not in ann_row
    assert "member-view\" hidden" in bob_row
    assert "Edit</a>" in ann_row and "Remove</a>" in ann_row
    assert 'value="/family/"' in html  # switcher next stays /family/


@pytest.mark.django_db
def test_edit_page_has_noscript_cancel_link_and_js_only_hidden_style(client, trio):
    html = client.get(reverse("family"), {"edit": trio[1].pk}).content.decode()
    assert '<noscript><a href="/family/" role="button" class="secondary">Cancel</a></noscript>' in html
    assert "<noscript><style>.js-only { display: none !important; }</style></noscript>" in html
    assert 'class="secondary js-only"' in html


@pytest.mark.django_db
def test_valid_nojs_edit_redirects_and_applies(client, trio):
    bob = trio[1]
    r = client.post(reverse("member_edit", args=[bob.pk]), {"name": "Bobby", "role": "adult", "avatar": "🐶"})
    assert r.status_code == 302 and r["Location"] == "/family/"
    page = client.get(r["Location"]).content.decode()
    assert "Bobby" in row(page, bob) and "🐶" in row(page, bob)
    assert "Bobby" in page.split('id="profile-switcher"', 1)[1]


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["", "   "])
def test_invalid_nojs_edit_renders_full_page(client, trio, name):
    ann, bob, cid = trio
    r = client.post(reverse("member_edit", args=[bob.pk]), {"name": name, "role": "adult", "avatar": "🐶"})
    html = r.content.decode()
    assert r.status_code == 200
    assert "<html" in html and 'id="profile-switcher"' in html
    bob_row = row(html, bob)
    assert "errorlist" in bob_row
    assert "member-view\" hidden" in bob_row
    assert re.search(r'<option value="adult"[^>]*selected', bob_row)
    assert "🐶" in bob_row
    assert 'name="name"' in row(html, ann)
    assert "member-view\" hidden" not in row(html, ann)
    add_form = html.split('id="add-member-form"', 1)[1]
    assert "value=" not in re.search(r'<input[^>]*id="id_name"[^>]*>', add_form).group(0)
    assert Member.objects.get(pk=bob.pk).name == "Bob"


@pytest.mark.django_db
def test_invalid_nojs_edit_leaves_header_and_db_unchanged(client, trio):
    bob = trio[1]
    r = client.post(reverse("member_edit", args=[bob.pk]), {"name": "", "role": "adult", "avatar": "🐶"})
    html = r.content.decode()
    header = html.split('id="profile-switcher"', 1)[1].split('id="family"', 1)[0]
    assert "Bob" in header and "🐱" in header
    bob.refresh_from_db()
    assert (bob.role, bob.avatar) == ("kid", "🐱")


@pytest.mark.django_db
def test_fixing_invalid_edit_saves(client, trio):
    bob = trio[1]
    url = reverse("member_edit", args=[bob.pk])
    client.post(url, {"name": " ", "role": "adult", "avatar": "🐶"})
    r = client.post(url, {"name": "Rob", "role": "adult", "avatar": "🐶"})
    assert r.status_code == 302 and r["Location"] == "/family/"
    assert "Rob" in names()


@pytest.mark.django_db
def test_invalid_htmx_edit_still_bare_fragment(client, trio):
    r = client.post(reverse("member_edit", args=[trio[1].pk]), {"name": "", "role": "adult", "avatar": "🐶"}, **HTMX)
    html = r.content.decode()
    assert r.status_code == 200
    assert "<html" not in html and 'id="profile-switcher"' not in html
    assert html.lstrip().startswith("<article")


# --- Remove ----------------------------------------------------------------


@pytest.mark.django_db
def test_remove_link_keeps_htmx_attributes(client, trio):
    bob = trio[1]
    html = client.get(reverse("family")).content.decode()
    r = row(html, bob)
    assert f'href="/family/?remove={bob.pk}"' in r
    assert 'hx-confirm="Remove Bob? Their chores will become unassigned."' in r
    assert f'hx-post="/family/{bob.pk}/delete/"' in r


@pytest.mark.django_db
def test_remove_confirmation_page(client, trio):
    ann, bob, cid = trio
    r = client.get(reverse("family"), {"remove": bob.pk})
    html = r.content.decode()
    assert r.status_code == 200 and 'id="profile-switcher"' in html
    bob_row = row(html, bob)
    assert "Remove Bob? Their chores will become unassigned." in bob_row
    assert f'<form method="post" action="/family/{bob.pk}/delete/">' in bob_row
    assert "csrfmiddlewaretoken" in bob_row
    assert ">Remove Bob</button>" in bob_row
    assert '<a href="/family/" role="button" class="secondary">Cancel</a>' in bob_row
    assert "hx-" not in bob_row
    assert "move/" not in bob_row and "?edit=" not in bob_row and "?remove=" not in bob_row
    assert "🐱" in bob_row
    assert "?edit=" in row(html, ann)
    assert Member.objects.count() == 3


@pytest.mark.django_db
def test_remove_confirm_deletes_and_keeps_order(client, trio):
    ann, bob, cid = trio
    r = client.post(reverse("member_delete", args=[bob.pk]))
    assert r.status_code == 302 and r["Location"] == "/family/"
    assert names() == ["Ann", "Cid"]
    page = client.get("/family/").content.decode()
    assert "Bob" not in page


@pytest.mark.django_db
def test_removing_active_member_shows_who_are_you(client, trio):
    bob = trio[1]
    client.post(reverse("profile_switch", args=[bob.pk]))
    client.post(reverse("member_delete", args=[bob.pk]))
    assert "Who are you?" in client.get("/family/").content.decode()


@pytest.mark.django_db
def test_removing_last_member_shows_empty_state(client):
    only = make("Ann")
    client.post(reverse("member_delete", args=[only.pk]))
    assert "No family members yet" in client.get("/family/").content.decode()


@pytest.mark.django_db
def test_second_delete_post_is_404(client, trio):
    url = reverse("member_delete", args=[trio[1].pk])
    assert client.post(url).status_code == 302
    assert client.post(url).status_code == 404


# --- Hostile ids -----------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("param", ["edit", "remove"])
@pytest.mark.parametrize("value", HOSTILE)
def test_get_hostile_value_is_404(client, trio, param, value):
    assert client.get(f"/family/?{param}={value}").status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize("param", ["edit", "remove"])
def test_get_deleted_member_is_404(client, trio, param):
    gone = trio[1]
    gone.delete()
    assert client.get(f"/family/?{param}={gone.pk}").status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize("param", ["edit", "remove"])
def test_get_repeated_param_is_404(client, trio, param):
    ann, bob, _ = trio
    assert client.get(f"/family/?{param}={ann.pk}&{param}={bob.pk}").status_code == 404
    assert client.get(f"/family/?{param}={ann.pk}&{param}={ann.pk}").status_code == 404


@pytest.mark.django_db
def test_get_edit_and_remove_together_is_400(client, trio):
    pk = trio[0].pk
    assert client.get(f"/family/?edit={pk}&remove={pk}").status_code == 400


@pytest.mark.django_db
def test_unknown_params_ignored_and_plain_get_unchanged(client, trio):
    assert client.get("/family/?foo=bar").status_code == 200
    html = client.get("/family/").content.decode()
    assert 'member-view" hidden' not in html
    assert "Their chores will become unassigned.</small>" not in html


@pytest.mark.django_db
@pytest.mark.parametrize("headers", [{}, HTMX])
@pytest.mark.parametrize("pk", ["424242", HUGE])
@pytest.mark.parametrize("route", ["member_edit", "member_move", "member_delete"])
def test_post_unknown_or_huge_id_is_404(client, trio, route, pk, headers):
    from django.urls import resolve

    paths = {
        "member_edit": f"/family/{pk}/edit/",
        "member_move": f"/family/{pk}/move/?dir=up",
        "member_delete": f"/family/{pk}/delete/",
    }
    assert resolve(paths[route].split("?")[0]).url_name == route
    data = {"name": "X", "role": "adult", "avatar": "🐶"}
    assert client.post(paths[route], data, **headers).status_code == 404
    assert Member.objects.count() == 3


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path", ["/family/add/", "/family/{pk}/edit/", "/family/{pk}/move/?dir=up", "/family/{pk}/delete/"]
)
def test_get_on_post_only_urls_is_405(client, trio, path):
    assert client.get(path.format(pk=trio[0].pk)).status_code == 405
