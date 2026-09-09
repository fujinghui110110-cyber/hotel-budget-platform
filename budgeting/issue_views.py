from decimal import Decimal

from django import forms
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from budgeting.excel.money import cents_to_yuan, yuan_to_cents
from budgeting.models import BudgetCycle, ValidationIssue
from budgeting.services.workflow import audit


class IssueExplanationForm(forms.Form):
    acknowledgement_note = forms.CharField(
        label="说明",
        max_length=4000,
        required=True,
        widget=forms.Textarea(attrs={"rows": 6}),
    )

    def __init__(self, *args, required=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["acknowledgement_note"].required = required

    def clean_acknowledgement_note(self):
        note = self.cleaned_data.get("acknowledgement_note", "").strip()
        if self.fields["acknowledgement_note"].required and not note:
            raise forms.ValidationError("请填写 P1 问题说明。")
        return note


class P1ThresholdForm(forms.Form):
    threshold_yuan = forms.DecimalField(
        label="P1 金额阈值（元）",
        required=False,
        min_value=Decimal("0"),
        max_digits=24,
        decimal_places=4,
        help_text="留空表示禁用金额阈值；金额按分保存。",
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
    )


 
 
 


def _is_admin(user):
    return bool(user.is_authenticated and (user.role == "ADMIN" or user.is_superuser))


def _issue_queryset(upload_id=None):
    queryset = ValidationIssue.objects.select_related(
        "run__upload__project",
        "run__upload__cycle",
    ).filter(severity=ValidationIssue.Severity.P1)
    if upload_id is not None:
        queryset = queryset.filter(run__upload_id=upload_id)
    return queryset


def _get_issue(issue_id, upload_id=None):
    return get_object_or_404(_issue_queryset(upload_id), pk=issue_id)


def _forbidden_if_out_of_scope(request, issue):
    if _is_admin(request.user):
        return None
    if request.user.role != "PROJECT":
        return HttpResponseForbidden("无权访问该问题。")
    if issue.run.upload.project_id != request.user.project_id:
        return HttpResponseForbidden("无权访问该问题。")
    return None


def _post_data(request):
    data = request.POST.copy()
    if "acknowledgement_note" not in data:
        for alias in ("note", "explanation"):
            if alias in data:
                data["acknowledgement_note"] = data[alias]
                break
    return data


def _threshold_data(request):
    data = request.POST.copy()
    if "threshold_yuan" not in data:
        for alias in ("threshold", "p1_threshold_yuan", "p1_threshold"):
            if alias in data:
                data["threshold_yuan"] = data[alias]
                break
    return data


def _issue_context(issue, form, request, *, is_admin, frozen):
    upload = issue.run.upload
    return {
        "issue": issue,
        "upload": upload,
        "cycle": upload.cycle,
        "form": form,
        "is_admin": is_admin,
        "frozen": frozen,
        "read_only": frozen,
        "can_submit": not frozen,
    }


@login_required
def issue_explanation(request, issue_id, upload_id=None):
    issue = _get_issue(issue_id, upload_id)
    forbidden = _forbidden_if_out_of_scope(request, issue)
    if forbidden is not None:
        return forbidden

    is_admin = _is_admin(request.user)
    cycle = issue.run.upload.cycle
    frozen = cycle.status == BudgetCycle.Status.FROZEN

    if request.method == "GET":
        form = IssueExplanationForm(
            required=not is_admin,
            initial={"acknowledgement_note": issue.acknowledgement_note},
        )
        return render(
            request,
            "budgeting/issue_explanation.html",
            _issue_context(issue, form, request, is_admin=is_admin, frozen=frozen),
        )

    if request.method != "POST":
        return HttpResponse("仅支持 GET 或 POST。", status=405)

    if frozen:
        return HttpResponse("本周期已冻结，只读。", status=409)

    form = IssueExplanationForm(_post_data(request), required=not is_admin)
    if not form.is_valid():
        return render(
            request,
            "budgeting/issue_explanation.html",
            _issue_context(issue, form, request, is_admin=is_admin, frozen=False),
            status=400,
        )

    note = form.cleaned_data["acknowledgement_note"]
    try:
        with transaction.atomic():
            locked_issue = _issue_queryset().select_for_update().get(pk=issue.pk)
            locked_upload = locked_issue.run.upload
            locked_cycle = BudgetCycle.objects.select_for_update().get(pk=locked_upload.cycle_id)
            if locked_cycle.status == BudgetCycle.Status.FROZEN:
                return HttpResponse("本周期已冻结，只读。", status=409)

            previous_note = locked_issue.acknowledgement_note
            previous_acknowledged = locked_issue.acknowledged
            if is_admin:
                if note:
                    locked_issue.acknowledgement_note = note
                if not locked_issue.acknowledgement_note.strip():
                    form.add_error("acknowledgement_note", "请先由项目端填写说明，再确认 P1 问题。")
                    return render(
                        request,
                        "budgeting/issue_explanation.html",
                        _issue_context(
                            locked_issue,
                            form,
                            request,
                            is_admin=True,
                            frozen=False,
                        ),
                        status=400,
                    )
                locked_issue.acknowledged = True
                action = "P1_ISSUE_ACKNOWLEDGED"
            else:
                locked_issue.acknowledgement_note = note
                locked_issue.acknowledged = False
                action = "P1_ISSUE_EXPLAINED"

            locked_issue.save(update_fields=["acknowledgement_note", "acknowledged"])
            audit(
                request.user,
                action,
                "ValidationIssue",
                locked_issue.pk,
                {
                    "previous_acknowledged": previous_acknowledged,
                    "previous_note": previous_note,
                    "acknowledged": locked_issue.acknowledged,
                    "acknowledgement_note": locked_issue.acknowledgement_note,
                },
                project=locked_upload.project,
                cycle=locked_cycle,
                upload=locked_upload,
            )
    except ValidationIssue.DoesNotExist:
        return HttpResponse("问题不存在。", status=404)

    return redirect("issue_explanation", issue_id=issue.pk)


@login_required
def cycle_p1_threshold(request, cycle_id):
    if not _is_admin(request.user):
        return HttpResponseForbidden("仅管理端可设置 P1 阈值。")

    cycle = get_object_or_404(BudgetCycle, pk=cycle_id)
    frozen = cycle.status == BudgetCycle.Status.FROZEN
    initial = None if cycle.p1_threshold_cents is None else cents_to_yuan(cycle.p1_threshold_cents)

    if request.method == "GET":
        form = P1ThresholdForm(initial={"threshold_yuan": initial})
        return render(
            request,
            "budgeting/issue_explanation.html",
            {
                "threshold_form": form,
                "threshold_cycle": cycle,
                "cycle": cycle,
                "frozen": frozen,
                "read_only": frozen,
            },
        )

    if request.method != "POST":
        return HttpResponse("仅支持 GET 或 POST。", status=405)
    if frozen:
        return HttpResponse("本周期已冻结，只读。", status=409)

    form = P1ThresholdForm(_threshold_data(request))
    if not form.is_valid():
        return render(
            request,
            "budgeting/issue_explanation.html",
            {
                "threshold_form": form,
                "threshold_cycle": cycle,
                "cycle": cycle,
                "frozen": False,
                "read_only": False,
            },
            status=400,
        )

    threshold_cents = yuan_to_cents(form.cleaned_data["threshold_yuan"])
    with transaction.atomic():
        locked_cycle = BudgetCycle.objects.select_for_update().get(pk=cycle.pk)
        if locked_cycle.status == BudgetCycle.Status.FROZEN:
            return HttpResponse("本周期已冻结，只读。", status=409)
        previous_cents = locked_cycle.p1_threshold_cents
        locked_cycle.p1_threshold_cents = threshold_cents
        locked_cycle.save(update_fields=["p1_threshold_cents"])
        audit(
            request.user,
            "P1_THRESHOLD_UPDATED",
            "BudgetCycle",
            locked_cycle.pk,
            {
                "previous_p1_threshold_cents": previous_cents,
                "p1_threshold_cents": threshold_cents,
            },
            cycle=locked_cycle,
        )

    return redirect("management_p1_threshold", cycle_id=cycle.pk)


 
 
 
 
