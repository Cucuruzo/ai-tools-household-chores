from django.contrib import messages
from django.db import transaction
from django.db.models.functions import Lower
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .forms import ChoreForm, MemberForm, find_template
from .models import Chore, Member
from .profile import is_valid_pk, set_active_member


def home(request):
    return HttpResponse("Household Chore Manager")


def _member_form(member=None, data=None):
    """Edit forms get a per-member id prefix so several rows can coexist."""
    auto_id = f"id_m{member.pk}_%s" if member else "id_%s"
    return MemberForm(data, instance=member, auto_id=auto_id)


def _render_family(request, add_form=None):
    members = list(Member.objects.all())
    for m in members:
        m.edit_form = _member_form(m)
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


@require_GET
def family(request):
    return _render_family(request)


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
    member = get_object_or_404(Member, pk=pk)
    form = _member_form(member, request.POST)
    editing = not form.is_valid()
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
