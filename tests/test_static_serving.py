from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import Client, override_settings

CSS_URL = "/static/chores/style.css"
CSS_FILE = Path(settings.BASE_DIR) / "chores" / "static" / "chores" / "style.css"


def body(response):
    if response.streaming:
        return b"".join(response.streaming_content)
    return response.content


def test_middleware_follows_security_middleware():
    assert settings.MIDDLEWARE[0] == "django.middleware.security.SecurityMiddleware"
    assert settings.MIDDLEWARE[1] == "whitenoise.middleware.WhiteNoiseMiddleware"


def test_static_root_is_staticfiles_under_base_dir():
    assert settings.STATIC_ROOT == settings.BASE_DIR / "staticfiles"


def test_collected_css_served_with_debug_off(tmp_path):
    with override_settings(DEBUG=False, STATIC_ROOT=tmp_path):
        call_command("collectstatic", interactive=False, verbosity=0)
        response = Client().get(CSS_URL)
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/css")
        assert body(response) == CSS_FILE.read_bytes()


def test_css_is_404_with_debug_off_and_no_collectstatic(tmp_path):
    with override_settings(DEBUG=False, STATIC_ROOT=tmp_path):
        assert Client().get(CSS_URL).status_code == 404


def test_css_served_with_debug_on_and_no_collectstatic(tmp_path):
    with override_settings(DEBUG=True, STATIC_ROOT=tmp_path):
        response = Client().get(CSS_URL)
        assert response.status_code == 200
        assert body(response) == CSS_FILE.read_bytes()
