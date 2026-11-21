import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from chores.models import AVATAR_CHOICES, Member

HTMX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def client():
    return Client()


def make(name, role="adult", avatar="😀"):
    return Member.objects.create(name=name, role=role, avatar=avatar)


def names_in_order():
    return list(Member.objects.values_list("name", flat=True))


# --- Model -----------------------------------------------------------------


def test_avatar_choices_has_at_least_12_emoji():
    assert len(AVATAR_CHOICES) >= 12


@pytest.mark.django_db
def test_new_members_get_max_order_plus_one():
    a = make("Ann")
    b = make("Bob")
    b.order = 10
    b.save()
    c = make("Cid")
    assert a.order == 0
    assert c.order == 11


@pytest.mark.django_db
def test_model_rejects_invalid_avatar():
    member = Member(name="Ann", role="adult", avatar="X")
    with pytest.raises(ValidationError):
        member.full_clean()


@pytest.mark.django_db
def test_member_registered_in_admin():
    from django.contrib import admin

    assert Member in admin.site._registry


# --- List ------------------------------------------------------------------


@pytest.mark.django_db
def test_family_empty_state(client):
    response = client.get(reverse("family"))
    assert response.status_code == 200
    assert "No family members yet" in response.content.decode()
    assert 'id="add-member-form"' in response.content.decode()


@pytest.mark.django_db
def test_family_lists_members_sorted_by_order(client):
    make("Zed")
    Member.objects.filter(name="Zed").update(order=5)
    make("Amy", role="kid", avatar="👧")
    Member.objects.filter(name="Amy").update(order=1)
    response = client.get(reverse("family"))
    html = response.content.decode()
    assert html.index("Amy") < html.index("Zed")
    assert "👧" in html and "Kid" in html and "Adult" in html
    assert "No family members yet" not in html


@pytest.mark.django_db
def test_first_member_has_no_up_and_last_has_no_down(client):
    first, middle, last = make("Ann"), make("Bob"), make("Cid")
    html = client.get(reverse("family")).content.decode()
    assert f"{first.pk}/move/?dir=up" not in html
    assert f"{first.pk}/move/?dir=down" in html
    assert f"{middle.pk}/move/?dir=up" in html
    assert f"{middle.pk}/move/?dir=down" in html
    assert f"{last.pk}/move/?dir=up" in html
    assert f"{last.pk}/move/?dir=down" not in html


@pytest.mark.django_db
def test_remove_button_asks_for_confirmation(client):
    make("Emma")
    html = client.get(reverse("family")).content.decode()
    assert 'hx-confirm="Remove Emma? Their chores will become unassigned."' in html


# --- Add -------------------------------------------------------------------


@pytest.mark.django_db
def test_add_valid_member_goes_to_bottom(client):
    make("Ann")
    response = client.post(
        reverse("member_add"), {"name": "  Bob  ", "role": "kid", "avatar": "👦"}, **HTMX
    )
    assert response.status_code == 200
    bob = Member.objects.get(name="Bob")  # trimmed
    assert bob.role == "kid"
    assert bob.order == Member.objects.get(name="Ann").order + 1
    html = response.content.decode()
    assert html.index("Ann") < html.index("Bob")


@pytest.mark.django_db
def test_add_without_htmx_redirects(client):
    response = client.post(
        reverse("member_add"), {"name": "Bob", "role": "kid", "avatar": "👦"}
    )
    assert response.status_code == 302
    assert Member.objects.count() == 1


@pytest.mark.django_db
def test_add_duplicate_name_allowed(client):
    make("Sam")
    client.post(reverse("member_add"), {"name": "Sam", "role": "adult", "avatar": "😀"}, **HTMX)
    assert Member.objects.filter(name="Sam").count() == 2


@pytest.mark.django_db
@pytest.mark.parametrize(
    "data",
    [
        {"name": "", "role": "adult", "avatar": "😀"},
        {"name": "   ", "role": "adult", "avatar": "😀"},
        {"name": "x" * 51, "role": "adult", "avatar": "😀"},
        {"name": "Ann", "role": "adult", "avatar": "X"},
        {"name": "Ann", "role": "boss", "avatar": "😀"},
    ],
    ids=["blank", "whitespace", "too-long", "bad-avatar", "bad-role"],
)
def test_add_invalid_shows_error_and_creates_nothing(client, data):
    response = client.post(reverse("member_add"), data, **HTMX)
    assert response.status_code == 200
    assert 'class="errorlist"' in response.content.decode()
    assert Member.objects.count() == 0


@pytest.mark.django_db
def test_add_name_of_50_chars_is_accepted(client):
    client.post(reverse("member_add"), {"name": "x" * 50, "role": "adult", "avatar": "😀"}, **HTMX)
    assert Member.objects.count() == 1


# --- Edit ------------------------------------------------------------------


@pytest.mark.django_db
def test_edit_valid_returns_updated_row(client):
    m = make("Ann")
    response = client.post(
        reverse("member_edit", args=[m.pk]),
        {"name": " Annie ", "role": "kid", "avatar": "🦊"},
        **HTMX,
    )
    assert response.status_code == 200
    m.refresh_from_db()
    assert (m.name, m.role, m.avatar) == ("Annie", "kid", "🦊")
    html = response.content.decode()
    assert f'id="member-{m.pk}"' in html
    assert "Annie" in html
    assert '<form class="member-edit" hidden' in html


@pytest.mark.django_db
def test_edit_invalid_keeps_form_open_with_error(client):
    m = make("Ann")
    order_before = m.order
    response = client.post(
        reverse("member_edit", args=[m.pk]), {"name": "", "role": "adult", "avatar": "😀"}, **HTMX
    )
    assert response.status_code == 200
    html = response.content.decode()
    assert 'class="errorlist"' in html
    assert '<div class="member-view" hidden' in html
    m.refresh_from_db()
    assert m.name == "Ann"
    assert m.order == order_before


# --- Move ------------------------------------------------------------------


@pytest.mark.django_db
def test_move_up_swaps_with_previous(client):
    a, b, c = make("Ann"), make("Bob"), make("Cid")
    response = client.post(reverse("member_move", args=[c.pk]) + "?dir=up", **HTMX)
    assert response.status_code == 200
    assert names_in_order() == ["Ann", "Cid", "Bob"]
    html = response.content.decode()
    assert html.index("Cid") < html.index("Bob")


@pytest.mark.django_db
def test_move_down_swaps_with_next(client):
    a, b, c = make("Ann"), make("Bob"), make("Cid")
    client.post(reverse("member_move", args=[a.pk]) + "?dir=down", **HTMX)
    assert names_in_order() == ["Bob", "Ann", "Cid"]


@pytest.mark.django_db
def test_move_up_on_first_is_noop(client):
    a, b = make("Ann"), make("Bob")
    response = client.post(reverse("member_move", args=[a.pk]) + "?dir=up", **HTMX)
    assert response.status_code == 200
    assert names_in_order() == ["Ann", "Bob"]


@pytest.mark.django_db
def test_move_down_on_last_is_noop(client):
    a, b = make("Ann"), make("Bob")
    response = client.post(reverse("member_move", args=[b.pk]) + "?dir=down", **HTMX)
    assert response.status_code == 200
    assert names_in_order() == ["Ann", "Bob"]


@pytest.mark.django_db
def test_move_with_tied_orders_still_moves(client):
    a, b = make("Ann"), make("Bob")
    Member.objects.update(order=3)
    client.post(reverse("member_move", args=[b.pk]) + "?dir=up", **HTMX)
    assert names_in_order() == ["Bob", "Ann"]


@pytest.mark.django_db
def test_move_bad_direction_is_400(client):
    a = make("Ann")
    response = client.post(reverse("member_move", args=[a.pk]) + "?dir=sideways")
    assert response.status_code == 400


# --- Delete ----------------------------------------------------------------


@pytest.mark.django_db
def test_delete_removes_member_and_keeps_relative_order(client):
    a, b, c = make("Ann"), make("Bob"), make("Cid")
    response = client.post(reverse("member_delete", args=[b.pk]), **HTMX)
    assert response.status_code == 200
    assert names_in_order() == ["Ann", "Cid"]
    assert "Bob" not in response.content.decode()


@pytest.mark.django_db
def test_delete_last_member_shows_empty_state(client):
    a = make("Ann")
    response = client.post(reverse("member_delete", args=[a.pk]), **HTMX)
    assert "No family members yet" in response.content.decode()


# --- 404 / 405 -------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["member_edit", "member_move", "member_delete"])
def test_unknown_id_is_404(client, url):
    response = client.post(
        reverse(url, args=[999]) + "?dir=up", {"name": "X", "role": "adult", "avatar": "😀"}
    )
    assert response.status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url",
    [
        reverse("member_add"),
        reverse("member_edit", args=[1]),
        reverse("member_move", args=[1]) + "?dir=up",
        reverse("member_delete", args=[1]),
    ],
)
def test_get_on_post_only_urls_is_405(client, url):
    make("Ann")
    assert client.get(url).status_code == 405
