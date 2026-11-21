"""Run each request in the viewer's time zone (#26).

The browser script in ``base.html`` writes the IANA zone into the ``tz``
cookie. Only names in ``zoneinfo.available_timezones()`` are accepted;
anything else (missing, empty, unknown, wrong case, padded) means UTC.
This middleware only reads the cookie, it never sets or deletes it.
"""
import functools
import zoneinfo

from django.utils import timezone

COOKIE_NAME = "tz"


@functools.cache
def available_zones():
    return frozenset(zoneinfo.available_timezones())


def viewer_zone(request):
    """The ZoneInfo named by the ``tz`` cookie, or None if it is not usable."""
    value = request.COOKIES.get(COOKIE_NAME)
    if not value or value not in available_zones():
        return None
    try:
        return zoneinfo.ZoneInfo(value)
    except (ValueError, OSError, zoneinfo.ZoneInfoNotFoundError):
        return None


class ViewerTimeZoneMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        zone = viewer_zone(request)
        try:
            if zone is None:
                timezone.deactivate()  # TIME_ZONE = "UTC"
            else:
                timezone.activate(zone)
            return self.get_response(request)
        finally:
            timezone.deactivate()
