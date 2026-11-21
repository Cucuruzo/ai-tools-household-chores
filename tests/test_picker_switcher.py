"""After a template pick, the header switcher's ``next`` follows the pushed URL (#44).

The pick is ``hx-get`` of a link URL with ``hx-select-oob="#profile-switcher"``: htmx
takes the nav from the full-page response, so these tests check the markup and that
response (the browser swap itself is htmx's).
"""
import datetime
import html
import re
from unittest import mock

import pytest
from django.test import Client

from chores.models import Chore, Member

pytestmark = pytest.mark.django_db

WED = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=datetime.timezone.utc)
HTMX = {"HTTP_HX_REQUEST": "true"}
OOB = 'hx-select-oob="#profile-switcher"'
NAV = re.compile(r'<nav id="profile-switcher"[^>]*>.*?</nav>', re.S)
NEXT = re.compile(r'name="next" value="([^"]*)"')
PICKER = re.compile(r'<section id="template-picker">.*?</section>', re.S)
LINK = re.compile(r"<a [^>]*>", re.S)
CREATE = "/chores/create/"


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def mom():
    return Member.objects.create(name="Mom", role="parent", avatar="M", order=1)


@pytest.fixture
def emma(mom):
    return Member.objects.create(name="Emma", role="kid", avatar="E", order=2)


def template(title, **kwargs):
    values = dict(title=title, start_date=datetime.date(2026, 1, 1), recurrence="weekly",
                  recurrence_day=2, assignment_mode="unassigned", points=20, is_template=True)
    values.update(kwargs)
    return Chore.objects.create(**values)


@pytest.fixture
def mop():
    return template("Mop")


@pytest.fixture
def wash():
    return template("Wash", is_builtin=True)


def TOTAL():
    """Every template on the page, the seeded built-in ones included."""
    return Chore.objects.filter(is_template=True).count()


def get(client, url, **extra):
    with mock.patch("django.utils.timezone.now", return_value=WED):
        return client.get(url, **extra)


def body(client, url, **extra):
    response = get(client, url, **extra)
    assert response.status_code == 200
    return response.content.decode()


def blank(page):
    return re.sub(r"[A-Za-z0-9]{32,}", "", page)


def nexts(page):
    return [html.unescape(v) for v in NEXT.findall(page)]


def picker_links(page):
    section = PICKER.search(page).group(0)
    links = [a for a in LINK.findall(section) if 'href="/chores/create/?template=' in a]
    assert links
    return links


def attr(tag, name):
    return html.unescape(re.search(rf'\b{name}="([^"]*)"', tag).group(1))


def pick(client, mop_pk, start="", **extra):
    """The picker link on ``start`` for the template, and the HTMX response to following it."""
    links = [a for a in picker_links(body(client, CREATE + start)) if f"template={mop_pk}" in a]
    assert len(links) == 1
    url = attr(links[0], "hx-get")
    return url, get(client, url, **extra)


def only_next(response):
    assert response.status_code == 200
    page = response.content.decode()
    assert page.count('<nav id="profile-switcher"') == 1
    assert "hx-swap-oob" not in page
    found = set(nexts(page))
    assert len(found) == 1 and len(nexts(page)) >= 1
    return found.pop()


# Markup

def test_every_picker_link_has_oob_once_and_manage_link_does_not(client, emma, mop, wash):
    page = body(client, CREATE)
    section = PICKER.search(page).group(0)
    links = picker_links(page)
    assert len(links) == TOTAL()
    for a in links:
        assert a.count(OOB) == 1
        assert attr(a, "href") == attr(a, "hx-get")
        assert a.index("href=") < a.index("hx-get=") < a.index("hx-select-oob=")
        assert re.search(r'href="[^"]*"\s+hx-get="', a)
        assert attr(a, "hx-target") == "#chore-form-area"
        assert attr(a, "hx-select") == "#chore-form-area"
        assert attr(a, "hx-swap") == "outerHTML"
        assert attr(a, "hx-push-url") == "true"
    manage = [a for a in LINK.findall(section) if "template" in a and "?template=" not in a]
    assert manage and all("hx-select-oob" not in a for a in manage)
    assert page.count("hx-select-oob") == TOTAL()
    assert "hx-swap-oob" not in page


def test_removing_the_attribute_leaves_the_rest(client, emma, mop):
    page = body(client, CREATE)
    stripped = page.replace('               ' + OOB + "\n", "")
    assert "hx-select-oob" not in stripped
    # Same markup otherwise: the attribute sits on its own line, nothing else moved.
    assert blank(stripped).count("<nav id=\"profile-switcher\"") == 1


@pytest.mark.parametrize("query", ["", "?date=2026-11-18&return_to=dashboard", "?recurrence=weekly",
                                   "?date=abc", "?template=999999"])
def test_oob_count_equals_templates(client, emma, mop, wash, query):
    assert body(client, CREATE + query).count("hx-select-oob") == TOTAL() > 2


def test_no_templates_no_picker(client, emma):
    Chore.objects.filter(is_template=True).delete()
    page = body(client, CREATE)
    assert "hx-select-oob" not in page and "template-picker" not in page


# After a pick: the HTMX response

def test_pick_keeps_date_and_dashboard_return(client, emma, mop):
    url, response = pick(client, mop.pk, "?date=2026-11-18&return_to=dashboard", **HTMX)
    assert url == f"{CREATE}?template={mop.pk}&date=2026-11-18&return_to=dashboard"
    assert only_next(response) == f"{CREATE}?date=2026-11-18&template={mop.pk}&return_to=dashboard"


def test_pick_from_plain_create(client, emma, mop):
    url, response = pick(client, mop.pk, **HTMX)
    assert url == f"{CREATE}?template={mop.pk}"
    assert only_next(response) == f"{CREATE}?template={mop.pk}"


def test_pick_keeps_dashboard_filters(client, emma, mop):
    start = f"?date=2026-11-18&return_to=dashboard&week=2026-W48&member={emma.pk}&day=3"
    _, response = pick(client, mop.pk, start, **HTMX)
    assert only_next(response) == (
        f"{CREATE}?date=2026-11-18&template={mop.pk}&return_to=dashboard"
        f"&week=2026-W48&member={emma.pk}&day=3")


def test_pick_drops_recurrence(client, emma, mop):
    links = picker_links(body(client, CREATE + "?recurrence=weekly"))
    assert all("recurrence" not in attr(a, "hx-get") for a in links)
    _, response = pick(client, mop.pk, "?recurrence=weekly", **HTMX)
    assert only_next(response) == f"{CREATE}?template={mop.pk}"


def test_pick_with_invalid_date(client, emma, mop):
    links = picker_links(body(client, CREATE + "?date=abc"))
    assert all("date" not in attr(a, "hx-get") for a in links)
    _, response = pick(client, mop.pk, "?date=abc", **HTMX)
    assert only_next(response) == f"{CREATE}?template={mop.pk}"


def test_second_pick_replaces_next(client, emma, mop, wash):
    _, first = pick(client, mop.pk, **HTMX)
    assert only_next(first) == f"{CREATE}?template={mop.pk}"
    # The links on the swapped-in page are the same ones: pick the other one.
    _, second = pick(client, wash.pk, **HTMX)
    assert only_next(second) == f"{CREATE}?template={wash.pk}"


def test_template_deleted_after_load_gives_blank_form(client, emma, mop, wash):
    links = picker_links(body(client, CREATE + "?date=2026-11-18"))
    url = attr([a for a in links if f"template={mop.pk}" in a][0], "hx-get")
    mop.delete()
    response = get(client, url, **HTMX)
    assert only_next(response) == f"{CREATE}?date=2026-11-18"
    assert only_next(get(client, url)) == f"{CREATE}?date=2026-11-18"


def test_active_member_stays_marked_and_order_kept(client, mom, emma, mop):
    before = NAV.search(body(client, CREATE)).group(0)
    assert "Who are you?" in before and "aria-current" not in before
    client.post(f"/profile/{emma.pk}/", {"next": CREATE})
    before = NAV.search(body(client, CREATE)).group(0)
    assert before.count('aria-current="true"') == 1 and "Who are you?" not in before
    _, response = pick(client, mop.pk, **HTMX)
    after = NAV.search(response.content.decode()).group(0)
    assert after.count('aria-current="true"') == 1 and "Who are you?" not in after
    assert re.search(r'aria-current="true">\s*<span class="avatar">E</span>', after)
    assert re.findall(r'class="avatar">(.)<', before) == re.findall(r'class="avatar">(.)<', after) == ["M", "E"]


def test_who_are_you_still_shown_when_no_active_member(client, mom, emma, mop):
    _, response = pick(client, mop.pk, **HTMX)
    assert "Who are you?" in NAV.search(response.content.decode()).group(0)


def test_htmx_nav_equals_plain_nav(client, emma, mop):
    url, response = pick(client, mop.pk, "?date=2026-11-18", **HTMX)
    plain = body(client, url)
    nav = NAV.findall(response.content.decode())
    assert len(nav) == 1 and "hx-swap-oob" not in nav[0]
    assert blank(nav[0]) == blank(NAV.search(plain).group(0))


def test_create_page_ignores_hx_request(client, emma, mop):
    for q in ("", f"?template={mop.pk}", "?date=2026-11-18&return_to=dashboard"):
        assert blank(body(client, CREATE + q, **HTMX)) == blank(body(client, CREATE + q))


def test_switch_after_pick_prefills_template(client, mom, emma, mop):
    _, response = pick(client, mop.pk, **HTMX)
    page = response.content.decode()
    nxt = only_next(response)
    nav = NAV.search(page).group(0)
    assert nxt == f"{CREATE}?template={mop.pk}"
    form = [f for f in re.findall(r"<form.*?</form>", nav, re.S) if f"/profile/{emma.pk}/" in f][0]
    switched = client.post(attr(form, "action"), {"next": nxt})
    assert switched.status_code == 302 and switched["Location"] == nxt
    landed = body(client, nxt)
    assert 'value="Mop"' in landed and 'aria-current="true"' in NAV.search(landed).group(0)


# No members

def test_no_members(client, mop):
    page = body(client, CREATE)
    assert "Add your family" in page and "name=\"next\"" not in page
    nav = NAV.search(page).group(0)
    assert "<form" not in nav
    _, response = pick(client, mop.pk, **HTMX)
    after = response.content.decode()
    nav = NAV.search(after).group(0)
    assert "Add your family" in nav and "<form" not in nav and 'name="next"' not in after


# No JS and other pages

def test_no_js_link_is_a_plain_href(client, emma, mop):
    page = body(client, CREATE + "?date=2026-11-18")
    mop_link = [a for a in picker_links(page) if f"template={mop.pk}&" in a][0]
    url = attr(mop_link, "href")
    assert url == f"{CREATE}?template={mop.pk}&date=2026-11-18"
    assert only_next(get(client, url)) == f"{CREATE}?date=2026-11-18&template={mop.pk}"


def test_edit_and_other_pages_have_no_picker_oob(client, emma, mop):
    chore = Chore.objects.create(title="Dishes", start_date=datetime.date(2026, 11, 1),
                                 recurrence="once")
    for url in (f"/chores/{chore.pk}/edit/", "/chores/templates/", "/", "/calendar/"):
        response = get(client, url)
        if response.status_code == 200:
            assert "hx-select-oob" not in response.content.decode(), url
    assert "hx-select-oob" not in body(client, f"/chores/{chore.pk}/edit/")
