from django.contrib import messages
from django.db import transaction
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
from .forms import ChoreForm, MemberForm, find_template
from .models import Chore, ChoreOccurrence, Member
from .profile import MAX_PK, is_valid_pk, set_active_member
from .services import eligible_pool, materialize_week, project_occurrences


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


def _stored_cards(occurrences, pools):
    cards = []
    for occ in occurrences.select_related("chore", "member"):
        cards.append(dash.Card(
            pk=occ.pk, chore_id=occ.chore_id, title=occ.chore.title, date=occ.date,
            due_time=occ.due_time, points=occ.points, status=occ.status, member=occ.member,
            assignee_note=_note(pools, occ.chore, occ.chore.assignment_mode, occ.member, occ.is_override),
        ))
    return cards


def _projected_cards(projected, pools):
    chores = Chore.objects.in_bulk({p["chore_id"] for p in projected})
    members = Member.objects.in_bulk({p["member_id"] for p in projected if p["member_id"]})
    cards = []
    for p in projected:
        member = members.get(p["member_id"])
        cards.append(dash.Card(
            pk=None, chore_id=p["chore_id"], title=p["title"], date=p["date"],
            due_time=p["due_time"], points=p["points"], status=dash.PLANNED, member=member,
            assignee_note=_note(pools, chores[p["chore_id"]], p["assignment_mode"], member, False),
        ))
    return cards


def _shown_occurrences():
    # Templates never get rows (#9); the filter keeps it that way on screen too.
    return ChoreOccurrence.objects.filter(chore__is_template=False)


def _dashboard_context(monday, today, now):
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
        sections = dash.group_current_week(_stored_cards(rows, pools), today, now)
        context["sections"] = [
            ("overdue", "Overdue", sections["overdue"]),
            ("today", "Today", sections["today"]),
            ("upcoming", "Upcoming", sections["upcoming"]),
        ]
        context["done_earlier"] = sections["done_earlier"]
        context["is_empty"] = not any(sections.values())
    else:
        cards = _stored_cards(_shown_occurrences().filter(week), pools)
        if nav["is_past"]:
            dash.mark_past_overdue(cards)
        else:
            projected = project_occurrences(nav["monday"], nav["sunday"], today=today)
            cards += _projected_cards(projected, pools)
        context["cards"] = sorted(cards, key=dash.list_key)
        context["is_empty"] = not cards
    return context


@require_GET
def dashboard(request):
    # One clock read: "now" and "today" are the same instant in the viewer's
    # zone (#26); timezone.localdate(now) is timezone.localdate() for it.
    now = timezone.localtime()
    today = timezone.localdate(now)
    monday = dash.parse_week(request.GET.get("week")) or dash.monday_of(today)
    context = _dashboard_context(monday, today, now)
    partial = _is_htmx(request) and request.headers.get("HX-History-Restore-Request") != "true"
    template = "chores/_dashboard_list.html" if partial else "chores/dashboard.html"
    response = render(request, template, context)
    # The same URL serves a partial or a full page; keep browser caches apart.
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
    raw = values[0]
    if len(values) > 1 or not (raw.isascii() and raw.isdigit()):
        raise Http404
    pk = int(raw)
    if not 1 <= pk <= MAX_PK or not Member.objects.filter(pk=pk).exists():
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
    set_active_member(request, member)
    target = request.POST.get("next") or request.headers.get("Referer")
    if not target or not url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        target = "/"
    return redirect(target)


def _chore_form_page(request, form, chore=None):
    context = {"form": form, "chore": chore}
    if chore is None:  # the picker is only offered when creating
        templates = Chore.objects.filter(is_template=True).order_by(Lower("title"), "id")
        builtin = [t for t in templates if t.is_builtin]
        household = [t for t in templates if not t.is_builtin]
        context["builtin_templates"] = builtin
        context["household_templates"] = household
        context["template_groups"] = [("Built-in", builtin), ("Household", household)]
    return render(request, "chores/chore_form.html", context)


def _save_chore(request, form, chore=None):
    """Validate a bound ChoreForm and save; returns a response."""
    if not form.is_valid():
        return _chore_form_page(request, form, chore)
    creating = chore is None
    with transaction.atomic():
        chore = form.save(commit=False)
        if creating:
            chore.template_source = form.cleaned_data["template"]
        chore.save()
        add_template = form.cleaned_data["save_as_template"]
        if add_template:
            form.save_household_template(chore)
    messages.success(request, "Chore saved and template added" if add_template else "Chore saved")
    return redirect("chore_edit", pk=chore.pk)


@require_http_methods(["GET", "POST"])
def chore_create(request):
    if request.method == "POST":
        return _save_chore(request, ChoreForm(request.POST))
    template = find_template(request.GET.get("template"))
    form = ChoreForm.from_template(template) if template else ChoreForm()
    return _chore_form_page(request, form)


@require_http_methods(["GET", "POST"])
def chore_edit(request, pk):
    if not is_valid_pk(pk):  # out-of-range ids would overflow the DB query
        raise Http404("No such chore")
    # Templates (is_template=True) are not editable here; that is #23.
    chore = get_object_or_404(Chore, pk=pk, is_template=False)
    if request.method == "POST":
        return _save_chore(request, ChoreForm(request.POST, instance=chore), chore)
    return _chore_form_page(request, ChoreForm(instance=chore), chore)
