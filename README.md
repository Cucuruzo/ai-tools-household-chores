# ai-tools-household-chores

## Running

Development:

```
uv sync
uv run python manage.py runserver
```

No `collectstatic` is needed; the stylesheet is served straight from the app's `static/` folder.

Without `runserver` (for example under gunicorn):

```
uv run python manage.py collectstatic --noinput
uv run --with gunicorn gunicorn ai_tools_household_chores.wsgi
```

Re-run `collectstatic` after any change under `static/`. `--with` uses gunicorn for that one run without adding it to the project.
