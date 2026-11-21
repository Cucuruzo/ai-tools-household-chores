import datetime
import secrets

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.db.models.functions import Lower
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import dashboard as dash
from . import month, undo
from .forms import ChoreForm, MemberForm, OccurrenceForm, find_template
from .models import Chore, ChoreOccurrence, Member
from .profile import (
    MAX_PK,
    _parse_pk,
    clear_active_member,
    get_active_member,
    is_valid_pk,
    set_active_member,
)
from .services import (
    ASSIGNMENT_FIELDS, RULE_FIELDS, FutureEditRefused, MoveRefused, OneOffEditRefused, _day_text,
    apply_future_edit, apply_one_off_edit, cancel_projected_date, edit_projected_date,
    eligible_pool, materialize_one_off,
    materialize_week, move_occurrence, move_projected_date, project_occurrences,
    projected_occurrence, set_occurrence_status, set_projected_member, update_occurrence,
)


@require_GET
def home(request):
    return redirect("dashboard")


# --- Dashboard (#10) -----------------------------------------------------------


class _PoolCache:
    """Whether each chore's eligible pool is empty now, one query per chore."""

    def __init__(self):
        self._empty = {}

    def empty(self, chore):
        if chore.pk not in self._empty:
            self._empty[chore.pk] = not eligible_pool(chore)
        return self._empty[chore.pk]


def _note(pools, chore, mode, member, is_override):
    has_member = member is not None
    pool_empty = mode == Chore.AssignmentMode.ROTATING and not has_member and pools.empty(chore)
    return dash.assignee_note(mode, has_member, is_override, pool_empty)


def _stored_cards(occurrences, pools, today=None):
    from . import progress  # progress imports this module, so not at the top

    streaks = progress.chore_streaks(today) if today is not None else {}
    cards = []
    for occ in occurrences.select_related("chore", "member", "completed_by"):
        chore = occ.chore
        # The conditions /progress/ lists a chore under, plus 2 or more.
        streak = streaks.get(occ.chore_id, 0)
        if (chore.recurrence == Chore.Recurrence.ONCE or streak < 2
                or (chore.end_date is not None and chore.end_date < today)):
            streak = 0
        cards.append(dash.Card(
            pk=occ.pk, chore_id=occ.chore_id, title=occ.title, date=occ.date,
            due_time=occ.due_time, points=occ.points, status=occ.status, member=occ.member,
            assignee_note=_note(pools, occ.chore, occ.chore.assignment_mode, occ.member, occ.is_override),
            recurring=occ.chore.recurrence != Chore.Recurrence.ONCE,
            completed_by=occ.completed_by,
            streak=streak,
            instructions=occ.instructions,
        ))
    return cards


def _projected_cards(projected, pools):
    chores = Chore.objects.in_bulk({p["chore_id"] for p in projected})
    members = Member.objects.in_bulk({p["member_id"] for p in projected if p["member_id"]})
    cards = []
    for p in projected:
        member = members.get(p["member_id"])
        chore = chores[p["chore_id"]]
        cards.append(dash.Card(
            pk=None, chore_id=p["chore_id"], title=p["title"], date=p["date"],
            due_time=p["due_time"], points=p["points"], status=dash.PLANNED, member=member,
            assignee_note=_note(pools, chore, p["assignment_mode"], member, p["is_override"]),
            recurring=chore.recurrence != Chore.Recurrence.ONCE,
            instructions=p["instructions"],
        ))
    return cards


def _shown_occurrences():
    # Templates never get rows (#9); the filter keeps it that way on screen too.
    # Cancelled rows (#5) stay stored but are shown nowhere.
    return ChoreOccurrence.objects.filter(chore__is_template=False, is_cancelled=False)


def _dashboard_context(monday, today, now, member_filter=None, day_filter=None):
    nav = dash.week_nav(monday, today)
    pools = _PoolCache()
    week = Q(date__gte=nav["monday"], date__lte=nav["sunday"])
    context = {"nav": nav}
    if nav["is_current"]:
        # Same ``today`` for the Monday and the call, so this never raises.
        materialize_week(nav["monday"], today=today)
        rows = _shown_occurrences().filter(
            week | Q(status=ChoreOccurrence.Status.TODO, date__lt=nav["monday"])
        )
        everything = _stored_cards(rows, pools, today=today)
        # "No chores this week" is decided on the unfiltered week.
        context["is_empty"] = not any(dash.group_current_week(everything, today, now).values())
        shown = dash.filter_cards(everything, nav["monday"], member_filter, day_filter)
        sections = dash.group_current_week(shown, today, now)
        context["sections"] = [
            ("overdue", "Overdue", sections["overdue"]),
            ("today", "Today", sections["today"]),
            ("upcoming", "Upcoming", sections["upcoming"]),
        ]
        context["done_earlier"] = sections["done_earlier"]
    else:
        cards = _stored_cards(_shown_occurrences().filter(week), pools, today=today)
        if nav["is_past"]:
            dash.mark_past_overdue(cards)
        else:
            projected = project_occurrences(nav["monday"], nav["sunday"], today=today)
            cards += _projected_cards(projected, pools)
        context["is_empty"] = not cards
        shown = dash.filter_cards(cards, nav["monday"], member_filter, day_filter)
        context["cards"] = sorted(shown, key=dash.list_key)
    return context


def _filter_context(nav, member, day):
    """Normalized filter values and every dashboard link that carries them."""
    def url(week):
        return reverse("dashboard") + dash.dashboard_query(week, member, day)

    own_week = None if nav["is_current"] else dash.week_param(nav["monday"])
    # "+ Add Chore" (#19): the day picked by the filter, else today, else the shown Monday.
    if day is not None:
        add_date = nav["monday"] + datetime.timedelta(days=day - 1)
    elif nav["is_current"]:
        add_date = timezone.localdate()
    else:
        add_date = nav["monday"]
    add_query = dash.dashboard_query(own_week, member, day)
    add_chore_url = (
        f"{reverse('chore_create')}?date={add_date.isoformat()}&recurrence=once&return_to=dashboard"
        + ("&" + add_query[1:] if add_query else "")
    )
    return {
        "add_chore_url": add_chore_url,
        "filter_member": "" if member is None else member,
        "filter_day": day,
        "filter_week": own_week,
        "filter_days": [nav["monday"] + datetime.timedelta(days=i) for i in range(7)],
        "previous_url": url(nav["previous"]) if nav["previous"] else None,
        "next_url": url(nav["next"]) if nav["next"] else None,
        "this_week_url": url(None),
        "switcher_next": url(own_week),
        "return_query": dash.dashboard_query(own_week, member, day),
    }


def _dashboard_filters(params):
    """Normalized ``(monday or None, member, day)`` from GET or POST data.

    Unparsable values become None, and a member pk is dropped when that
    member does not exist, so callers only ever see safe values.
    """
    monday = dash.parse_week(params.get("week"))
    member = dash.parse_member_filter(params.get("member"), _parse_pk)
    if isinstance(member, int) and not Member.objects.filter(pk=member).exists():
        member = None
    return monday, member, dash.parse_day_filter(params.get("day"))


def _dashboard_redirect(params):
    """302 back to the dashboard view described by ``params`` (GET or POST data)."""
    monday, member, day = _dashboard_filters(params)
    week = dash.week_param(monday) if monday else None
    return redirect(reverse("dashboard") + dash.dashboard_query(week, member, day))


def _append_switcher_oob(request, response, next_url):
    """Out-of-band header switcher whose ``next`` is the view just swapped in."""
    response.content += render_to_string(
        "chores/_profile_switcher.html",
        {"switcher_oob": True, "switcher_next": next_url},
        request,
    ).encode()


@require_GET
def dashboard(request):
    # One clock read: "now" and "today" are the same instant in the viewer's
    # zone (#26); timezone.localdate(now) is timezone.localdate() for it.
    now = timezone.localtime()
    today = timezone.localdate(now)
    monday, member_filter, day_filter = _dashboard_filters(request.GET)
    monday = monday or dash.monday_of(today)
    context = _dashboard_context(monday, today, now, member_filter, day_filter)
    context.update(_filter_context(context["nav"], member_filter, day_filter))
    partial = _is_htmx(request) and request.headers.get("HX-History-Restore-Request") != "true"
    template = "chores/_dashboard_list.html" if partial else "chores/dashboard.html"
    if not partial:
        # The Undo button next to "Chore deleted" (#24); the slot is only read.
        token = undo.live_token(request.session.get(undo.SLOT), now)
        if token is not None:
            context["undo_token"] = token
    response = render(request, template, context)
    if partial:
        _append_switcher_oob(request, response, context["switcher_next"])
    # The same URL serves a partial or a full page; keep browser caches apart.
    patch_vary_headers(response, ["HX-Request", "HX-History-Restore-Request"])
    return response


# --- Monthly calendar (#14) ----------------------------------------------------


def _month_entries(nav, today):
    """``{date: [(status, due_time, title, chore_id)]}`` for the month shown."""
    entries = {}
    stored = _shown_occurrences().filter(date__gte=nav["first"], date__lte=nav["last"])
    for status, day, due_time, title, chore_id in stored.values_list(
        "status", "date", "due_time", "title", "chore_id"
    ):
        entries.setdefault(day, []).append(
            (month.dot_status(status, day, today), due_time, title, chore_id)
        )
    if not nav["is_past"]:
        for p in project_occurrences(nav["first"], nav["last"], today=today):
            entries.setdefault(p["date"], []).append(
                (dash.PLANNED, p["due_time"], p["title"], p["chore_id"])
            )
    return entries


def _month_url(first, today):
    """``/calendar/`` for the current month, else ``?month=`` from the parsed month."""
    if first == month.first_of_month(today):
        return reverse("calendar")
    return reverse("calendar") + "?month=" + month.month_param(first)


def _calendar_return(data, today):
    """None unless return_to=calendar (exactly); else the normalized month
    param: "" for the current month or a dropped value, else "YYYY-MM" (#36)."""
    if data.get("return_to") != "calendar":
        return None
    first = month.parse_month(data.get("month"), today)
    if first is None or first == month.first_of_month(today):
        return ""
    return month.month_param(first)


def _calendar_url(calendar):
    """``/calendar/`` plus ``?month=`` when ``calendar`` (a normalized month) is set."""
    url = reverse("calendar")
    return url + "?month=" + calendar if calendar else url


def _calendar_context(first, today):
    nav = month.month_nav(first, today)
    if nav["is_current"]:
        # Same ``today`` for the Monday and the call, so this never raises.
        materialize_week(dash.monday_of(today), today=today)
    entries = _month_entries(nav, today)
    dashboard_url = reverse("dashboard")
    create_url = reverse("chore_create")
    add_suffix = "&return_to=calendar" + ("" if nav["is_current"] else "&month=" + month.month_param(first))
    rows = []
    for week in month.grid_weeks(first):
        row = []
        for day in week:
            in_month = day.month == first.month
            cell = {"date": day, "in_month": in_month, "today": in_month and day == today}
            if in_month:
                cell.update(month.day_cell(entries.get(day, [])))
                cell["dashboard_url"] = dashboard_url + dash.dashboard_query(
                    dash.week_param(dash.monday_of(day)), None, day.isoweekday()
                )
                cell["add_url"] = f"{create_url}?date={day.isoformat()}{add_suffix}"
                cell["label"] = month.day_label(day)
            row.append(cell)
        rows.append(row)
    return {
        "nav": nav,
        "weekday_headers": month.WEEKDAY_HEADERS,
        "rows": rows,
        "is_empty": not entries,
        "previous_url": _month_url(month.shift_month(first, -1), today) if nav["previous"] else None,
        "next_url": _month_url(month.shift_month(first, 1), today) if nav["next"] else None,
        "this_month_url": reverse("calendar"),
        "create_url": create_url + "?" + add_suffix[1:],
        "switcher_next": _month_url(first, today),
    }


@require_GET
def calendar(request):
    today = timezone.localdate()
    first = month.parse_month(request.GET.get("month"), today) or month.first_of_month(today)
    context = _calendar_context(first, today)
    partial = _is_htmx(request) and request.headers.get("HX-History-Restore-Request") != "true"
    template = "chores/_calendar_grid.html" if partial else "chores/calendar.html"
    response = render(request, template, context)
    if partial:
        _append_switcher_oob(request, response, context["switcher_next"])
    patch_vary_headers(response, ["HX-Request", "HX-History-Restore-Request"])
    return response


def _member_form(member=None, data=None):
    """Edit forms get a per-member id prefix so several rows can coexist."""
    auto_id = f"id_m{member.pk}_%s" if member else "id_%s"
    return MemberForm(data, instance=member, auto_id=auto_id)


def _render_family(request, add_form=None, edit_id=None, edit_form=None, remove_id=None):
    members = list(Member.objects.all())
    for m in members:
        m.edit_form = edit_form if m.pk == edit_id and edit_form else _member_form(m)
        m.editing = m.pk == edit_id
        m.confirm_remove = m.pk == remove_id
    return render(
        request,
        "chores/family.html",
        {
            "members": members,
            "add_form": add_form or _member_form(),
            "switcher_next": reverse("family"),
        },
    )


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _family_pk_param(request, name):
    """Return a valid pk from ?<name>=, None if absent; 404 for anything hostile."""
    values = request.GET.getlist(name)
    if not values:
        return None
    pk = _parse_pk(values[0]) if len(values) == 1 else None
    if pk is None or not Member.objects.filter(pk=pk).exists():
        raise Http404
    return pk


@require_GET
def family(request):
    if "edit" in request.GET and "remove" in request.GET:
        return HttpResponseBadRequest("edit and remove cannot be combined")
    return _render_family(
        request,
        edit_id=_family_pk_param(request, "edit"),
        remove_id=_family_pk_param(request, "remove"),
    )


@require_POST
def member_add(request):
    form = _member_form(data=request.POST)
    if form.is_valid():
        form.save()
        if not _is_htmx(request):
            return redirect("family")
        return _render_family(request)
    return _render_family(request, add_form=form)


@require_POST
def member_edit(request, pk):
    if not is_valid_pk(pk):
        raise Http404
    member = get_object_or_404(Member, pk=pk)
    form = _member_form(member, request.POST)
    editing = not form.is_valid()
    if editing and not _is_htmx(request):
        return _render_family(request, edit_id=pk, edit_form=form)
    if not editing and not _is_htmx(request):
        form.save()
        return redirect("family")
    if not editing:
        member = form.save()
        form = _member_form(member)
    others = Member.objects.exclude(pk=member.pk)
    context = {
        "member": member,
        "form": form,
        "editing": editing,
        "is_first": not others.filter(order__lt=member.order).exists(),
        "is_last": not others.filter(order__gt=member.order).exists(),
    }
    response = render(request, "chores/_member_row.html", context)
    if not editing:
        # Out-of-band swap so the header switcher shows the new name/avatar.
        response.content += render_to_string(
            "chores/_profile_switcher.html",
            {"switcher_oob": True, "switcher_next": reverse("family")},
            request,
        ).encode()
    return response


@require_POST
def member_move(request, pk):
    if not is_valid_pk(pk):
        raise Http404
    member = get_object_or_404(Member, pk=pk)
    direction = request.GET.get("dir")
    if direction not in ("up", "down"):
        return HttpResponseBadRequest("dir must be 'up' or 'down'")

    with transaction.atomic():
        members = list(Member.objects.select_for_update())
        index = next(i for i, m in enumerate(members) if m.pk == member.pk)
        neighbour_index = index - 1 if direction == "up" else index + 1
        if 0 <= neighbour_index < len(members):
            neighbour = members[neighbour_index]
            if neighbour.order != member.order:
                member.order, neighbour.order = neighbour.order, member.order
                Member.objects.bulk_update([member, neighbour], ["order"])
            else:
                # Tied orders (e.g. edited in admin): renumber so the swap sticks.
                members[index], members[neighbour_index] = neighbour, members[index]
                for i, m in enumerate(members):
                    m.order = i
                Member.objects.bulk_update(members, ["order"])

    if not _is_htmx(request):
        return redirect("family")
    return _render_family(request)


@require_POST
def member_delete(request, pk):
    if not is_valid_pk(pk):
        raise Http404
    get_object_or_404(Member, pk=pk).delete()
    if not _is_htmx(request):
        return redirect("family")
    return _render_family(request)


@require_POST
def profile_switch(request, pk):
    if not is_valid_pk(pk):  # out-of-range ids would overflow the DB query
        raise Http404("No such member")
    member = get_object_or_404(Member, pk=pk)
    # Clicking the active member again deselects them. get_active_member also
    # drops a stale id first, so a deleted active member never blocks a switch.
    if get_active_member(request) == member:
        clear_active_member(request)
    else:
        set_active_member(request, member)
    target = request.POST.get("next") or request.headers.get("Referer")
    if not target or not url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        target = "/"
    return redirect(target)


def _edit_url(pk, filters, occurrence=None, planned=None):
    """The edit page's URL with #27's query, plus ``occurrence=`` or
    ``planned=YYYY-MM-DD`` (#38) when given."""
    url = reverse("chore_edit", args=[pk])
    if filters is not None:
        monday, member, day = filters
        url += dash.dashboard_query(dash.week_param(monday) if monday else None, member, day)
    if occurrence is not None:
        url += ("&" if "?" in url else "?") + f"occurrence={occurrence.pk}"
    if planned is not None:
        url += ("&" if "?" in url else "?") + f"planned={planned.isoformat()}"
    return url


def _create_url(start_date, template, recurrence, filters, calendar=None):
    """The create page's canonical URL from normalized values only (#34)."""
    parts = []
    if start_date:
        parts.append(f"date={start_date.isoformat()}")
    if template:
        parts.append(f"template={template.pk}")
    elif recurrence:  # a template page ignores recurrence
        parts.append(f"recurrence={recurrence}")
    if filters is not None:
        monday, member, day = filters
        parts.append("return_to=dashboard")
        query = dash.dashboard_query(dash.week_param(monday) if monday else None, member, day)
        if query:
            parts.append(query[1:])
    elif calendar is not None:  # opened from a calendar day (#36)
        parts.append("return_to=calendar")
        if calendar:
            parts.append(f"month={calendar}")
    url = reverse("chore_create")
    return url + "?" + "&".join(parts) if parts else url


def _chore_form_page(request, form, chore=None, start_date=None, filters=None, scope=None,
                     template=None, recurrence=None, calendar=None):
    context = {"form": form, "chore": chore, "prefill_date": start_date}
    if chore is None:
        context["switcher_next"] = _create_url(start_date, template, recurrence, filters, calendar)
    else:
        occurrence = scope.get("scope_occurrence") if scope else None
        planned = scope.get("scope_planned") if scope else None
        context["switcher_next"] = _edit_url(
            chore.pk, filters, occurrence, planned["date"] if planned else None)
        if planned is not None:  # #38: the same page, cut off at today
            context["edit_from_today_url"] = _edit_url(chore.pk, filters)
    if scope is not None:  # a recurring chore's edit scope (#20)
        context.update(scope)
    if filters is not None:  # normalized (monday, member, day) from the dashboard (#27)
        monday, member, day = filters
        week = dash.week_param(monday) if monday else None
        context.update(
            filter_week=week,
            filter_member="" if member is None else member,
            filter_day=day,
            back_url=reverse("dashboard") + dash.dashboard_query(week, member, day),
        )
        if chore is None:  # create opened from the dashboard (#19)
            context["return_to"] = "dashboard"
            query = dash.dashboard_query(week, member, day)
            context["picker_return"] = "&return_to=dashboard" + ("&" + query[1:] if query else "")
    if chore is None and filters is None and calendar is not None:  # from a calendar day (#36)
        context.update(
            return_to="calendar",
            return_month=calendar,
            calendar_back_url=_calendar_url(calendar),
            picker_return="&return_to=calendar" + (f"&month={calendar}" if calendar else ""),
        )
    if chore is None:  # the picker is only offered when creating
        templates = Chore.objects.filter(is_template=True).order_by(Lower("title"), "id")
        builtin = [t for t in templates if t.is_builtin]
        household = [t for t in templates if not t.is_builtin]
        context["builtin_templates"] = builtin
        context["household_templates"] = household
        context["template_groups"] = [("Built-in", builtin), ("Household", household)]
    return render(request, "chores/chore_form.html", context)


def _edit_redirect(pk, filters, occurrence=None, planned=None):
    """302 to the edit page with #27's query, plus ``occurrence=`` or
    ``planned=`` (#38) when given."""
    return redirect(_edit_url(pk, filters, occurrence, planned))


def _save_chore(request, form, chore=None, filters=None, scope=None, calendar=None):
    """Validate a bound ChoreForm and save; returns a response.

    A new one-off due this week or earlier is stored at once (#19), and a
    create opened from the dashboard returns there.

    With ``scope`` (a recurring chore's edit, #20) a repeating schedule is
    saved "this and future" from ``scope["scope_day"]``. An edit that leaves
    the chore ``once`` keeps its row or projected date in sync (#35), with
    ``today`` read once: ``scope["scope_today"]``, else the viewer's date.
    From a Planned date (#38) a refused edit (``FutureEditRefused``, also a
    switch to once) is a non-field error with status 200, and a saved one
    returns to the page with ``planned=``.
    """
    creating = chore is None
    old = None
    if not creating:  # read before is_valid(), which writes the form values onto it
        old = {name: getattr(chore, name) for name in RULE_FIELDS + ASSIGNMENT_FIELDS}
    if not form.is_valid():
        if scope is not None:
            # Show the saved chore (Delete options), not the rejected values.
            chore = Chore.objects.get(pk=chore.pk)
        return _chore_form_page(request, form, chore, filters=filters, scope=scope,
                                calendar=calendar)
    try:
        with transaction.atomic():
            chore = form.save(commit=False)
            if creating:
                chore.template_source = form.cleaned_data["template"]
            if scope is not None and scope.get("scope_planned") and chore.recurrence == Chore.Recurrence.ONCE:
                raise FutureEditRefused(FutureEditRefused.SCHEDULE)  # a schedule change too
            if scope is not None and chore.recurrence != Chore.Recurrence.ONCE:
                apply_future_edit(chore, old, scope["scope_day"], scope["scope_today"])
            elif not creating and chore.recurrence == Chore.Recurrence.ONCE:
                today = scope["scope_today"] if scope is not None else timezone.localdate()
                apply_one_off_edit(chore, old, today)
            else:
                chore.save()
            if creating and chore.recurrence == Chore.Recurrence.ONCE:
                materialize_one_off(chore, timezone.localdate())
            add_template = form.cleaned_data["save_as_template"]
            if add_template:
                form.save_household_template(chore)
    except OneOffEditRefused as refused:  # everything above is rolled back
        form.add_error("start_date", str(refused))
        chore = Chore.objects.get(pk=chore.pk)
        return _chore_form_page(request, form, chore, filters=filters, scope=scope)
    except FutureEditRefused as refused:  # everything above is rolled back (#38)
        form.add_error(None, str(refused))
        chore = Chore.objects.get(pk=chore.pk)
        return _chore_form_page(request, form, chore, filters=filters, scope=scope)
    messages.success(request, "Chore saved and template added" if add_template else "Chore saved")
    if creating and filters is not None:  # opened with return_to=dashboard (#19)
        return _dashboard_redirect(request.POST)
    if creating and calendar is not None:  # opened with return_to=calendar (#36)
        return redirect(_calendar_url(calendar))
    planned = scope.get("scope_planned") if scope is not None else None
    return _edit_redirect(chore.pk, filters, planned=planned["date"] if planned else None)


def _edit_occurrence(chore, data):
    """The ``occurrence=`` row of ``chore`` (last value), None when absent;
    404 for anything hostile, another chore's row or a cancelled row."""
    if "occurrence" not in data:
        return None
    pk = _parse_pk(data.get("occurrence"))
    if pk is None:
        raise Http404("No such occurrence")
    return get_object_or_404(ChoreOccurrence, pk=pk, chore=chore, is_cancelled=False)


def _edit_planned(chore, data, today):
    """The projected dict of the ``planned=`` date of ``chore`` (#38), None
    when absent; 404 for anything that is not a projected date after this
    Sunday for ``today`` (unparseable, empty, this week, off the rule,
    cancelled)."""
    if "planned" not in data:
        return None
    day = month.parse_date(data.get("planned"))
    if day is None or day <= dash.monday_of(today) + datetime.timedelta(days=6):
        raise Http404("Not a planned date of this chore")
    projected = projected_occurrence(chore, day, today)
    if projected is None:
        raise Http404("Not a planned date of this chore")
    return projected


def _scope_context(occurrence, today, planned=None):
    """Template values for a recurring chore's edit page (#20), cut off at a
    stored row, a Planned date (#38) or today."""
    day = occurrence.date if occurrence else planned["date"] if planned else today
    context = {"scope_day": day, "scope_today": today, "scope_occurrence": occurrence,
               "scope_planned": planned}
    if occurrence is not None and occurrence.status == ChoreOccurrence.Status.TODO:
        context["occurrence_form"] = OccurrenceForm.for_row(occurrence)
    elif planned is not None:
        context["occurrence_form"] = OccurrenceForm.for_planned(planned)
    return context


def _moved_message(day, today):
    """"Occurrence moved to Sat 5 Dec", with the year when it is not today's (#48)."""
    return f"Occurrence moved to {_day_text(day, today)}"


def _save_occurrence(request, chore, occurrence, filters, scope):
    """"This occurrence only" (#20, #39): due time, points, title and instructions of one todo row.

    A new ``date`` moves the row within the current week instead (#40)."""
    if occurrence is None:
        return HttpResponseBadRequest("Choose an occurrence")
    if occurrence.status != ChoreOccurrence.Status.TODO:
        return HttpResponseBadRequest("Undo first")
    form = OccurrenceForm(request.POST)
    if not form.is_valid():
        scope["occurrence_form"] = form
        return _chore_form_page(request, ChoreForm(instance=chore), chore, filters=filters, scope=scope)
    data = form.cleaned_data
    moving = data["date"] is not None and data["date"] != occurrence.date
    if moving:  # #40: a new date in the current week
        try:
            saved = move_occurrence(occurrence, data["date"], data["due_time"], data["points"],
                                    data["title"], data["instructions"], scope["scope_today"])
        except MoveRefused as refused:  # nothing was written
            form.add_error("date", str(refused))
            scope["occurrence_form"] = form
            return _chore_form_page(request, ChoreForm(instance=chore), chore, filters=filters, scope=scope)
    else:
        saved = update_occurrence(occurrence, data["due_time"], data["points"],
                                  data["title"], data["instructions"])
    if not saved:
        # Lost a race: the row changed after it was read.
        current = ChoreOccurrence.objects.filter(pk=occurrence.pk, chore=chore, is_cancelled=False).first()
        if current is None:
            raise Http404("No such occurrence")
        if current.status != ChoreOccurrence.Status.TODO:
            return HttpResponseBadRequest("Undo first")
    if moving:
        today = scope["scope_today"]
        messages.success(request, _moved_message(data["date"], today))
        if data["date"] > dash.monday_of(today) + datetime.timedelta(days=6):
            # A later week (#48): the row is now cancelled, so no occurrence=.
            return _edit_redirect(chore.pk, filters)
    else:
        messages.success(request, "Occurrence saved")
    return _edit_redirect(chore.pk, filters, occurrence)


def _save_planned(request, chore, filters, scope):
    """"This occurrence only" on a Planned date (#38): its title,
    instructions, due time and points. A posted ``date`` is ignored."""
    planned = scope["scope_planned"]
    form = OccurrenceForm.for_planned(planned, request.POST)
    if not form.is_valid():
        scope["occurrence_form"] = form
        return _chore_form_page(request, ChoreForm(instance=chore), chore, filters=filters, scope=scope)
    data = form.cleaned_data
    if not edit_projected_date(chore, planned["date"], data["title"], data["instructions"],
                               data["due_time"], data["points"], scope["scope_today"]):
        raise Http404("Not a planned date of this chore")  # lost a race
    messages.success(request, "Occurrence saved")
    return _edit_redirect(chore.pk, filters, planned=planned["date"])


@require_http_methods(["GET", "POST"])
def chore_create(request):
    # return_to=dashboard (exactly) keeps the dashboard view: GET reads the query,
    # POST the hidden inputs (#19).
    data = request.POST if request.method == "POST" else request.GET
    filters = _dashboard_filters(data) if data.get("return_to") == "dashboard" else None
    calendar = _calendar_return(data, timezone.localdate())
    if request.method == "POST":
        return _save_chore(request, ChoreForm(request.POST), filters=filters, calendar=calendar)
    # ?date= and ?recurrence= are read here only: never on edit, never on POST (#14, #19).
    start_date = month.parse_date(request.GET.get("date"))
    template = find_template(request.GET.get("template"))
    initial = {}
    if start_date:
        initial["start_date"] = start_date
    recurrence = request.GET.get("recurrence")
    if recurrence in Chore.Recurrence.values:
        initial["recurrence"] = recurrence
    else:
        recurrence = None
    if template:
        form = ChoreForm.from_template(template, start_date)
    else:
        form = ChoreForm(initial=initial)
    return _chore_form_page(request, form, start_date=start_date, filters=filters,
                            template=template, recurrence=recurrence, calendar=calendar)


@require_http_methods(["GET", "POST"])
def chore_edit(request, pk):
    if not is_valid_pk(pk):  # out-of-range ids would overflow the DB query
        raise Http404("No such chore")
    # Templates (is_template=True) are not editable here; that is #23.
    chore = get_object_or_404(Chore, pk=pk, is_template=False)
    # The dashboard view to return to: the form posts it back as hidden inputs.
    data = request.POST if request.method == "POST" else request.GET
    filters = _dashboard_filters(data)
    if chore.recurrence == Chore.Recurrence.ONCE:
        # One-off chores: occurrence and scope are ignored (#20); _save_chore keeps
        # the row or projected date in sync through apply_one_off_edit (#35).
        if request.method == "POST":
            return _save_chore(request, ChoreForm(request.POST, instance=chore), chore, filters)
        return _chore_form_page(request, ChoreForm(instance=chore), chore, filters=filters)
    # Recurring: cutoff D is the occurrence's date or the Planned date (#38),
    # else today in the viewer's zone.
    if "occurrence" in data and "planned" in data:
        return HttpResponseBadRequest("Give an occurrence or a planned date, not both")
    occurrence = _edit_occurrence(chore, data)
    today = timezone.localdate()
    planned = _edit_planned(chore, data, today)
    scope = _scope_context(occurrence, today, planned)
    if request.method == "GET":
        return _chore_form_page(request, ChoreForm(instance=chore), chore, filters=filters, scope=scope)
    choice = request.POST.get("scope", "future")
    if choice not in ("this", "future"):
        return HttpResponseBadRequest("scope must be 'this' or 'future'")
    if choice == "this":
        if planned is not None:
            return _save_planned(request, chore, filters, scope)
        return _save_occurrence(request, chore, occurrence, filters, scope)
    return _save_chore(request, ChoreForm(request.POST, instance=chore), chore, filters, scope)


# --- Delete (#5) -----------------------------------------------------------------

_HISTORY = (ChoreOccurrence.Status.DONE, ChoreOccurrence.Status.SKIPPED)


def _end_before(chore, day):
    """Stop projecting ``chore`` from ``day`` on; an earlier end_date stays."""
    end = day - datetime.timedelta(days=1)
    if chore.end_date is None or end < chore.end_date:
        chore.end_date = end
        chore.save(update_fields=["end_date"])


def _left_to_show(chore, last, today):
    """True when ``chore`` still has something to show, read after a delete wrote.

    Cheap checks first: a done / skipped row (any ``is_cancelled``), a to-do
    row that is not cancelled, or a date projected in [this week's Monday,
    ``last``] for the viewer's ``today`` (#26). Projection covers plain dates,
    override exceptions and #48 extras, never cancelled exceptions or rows.
    """
    if chore.occurrences.filter(status__in=_HISTORY).exists():
        return True
    if chore.occurrences.filter(status=ChoreOccurrence.Status.TODO, is_cancelled=False).exists():
        return True
    monday = dash.monday_of(today)
    return last >= monday and any(
        o["chore_id"] == chore.pk for o in project_occurrences(monday, last, today=today)
    )


def _delete_from(chore, start, today):
    """"This and future" from ``start``: to-do rows on/after it go, nothing before it does.

    The chore's exceptions (#31) dated on/after ``start`` go with them (#22),
    whoever calls: the stored card, the edit page and the Planned card.
    Exceptions before ``start`` stay, so those dates keep their members.

    The chore is kept with ``end_date = start - 1`` when something before
    ``start`` is left to show: a done / skipped row, a non-cancelled to-do row,
    or a date still projected from this week's Monday. Otherwise the chore row
    goes, with its cancelled rows and RotationState (CASCADE).
    """
    chore.occurrences.filter(status=ChoreOccurrence.Status.TODO, date__gte=start).delete()
    chore.exceptions.filter(date__gte=start).delete()
    if _left_to_show(chore, start - datetime.timedelta(days=1), today):
        _end_before(chore, start)
    else:
        chore.delete()  # occurrences and RotationState go by CASCADE


@require_POST
def chore_delete(request, pk):
    """Delete a chore, one stored occurrence (``occurrence``, #5) or one
    projected date (``date``, #22), or "this and future" from either.

    ``date`` must be a projected date of the chore for the viewer's
    ``today`` (#26). ``today`` is read at most once, and the same value is
    used for that check, for ``_delete_from`` and for ``_left_to_show``.

    "This only" on a recurring chore with an ``end_date`` (#43): the chore is
    snapshotted before the cancel is written, and if nothing is left to show
    up to ``end_date`` afterwards, the chore row is deleted in the same
    transaction (rows, exceptions and RotationState by CASCADE). Undo then
    gets kind ``chore`` with the pre-cancel snapshot. Without an ``end_date``
    the chore always has dates ahead, so no snapshot or check is made.
    """
    if not is_valid_pk(pk):  # out-of-range ids would overflow the DB query
        raise Http404("No such chore")
    today = None
    undone = None  # (kind, data) of what Undo (#24) puts back, None: nothing written
    with transaction.atomic():
        # Templates are deleted only through template_delete (#23).
        chore = get_object_or_404(Chore.objects.select_for_update(), pk=pk, is_template=False)
        occurrence = None
        day = None
        if "occurrence" in request.POST and "date" in request.POST:
            return HttpResponseBadRequest("Give an occurrence or a date, not both")
        if "date" in request.POST:
            day = month.parse_date(request.POST.get("date"))
            if day is None:
                return HttpResponseBadRequest("Invalid date")
            today = timezone.localdate()
            if projected_occurrence(chore, day, today) is None:
                raise Http404("Not a planned date of this chore")
        elif "occurrence" in request.POST:
            occurrence_pk = _parse_pk(request.POST["occurrence"])
            if occurrence_pk is None:
                raise Http404("No such occurrence")
            occurrence = get_object_or_404(
                ChoreOccurrence, pk=occurrence_pk, chore=chore, is_cancelled=False
            )
        if chore.recurrence == Chore.Recurrence.ONCE:
            snap = undo.snapshot(chore)
            # scope is ignored: delete the chore unless it has history.
            if chore.occurrences.filter(status__in=_HISTORY).exists():
                _end_before(chore, chore.start_date)
            else:
                chore.delete()
            undone = undo.after_delete(snap)
        else:
            scope = request.POST.get("scope")
            if scope not in ("this", "future"):
                return HttpResponseBadRequest("scope must be 'this' or 'future'")
            if scope == "this":
                if day is None and (occurrence is None or occurrence.status != ChoreOccurrence.Status.TODO):
                    return HttpResponseBadRequest("Only a to-do occurrence can be deleted on its own")
                # Only a chore with an end_date can run out of dates to show (#43).
                snap = undo.snapshot(chore) if chore.end_date is not None else None
                if day is not None:
                    undone = ("planned", {"date": day.isoformat(), "exception": undo.exception_on(chore, day)})
                    cancel_projected_date(chore, day)
                else:
                    occurrence.is_cancelled = True
                    occurrence.save(update_fields=["is_cancelled"])
                    undone = ("cancel", {"occurrence_id": occurrence.pk})
                if snap is not None:
                    if today is None:
                        today = timezone.localdate()
                    if not _left_to_show(chore, chore.end_date, today):
                        chore.delete()  # occurrences, exceptions and RotationState by CASCADE
                        undone = ("chore", snap)
            else:
                if today is None:
                    today = timezone.localdate()
                start = day or (occurrence.date if occurrence else today)
                snap = undo.snapshot(chore)
                _delete_from(chore, start, today)
                undone = undo.after_delete(snap)
    # Committed: only now does the latest delete replace the undo slot.
    if undone is None:
        request.session.pop(undo.SLOT, None)
        messages.success(request, "Chore deleted")
    else:
        request.session[undo.SLOT] = undo.new_slot(undone[0], pk, timezone.now(), undone[1])
        messages.success(request, "Chore deleted", extra_tags="undo-delete")
    return _dashboard_redirect(request.POST)


@require_POST
def chore_delete_undo(request):
    """Undo the latest delete of this browser (#24), within 600 seconds.

    ``token`` must be 32 lowercase hex characters (else 400) and match the
    session's slot (else 404, slot kept). A malformed or expired slot is a
    404 and removed. A state that can't be put back exactly is a 400 and
    the slot is removed. Dates come from the slot; ``localdate`` is never read.
    """
    tokens = request.POST.getlist("token")
    if len(tokens) != 1 or not undo.TOKEN.fullmatch(tokens[0]):
        return HttpResponseBadRequest("Invalid token")
    slot = request.session.get(undo.SLOT)
    if slot is None:
        raise Http404("Nothing to undo")
    if isinstance(slot, dict) and isinstance(slot.get("token"), str) \
            and not secrets.compare_digest(slot["token"].encode(), tokens[0].encode()):
        raise Http404("Nothing to undo")  # a forged POST leaves the real slot alone
    if not undo.is_live(slot, timezone.now()):
        del request.session[undo.SLOT]
        raise Http404("Nothing to undo")
    del request.session[undo.SLOT]
    try:
        undo.restore(slot)
    except (undo.Refused, IntegrityError):
        return HttpResponseBadRequest("This delete can no longer be undone")
    messages.success(request, "Chore restored")
    return _dashboard_redirect(request.POST)


# --- Done / skip / undo (#12) ------------------------------------------------------

_TOGGLE_TARGET = {
    "done": ChoreOccurrence.Status.DONE,
    "skip": ChoreOccurrence.Status.SKIPPED,
    "undo": ChoreOccurrence.Status.TODO,
}


def _toggle_allowed_from(action):
    if action == "undo":
        return (ChoreOccurrence.Status.DONE, ChoreOccurrence.Status.SKIPPED)
    return (ChoreOccurrence.Status.TODO,)


def _toggle_success(request, pk, chore_id):
    """The swapped card block for HTMX, else the 302 back to the same view.

    The block is followed by one out-of-band ``<article>`` (``card_only`` and
    ``card_oob`` in ``_chore_card.html``) for each other stored card of the
    chore that the posted dashboard view shows (#37), so a new streak reaches
    them without a reload. Planned cards never show a streak and are skipped.
    """
    if not _is_htmx(request):
        return _dashboard_redirect(request.POST)
    now = timezone.localtime()  # one clock read, viewer's zone (#26)
    today = timezone.localdate(now)
    monday, member, day = _dashboard_filters(request.POST)
    nav = dash.week_nav(monday or dash.monday_of(today), today)
    visible = Q(date__gte=nav["monday"], date__lte=nav["sunday"])
    if nav["is_current"]:
        visible |= Q(status=ChoreOccurrence.Status.TODO, date__lt=nav["monday"])
    rows = _shown_occurrences().filter(Q(pk=pk) | (Q(chore_id=chore_id) & visible))
    cards = _stored_cards(rows, _PoolCache(), today=today)
    for card in cards:
        card.overdue = dash.is_overdue(card, today, now)
    card = next((c for c in cards if c.pk == pk), None)
    if card is None:  # removed after the write
        raise Http404("No such occurrence")
    siblings = dash.filter_cards([c for c in cards if c.pk != pk], nav["monday"], member, day)
    context = {"card": card}
    context.update(_filter_context(nav, member, day))
    response = render(request, "chores/_chore_card.html", context)
    for sibling in sorted(siblings, key=dash.list_key):
        response.content += render_to_string(
            "chores/_chore_card.html",
            {**context, "card": sibling, "card_only": True, "card_oob": True},
        ).encode()
    return response


@require_POST
def occurrence_toggle(request, pk):
    if not is_valid_pk(pk):  # out-of-range ids would overflow the DB query
        raise Http404("No such occurrence")
    occurrence = get_object_or_404(_shown_occurrences(), pk=pk)
    action = request.POST.get("action")
    if action not in _TOGGLE_TARGET:
        return HttpResponseBadRequest("Unknown action")
    target = _TOGGLE_TARGET[action]
    if occurrence.status == target:
        return _toggle_success(request, pk, occurrence.chore_id)  # repeat: nothing to write
    if occurrence.status not in _toggle_allowed_from(action):
        return HttpResponseBadRequest("Undo first")
    credited = get_active_member(request) if action == "done" else None
    if not set_occurrence_status(occurrence, action, credited, timezone.now()):
        # Lost a race: the row changed or was cancelled after it was read.
        current = _shown_occurrences().filter(pk=pk).first()
        if current is None:
            raise Http404("No such occurrence")
        if current.status != target:
            return HttpResponseBadRequest("Undo first")
    return _toggle_success(request, pk, occurrence.chore_id)


# --- Assign / reassign (#13) -------------------------------------------------------


def _assign_response(request, pk):
    """The swapped card, the re-rendered list when a member filter now hides
    it, or (no JavaScript) the 302 back to the same view."""
    if not _is_htmx(request):
        return _dashboard_redirect(request.POST)
    now = timezone.localtime()  # one clock read, viewer's zone (#26)
    today = timezone.localdate(now)
    cards = _stored_cards(_shown_occurrences().filter(pk=pk), _PoolCache(), today=today)
    if not cards:  # removed after the write
        raise Http404("No such occurrence")
    [card] = cards
    card.overdue = dash.is_overdue(card, today, now)
    monday, member, day = _dashboard_filters(request.POST)
    monday = monday or dash.monday_of(today)
    if member is None or dash.filter_cards([card], monday, member, None):
        context = {"card": card, "card_only": True}
        context.update(_filter_context(dash.week_nav(monday, today), member, day))
        return render(request, "chores/_chore_card.html", context)
    context = _dashboard_context(monday, today, now, member, day)
    context.update(_filter_context(context["nav"], member, day))
    response = render(request, "chores/_dashboard_list.html", context)
    response["HX-Retarget"] = "#dashboard-list"
    response["HX-Reswap"] = "outerHTML"
    return response


@require_POST
def occurrence_assign(request, pk):
    if not is_valid_pk(pk):  # out-of-range ids would overflow the DB query
        raise Http404("No such occurrence")
    occurrence = get_object_or_404(_shown_occurrences(), pk=pk)
    raw = request.POST.get("assignee")
    if raw is None:
        return HttpResponseBadRequest("Choose who to assign")
    if raw == "":
        member_id = None
    else:
        member_id = _parse_pk(raw)
        if member_id is None or not Member.objects.filter(pk=member_id).exists():
            return HttpResponseBadRequest("Unknown member")
    if occurrence.status != ChoreOccurrence.Status.TODO:
        return HttpResponseBadRequest("Undo first")
    if occurrence.member_id != member_id:
        updated = _shown_occurrences().filter(
            pk=pk, status=ChoreOccurrence.Status.TODO
        ).update(member_id=member_id, is_override=True)
        if not updated:
            # Lost a race: the row was cancelled or marked done after it was read.
            current = _shown_occurrences().filter(pk=pk).first()
            if current is None:
                raise Http404("No such occurrence")
            if current.status != ChoreOccurrence.Status.TODO:
                return HttpResponseBadRequest("Undo first")
    return _assign_response(request, pk)


# --- Assign a projected date (#31) -------------------------------------------------


def _parse_assignee(raw):
    """``(member_id or None, error)`` for the posted ``assignee`` (as #13)."""
    if raw is None:
        return None, "Choose who to assign"
    if raw == "":
        return None, None
    member_id = _parse_pk(raw)
    if member_id is None or not Member.objects.filter(pk=member_id).exists():
        return None, "Unknown member"
    return member_id, None


def _planned_assign_response(request, chore, day, today, now):
    """The rebuilt Planned card, the re-rendered list when a member filter now
    hides it, or (no JavaScript) the 302 back to the same view."""
    if not _is_htmx(request):
        return _dashboard_redirect(request.POST)
    p = projected_occurrence(chore, day, today)
    if p is None:  # gone after the write
        raise Http404("No such date")
    [card] = _projected_cards([p], _PoolCache())
    monday, member, day_filter = _dashboard_filters(request.POST)
    monday = monday or dash.monday_of(today)
    if member is None or dash.filter_cards([card], monday, member, None):
        context = {"card": card, "card_only": True}
        context.update(_filter_context(dash.week_nav(monday, today), member, day_filter))
        return render(request, "chores/_chore_card.html", context)
    context = _dashboard_context(monday, today, now, member, day_filter)
    context.update(_filter_context(context["nav"], member, day_filter))
    response = render(request, "chores/_dashboard_list.html", context)
    response["HX-Retarget"] = "#dashboard-list"
    response["HX-Reswap"] = "outerHTML"
    return response


@require_POST
def planned_assign(request, chore_id):
    """Assign one projected date D of a chore before its week is stored.

    Writes only the ``(chore, D)`` exception (``is_override=True``); never a
    ChoreOccurrence or RotationState. D must be a projected date of the chore
    for the viewer's ``today`` (#26), checked in the same transaction as the
    write.
    """
    if not is_valid_pk(chore_id):  # out-of-range ids would overflow the DB query
        raise Http404("No such chore")
    now = timezone.localtime()  # one clock read, viewer's zone (#26)
    today = timezone.localdate(now)
    with transaction.atomic():
        chore = get_object_or_404(Chore.objects.select_for_update(), pk=chore_id, is_template=False)
        day = month.parse_date(request.POST.get("date"))
        if day is None:
            return HttpResponseBadRequest("Invalid date")
        member_id, error = _parse_assignee(request.POST.get("assignee"))
        if error:
            return HttpResponseBadRequest(error)
        projected = projected_occurrence(chore, day, today)
        if projected is None:
            raise Http404("Not a planned date of this chore")
        if projected["member_id"] != member_id:
            set_projected_member(chore, day, member_id)
    return _planned_assign_response(request, chore, day, today, now)


# --- Move a projected date (#48) ---------------------------------------------------


@require_POST
def planned_move(request, chore_id):
    """Move one projected date ``date`` of a recurring chore to ``target``,
    from today to ``min(end_date, 31 Dec 2099)``: a later week (#48) or the
    current week (#51), where it becomes a todo row right away. A plain POST
    that returns to the posted dashboard view; a refused target is a message
    there and writes nothing.

    ``date`` must be a projected date after this Sunday for the viewer's
    ``today`` (#26), read once and also passed to ``move_projected_date``.
    """
    if not is_valid_pk(chore_id):  # out-of-range ids would overflow the DB query
        raise Http404("No such chore")
    chore = get_object_or_404(Chore, pk=chore_id, is_template=False)
    if chore.recurrence == Chore.Recurrence.ONCE:
        return HttpResponseBadRequest("Move a one-off chore by editing its date")
    day = month.parse_date(request.POST.get("date"))
    if day is None:
        return HttpResponseBadRequest("Invalid date")
    today = timezone.localdate()
    if day <= dash.monday_of(today) + datetime.timedelta(days=6) \
            or projected_occurrence(chore, day, today) is None:
        raise Http404("Not a planned date of this chore")
    target = month.parse_date(request.POST.get("target"))
    if target is None:
        messages.error(request, "Enter a valid date.")
        return _dashboard_redirect(request.POST)
    if target == day:
        return _dashboard_redirect(request.POST)
    try:
        moved = move_projected_date(chore, day, target, today)
    except MoveRefused as refused:  # nothing was written
        messages.error(request, str(refused))
        return _dashboard_redirect(request.POST)
    if not moved:  # lost a race: no longer a planned date
        raise Http404("Not a planned date of this chore")
    messages.success(request, _moved_message(target, today))
    return _dashboard_redirect(request.POST)
