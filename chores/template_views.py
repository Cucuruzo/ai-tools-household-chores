from django.contrib import messages
from django.db import transaction
from django.db.models.functions import Lower
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .forms import TemplateForm
from .models import Chore
from .profile import is_valid_pk
from .views import _is_htmx


def _household_template_or_404(pk):
    if not is_valid_pk(pk):  # out-of-range ids would overflow the DB query
        raise Http404("No such template")
    return get_object_or_404(Chore, pk=pk, is_template=True, is_builtin=False)


def _render_list(request):
    templates = Chore.objects.filter(is_template=True, is_builtin=False).order_by(
        Lower("title"), "id"
    )
    return render(request, "chores/template_list.html", {"templates": templates})


@require_GET
def template_list(request):
    return _render_list(request)


@require_http_methods(["GET", "POST"])
def template_edit(request, pk):
    template = _household_template_or_404(pk)
    if request.method == "POST":
        form = TemplateForm(request.POST, instance=template)
        if form.is_valid():
            with transaction.atomic():
                form.save()
            messages.success(request, "Template saved")
            return redirect("template_list")
    else:
        form = TemplateForm(instance=template)
    return render(
        request, "chores/chore_form.html", {"form": form, "editing_template": template}
    )


@require_POST
def template_delete(request, pk):
    _household_template_or_404(pk).delete()  # derived chores: template_source -> NULL
    messages.success(request, "Template deleted")
    if not _is_htmx(request):
        return redirect("template_list")
    return _render_list(request)
