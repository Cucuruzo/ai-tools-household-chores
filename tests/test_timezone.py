import datetime
import re
import zoneinfo
from unittest import mock

import pytest
from django.conf import settings
from django.http import HttpResponse
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from chores import middleware
from chores.middleware import ViewerTimeZoneMiddleware
from chores.models import Chore, ChoreOccurrence, Member

UTC = datetime.timezone.utc
NOON = datetime.datetime(2026, 11, 21, 12, 5, tzinfo=UTC)  # Sat 21 Nov 2026, 12:05 UTC
CLOCK = re.compile(r'<time datetime="([^"]+)">([^<]+)</time>')


@pytest.fixture
def client():
    return Client()


def at(instant):
    return mock.patch("django.utils.timezone.now", return_value=instant)


def header_clock(client, tz=None, instant=NOON, url=None):
    """(datetime attribute, text) of the header clock on a full page."""
    if tz is not None:
        client.cookies["tz"] = tz
    with at(instant):
        response = client.get(url or reverse("family"))
    assert response.status_code == 200
    return CLOCK.search(response.content.decode()).groups()


# --- Settings ----------------------------------------------------------------


def test_settings_use_tz_and_utc():
    assert settings.USE_TZ is True
    assert settings.TIME_ZONE == "UTC"


def test_middleware_installed_after_session_middleware():
    mw = settings.MIDDLEWARE
    name = "chores.middleware.ViewerTimeZoneMiddleware"
    assert name in mw
    assert mw.index(name) > mw.index("django.contrib.sessions.middleware.SessionMiddleware")


def test_zoneinfo_database_is_available():
    assert zoneinfo.available_timezones()


@pytest.mark.django_db
def test_datetime_field_is_saved_in_utc():
    chore = Chore.objects.create(title="Dishes", start_date=datetime.date(2026, 11, 21))
    vilnius = zoneinfo.ZoneInfo("Europe/Vilnius")
    occ = ChoreOccurrence.objects.create(
        chore=chore, date=datetime.date(2026, 11, 21), points=10,
        done_at=datetime.datetime(2026, 11, 21, 14, 5, tzinfo=vilnius),
    )
    with timezone.override(vilnius):
        occ.refresh_from_db()
    assert occ.done_at.utcoffset() == datetime.timedelta(0)
    assert occ.done_at == NOON


# --- Browser script ------------------------------------------------------------


@pytest.mark.django_db
def test_full_page_has_inline_tz_script(client):
    page = client.get(reverse("family")).content.decode()
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    [script] = [s for s in scripts if "resolvedOptions().timeZone" in s]
    assert '"tz="' in script
    assert "path=/; max-age=31536000; SameSite=Lax" in script
    assert "location.reload()" in script


@pytest.mark.django_db
def test_htmx_partial_has_no_script_or_clock(client):
    member = Member.objects.create(name="Ann", avatar="😀")
    client.cookies["tz"] = "Europe/Vilnius"
    response = client.post(
        reverse("member_edit", args=[member.pk]),
        {"name": "Ann", "role": "adult", "avatar": "😀"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    page = response.content.decode()
    assert "Ann" in page
    assert "resolvedOptions" not in page
    assert "<time" not in page


@pytest.mark.django_db
def test_middleware_never_sets_the_cookie(client):
    client.cookies["tz"] = "Europe/Vilnius"
    response = client.get(reverse("family"))
    assert "tz" not in response.cookies


# --- Middleware: valid zones ------------------------------------------------------


def test_available_zones_computed_once():
    middleware.available_zones.cache_clear()
    with mock.patch("zoneinfo.available_timezones", wraps=zoneinfo.available_timezones) as spy:
        factory = RequestFactory()
        mw = ViewerTimeZoneMiddleware(lambda request: HttpResponse())
        for _ in range(3):
            request = factory.get("/")
            request.COOKIES["tz"] = "Europe/Vilnius"
            mw(request)
    assert spy.call_count == 1


def test_valid_cookie_activates_zone_during_request():
    seen = []

    def view(request):
        seen.append(timezone.get_current_timezone_name())
        return HttpResponse()

    request = RequestFactory().get("/")
    request.COOKIES["tz"] = "Europe/Vilnius"
    ViewerTimeZoneMiddleware(view)(request)
    assert seen == ["Europe/Vilnius"]


@pytest.mark.django_db
def test_deactivated_after_request(client):
    client.cookies["tz"] = "Pacific/Kiritimati"
    assert client.get(reverse("family")).status_code == 200
    assert timezone.get_current_timezone_name() == "UTC"


def test_deactivated_when_view_raises():
    def view(request):
        assert timezone.get_current_timezone_name() == "Pacific/Kiritimati"
        raise RuntimeError("boom")

    request = RequestFactory().get("/")
    request.COOKIES["tz"] = "Pacific/Kiritimati"
    with pytest.raises(RuntimeError):
        ViewerTimeZoneMiddleware(view)(request)
    assert timezone.get_current_timezone_name() == "UTC"


# --- Header clock --------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "tz, iso, text",
    [
        (None, "2026-11-21T12:05:00+00:00", "Sat 21 Nov, 12:05"),
        ("UTC", "2026-11-21T12:05:00+00:00", "Sat 21 Nov, 12:05"),
        ("Europe/Vilnius", "2026-11-21T14:05:00+02:00", "Sat 21 Nov, 14:05"),
        ("Pacific/Kiritimati", "2026-11-22T02:05:00+14:00", "Sun 22 Nov, 02:05"),
        ("Pacific/Pago_Pago", "2026-11-21T01:05:00-11:00", "Sat 21 Nov, 01:05"),
    ],
    ids=["no-cookie", "utc", "vilnius", "kiritimati", "pago-pago"],
)
def test_header_clock_in_viewer_zone(client, tz, iso, text):
    assert header_clock(client, tz) == (iso, text)


@pytest.mark.django_db
def test_header_clock_on_chore_form_page(client):
    assert header_clock(client, "Europe/Vilnius", url=reverse("chore_create"))[1] == "Sat 21 Nov, 14:05"


# --- Middleware: invalid cookies fall back to UTC --------------------------------------

UTC_CLOCK = ("2026-11-21T12:05:00+00:00", "Sat 21 Nov, 12:05")


@pytest.mark.django_db
def test_no_cookie_is_utc(client):
    assert header_clock(client) == UTC_CLOCK


@pytest.mark.django_db
def test_empty_cookie_is_utc(client):
    assert header_clock(client, "") == UTC_CLOCK


@pytest.mark.django_db
def test_unknown_zone_is_utc(client):
    assert header_clock(client, "Mars/Base") == UTC_CLOCK


@pytest.mark.django_db
def test_script_tag_cookie_is_utc_and_not_echoed(client):
    value = "<script>alert(1)</script>"
    client.cookies["tz"] = value
    with at(NOON):
        response = client.get(reverse("family"))
    assert response.status_code == 200
    body = response.content.decode()
    assert CLOCK.search(body).groups() == UTC_CLOCK
    assert "alert(1)" not in body
    assert value not in body
    assert "&lt;script&gt;alert(1)" not in body


@pytest.mark.django_db
def test_very_long_cookie_is_utc(client):
    assert header_clock(client, "A" * 10_000) == UTC_CLOCK


@pytest.mark.django_db
def test_wrong_case_zone_is_utc(client):
    assert header_clock(client, "europe/vilnius") == UTC_CLOCK


@pytest.mark.django_db
def test_path_traversal_cookie_is_utc(client):
    assert header_clock(client, "../../etc/passwd") == UTC_CLOCK


@pytest.mark.django_db
def test_trailing_space_zone_is_utc(client):
    assert header_clock(client, "Europe/Vilnius ") == UTC_CLOCK


# --- DST (Europe/Vilnius) -----------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "instant, iso, text",
    [
        ("2026-10-25T00:30:00", "2026-10-25T03:30:00+03:00", "Sun 25 Oct, 03:30"),
        ("2026-10-25T01:30:00", "2026-10-25T03:30:00+02:00", "Sun 25 Oct, 03:30"),
    ],
    ids=["before-fall-back", "after-fall-back"],
)
def test_dst_autumn_change(client, instant, iso, text):
    when = datetime.datetime.fromisoformat(instant).replace(tzinfo=UTC)
    assert header_clock(client, "Europe/Vilnius", when) == (iso, text)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "instant, iso, text",
    [
        ("2027-03-28T00:59:00", "2027-03-28T02:59:00+02:00", "Sun 28 Mar, 02:59"),
        ("2027-03-28T01:00:00", "2027-03-28T04:00:00+03:00", "Sun 28 Mar, 04:00"),
    ],
    ids=["before-spring-forward", "after-spring-forward"],
)
def test_dst_spring_change(client, instant, iso, text):
    when = datetime.datetime.fromisoformat(instant).replace(tzinfo=UTC)
    assert header_clock(client, "Europe/Vilnius", when) == (iso, text)


# --- Code that reads "today" --------------------------------------------------------------


def start_date_default(client, tz=None):
    if tz is not None:
        client.cookies["tz"] = tz
    with at(NOON):
        page = client.get(reverse("chore_create")).content.decode()
    return re.search(r'<input type="date" name="start_date" value="([^"]*)"', page).group(1)


@pytest.mark.django_db
def test_form_default_start_date_follows_viewer_zone(client):
    assert start_date_default(client, "Pacific/Kiritimati") == "2026-11-22"


@pytest.mark.django_db
def test_form_default_start_date_without_cookie_is_utc(client):
    assert start_date_default(client) == "2026-11-21"
