from __future__ import annotations

import csv
import io
import json
from decimal import Decimal
from functools import wraps

from django import forms
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import OuterRef, Q, Subquery
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from budgeting.models import (
    AdjustmentLine,
    BudgetCycle,
    ManagementQuestion,
    NormalizedValue,
    Project,
    ProjectCycle,
    REPORTS,
    QuestionReply,
    UploadVersion,
    ValidationIssue,
)
from budgeting.services.metrics import METRICS, get_metric
from budgeting.services.trends import (
    DISPLAY_UNIT_LABELS,
    KIND_LABELS,
    aggregate_metric,
    approved_current_uploads,
    latest_report_uploads,
    build_trend,
    build_drilldown,
    export_payload,
    flatten_export,
    metric_choices,
    normalize_display_unit,
    _display_value,
)
from budgeting.services.workflow import active_cycle, audit


DEFAULT_REPORT = "PL_TOTAL_WINE"
DEFAULT_METRICS = (
    "revenue_total",
    "revenue_room",
    "revenue_fb",
    "profit_gop",
    "profit_operating",
    "profit_npi",
    "occ",
    "adr",
    "revpar",
)


class QuestionForm(forms.Form):
    value_id = forms.IntegerField(required=False, min_value=1)
    upload_id = forms.CharField(required=False, max_length=64)
    report_code = forms.ChoiceField(choices=tuple(REPORTS.items()))
    row_code = forms.CharField(max_length=120)
    period = forms.CharField(max_length=40)
    body = forms.CharField(max_length=4000, widget=forms.Textarea(attrs={"rows": 5}))

    def clean_body(self):
        body = self.cleaned_data["body"].strip()
        if not body:
            raise forms.ValidationError("问题内容不能为空。")
        return body


class ReplyForm(forms.Form):
    body = forms.CharField(label="回复内容", max_length=4000, widget=forms.Textarea(attrs={"rows": 4}))

    def clean_body(self):
        body = self.cleaned_data["body"].strip()
        if not body:
            raise forms.ValidationError("回复内容不能为空。")
        return body


class QuestionContextForm(forms.Form):
    project_id = forms.ChoiceField(label="项目")
    report_code = forms.ChoiceField(label="报表", choices=tuple(REPORTS.items()))
    metric = forms.ChoiceField(label="指标")
    period = forms.ChoiceField(label="期间")
    body = forms.CharField(label="问题内容", max_length=4000, widget=forms.Textarea(attrs={"rows": 5}))

    def clean_body(self):
        body = self.cleaned_data["body"].strip()
        if not body:
            raise forms.ValidationError("问题内容不能为空。")
        return body


QuestionCreateForm = QuestionForm
QuestionReplyForm = ReplyForm


def _admin(user):
    return bool(user.is_authenticated and (getattr(user, "is_admin_role", False) or user.is_superuser))


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not _admin(request.user):
            return HttpResponseForbidden("仅管理端可访问。")
        return view(request, *args, **kwargs)

    return wrapped


def cockpit_user_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not (_admin(request.user) or getattr(request.user, "role", "") == "PROJECT"):
            return HttpResponseForbidden("无权访问。")
        return view(request, *args, **kwargs)

    return wrapped


def _cycle(request):
    cycle_id = request.GET.get("cycle") or request.POST.get("cycle")
    if cycle_id:
        try:
            return BudgetCycle.objects.get(pk=int(cycle_id))
        except (BudgetCycle.DoesNotExist, TypeError, ValueError):
            pass
    return active_cycle()


def _report(request):
    report = request.GET.get("report_code") or request.GET.get("report") or request.POST.get("report_code") or DEFAULT_REPORT
    return report if report in REPORTS else DEFAULT_REPORT


def _metric(request, report_code=DEFAULT_REPORT):
    code = request.GET.get("metric") or request.GET.get("metric_code") or request.POST.get("metric") or "revenue_total"
    if get_metric(code):
        return code
    for item in metric_choices(report_code):
        if item["row_code"] == code:
            return code
    return "revenue_total"


def _selected_project_id(request, cycle):
    raw = request.GET.get("project_id") or request.GET.get("project")
    try:
        project_id = int(raw) if raw else None
    except (TypeError, ValueError):
        return None
    if not project_id or not cycle:
        return None
    available_ids = {pc.project_id for pc in latest_report_uploads(cycle)}
    return project_id if project_id in available_ids else None


def _metric_spec(metric_code, report_code):
    metric = METRICS.get(metric_code)
    if metric:
        return metric
    for item in metric_choices(report_code):
        if item["code"] == metric_code:
            return METRICS.get(item["code"], item)
    return {}


def _display_unit(request, metric_code, report_code, default="wan"):
    return normalize_display_unit(_metric_spec(metric_code, report_code), request.GET.get("display_unit"), default=default)


def _drilldown_dimension(request, cycle):
    default_year = cycle.budget_year if cycle else None
    try:
        year = int(request.GET.get("year") or default_year)
    except (TypeError, ValueError):
        year = default_year
    kind = (request.GET.get("kind") or "BUDGET").upper()
    if kind not in KIND_LABELS:
        kind = "BUDGET"
    try:
        month = int(request.GET.get("month")) if request.GET.get("month") else None
    except (TypeError, ValueError):
        month = None
    if month is not None and not 1 <= month <= 12:
        month = None
    return year, kind, month


def _period_label(period):
    raw = (period or "").strip().upper()
    if raw == "YEAR":
        return "年度"
    if len(raw) >= 5 and raw[0] in {"A", "F", "B"} and raw[1:5].isdigit():
        kind = {"A": "实际", "F": "预测", "B": "预算"}[raw[0]]
        month = raw[5:].replace("M", "").replace("-", "").replace("/", "").replace("_", "")
        return f"{raw[1:5]}{kind}" + (f" · {int(month):02d}月" if month.isdigit() and 1 <= int(month) <= 12 else " · 年度")
    if raw.isdigit() and 1 <= int(raw) <= 12:
        return f"{int(raw):02d}月"
    return period or "未指定期间"


def _question_context_form(cycle, data=None, initial=None):
    form = QuestionContextForm(data=data, initial=initial)
    uploads = approved_current_uploads(cycle) if cycle else []
    form.fields["project_id"].choices = [(str(pc.project_id), f"{pc.project.code} {pc.project.name}") for pc in uploads]
    form.fields["metric"].choices = [(item["code"], item["label"]) for item in metric_choices(DEFAULT_REPORT) if item["available"]]
    upload_ids = [pc.current_upload_id for pc in uploads]
    periods = NormalizedValue.objects.filter(upload_id__in=upload_ids).values_list("period", flat=True).distinct().order_by("period") if upload_ids else []
    form.fields["period"].choices = [(period, _period_label(period)) for period in periods]
    return form


def _question_context(value):
    metric = {}
    for code, spec in METRICS.items():
        rows = spec.get("rows") or {}
        if rows.get(value.report_code) == value.row_code:
            metric = {"code": code, **spec}
            break
    return {
        "project_label": f"{value.upload.project.code} {value.upload.project.name}",
        "version_label": value.upload.original_name or value.upload.original_path or str(value.upload_id),
        "version_id": str(value.upload_id),
        "report_label": REPORTS.get(value.report_code, value.report_code),
        "metric_code": metric.get("code", value.row_code),
        "metric_label": metric.get("label", value.row_label or value.row_code),
        "period_label": _period_label(value.period),
        "source_label": f"{value.source_sheet}!{value.source_cell}",
        "source_formula": value.source_formula or "无公式",
        "value_display": _display(value.value_int, value.unit),
    }


def _question_snapshot_context(question):
    snapshot = question.snapshot or {}
    upload = getattr(question, "upload", None)
    version_id = str(getattr(question, "upload_id", "") or snapshot.get("upload_id", ""))
    version_label = (
        getattr(upload, "original_name", "")
        or getattr(upload, "original_path", "")
        or snapshot.get("original_name", "")
        or snapshot.get("original_path", "")
        or snapshot.get("version_label", "")
        or version_id
    )
    return {
        "project_label": f"{snapshot.get('project_code', '')} {snapshot.get('project_name', '')}".strip(),
        "version_label": version_label,
        "version_id": version_id,
        "report_label": REPORTS.get(snapshot.get("report_code"), snapshot.get("report_code", "")),
        "metric_code": snapshot.get("row_code", ""),
        "metric_label": snapshot.get("row_label") or snapshot.get("row_code", ""),
        "period_label": _period_label(snapshot.get("period")),
        "source_label": f"{snapshot.get('source_sheet', '')}!{snapshot.get('source_cell', '')}",
        "source_formula": snapshot.get("source_formula") or "无公式",
        "value_display": _display(snapshot.get("value_int"), snapshot.get("unit")),
    }


def _display(value, unit):
    if value is None:
        return "—"
    if unit == "RATIO":
        return f"{Decimal(value) / 100:.2f}%"
    if unit == "MONEY":
        return f"{Decimal(value) / 100:,.2f} 元"
    return f"{int(value):,}"


def _kpis(cycle, report_code):
    rows = []
    for code in DEFAULT_METRICS:
        metric = METRICS.get(code, {})
        bucket = aggregate_metric(cycle, code, report_code, cycle.budget_year if cycle else 0, "BUDGET", None, data_scope="latest") if cycle else {"value": None, "included_projects": 0}
        rows.append({
            "code": code,
            "label": metric.get("label", code),
            "group": metric.get("group"),
            "unit": metric.get("unit"),
            "value": bucket.get("value"),
            "value_int": bucket.get("value_int"),
            "display": _display_value(bucket.get("value"), metric.get("unit"), "yuan", code, metric.get("aggregation")),
            "included_projects": bucket.get("included_projects", 0),
            "available": bucket.get("value") is not None,
        })
    return rows


@admin_required
def management_dashboard(request):
    cycle = _cycle(request)
    report_code = _report(request)
    kpis = _kpis(cycle, report_code)
    uploads = latest_report_uploads(cycle)
    projects = []
    for pc in uploads:
        cells = []
        for kpi in kpis:
            bucket = aggregate_metric(cycle, kpi["code"], report_code, cycle.budget_year, "BUDGET", None, pc.project_id, data_scope="latest")
            cells.append(_display_value(bucket.get("value"), kpi["unit"], "yuan", kpi["code"], METRICS[kpi["code"]].get("aggregation")))
        projects.append({"project": pc.project, "cells": cells})
    trend = build_trend(cycle, "revenue_total", report_code, data_scope="latest") if cycle else {}
    project_cycles = ProjectCycle.objects.filter(cycle=cycle, project__is_active=True)
    latest_ids = project_cycles.annotate(latest_id=Subquery(
        UploadVersion.objects.filter(cycle=cycle, project_id=OuterRef("project_id"))
        .order_by("-created_at", "-pk").values("pk")[:1]
    )).values("latest_id")
    questions = ManagementQuestion.objects.filter(
        upload__cycle=cycle, upload__project_id__in=project_cycles.values("project_id")
    )
    management_tasks = {
        "submitted_projects": UploadVersion.objects.filter(pk__in=latest_ids, status="SUBMITTED").count(),
        "validation_projects": ValidationIssue.objects.filter(
            Q(severity="P0") | Q(severity="P1", acknowledged=False),
            run__upload_id__in=latest_ids,
        ).values("run__upload__project_id").distinct().count(),
        "unanswered_questions": questions.filter(status="OPEN").count(),
        "answered_questions": questions.filter(status="ANSWERED").count(),
        "open_adjustments": AdjustmentLine.objects.filter(
            cycle=cycle, project_id__in=project_cycles.values("project_id"), status="OPEN"
        ).count(),
    }
    return render(request, "budgeting/cockpit_dashboard.html", {
        "cycle": cycle,
        "report_code": report_code,
        "report_name": REPORTS[report_code],
        "REPORTS": REPORTS,
        "kpis": kpis,
        "projects": projects,
        "approved_projects": len(uploads),
        "active_projects": trend.get("active_project_count", 0),
        "coverage": trend.get("coverage", {"included": 0, "active": 0}),
        "metric_choices": metric_choices(report_code),
        "trend_data": trend,
        "question_count": management_tasks["unanswered_questions"],
        "management_tasks": management_tasks,
    })


@admin_required
def management_trend(request):
    cycle = _cycle(request)
    report_code = _report(request)
    metric_code = _metric(request, report_code)
    metric = _metric_spec(metric_code, report_code)
    selected_project_id = _selected_project_id(request, cycle)
    display_unit = _display_unit(request, metric_code, report_code)
    trend = build_trend(cycle, metric_code, report_code, project_id=selected_project_id, display_unit=display_unit, data_scope="latest") if cycle else {}
    project_choices = [
        {"id": pc.project_id, "code": pc.project.code, "name": pc.project.name}
        for pc in latest_report_uploads(cycle)
    ] if cycle else []
    amount_metric = metric.get("unit") == "MONEY" and (metric.get("aggregation") or "").upper() != "DERIVED"
    return render(request, "budgeting/cockpit_trend.html", {
        "cycle": cycle,
        "report_code": report_code,
        "metric_code": metric_code,
        "report_name": REPORTS[report_code],
        "REPORTS": REPORTS,
        "metric_choices": metric_choices(report_code),
        "trend": trend,
        "trend_json": json.dumps(trend, ensure_ascii=False),
        "data_url": reverse("cockpit_trend_data"),
        "unit": metric.get("unit", "MONEY"),
        "display_unit": display_unit,
        "display_unit_options": [{"value": value, "label": DISPLAY_UNIT_LABELS[value]} for value in ("wan", "yuan")] if amount_metric else [],
        "amount_metric": amount_metric,
        "project_choices": project_choices,
        "selected_project_id": selected_project_id,
        "kind_labels": KIND_LABELS,
    })


@admin_required
def cockpit_trend_data(request):
    cycle = _cycle(request)
    report_code = _report(request)
    metric_code = _metric(request, report_code)
    project_id = _selected_project_id(request, cycle)
    display_unit = _display_unit(request, metric_code, report_code)
    if not cycle:
        return JsonResponse({"metric": metric_code, "report_code": report_code, "series": [], "annual": [], "coverage": {"included": 0, "active": 0}})
    return JsonResponse(build_trend(cycle, metric_code, report_code, project_id=project_id, display_unit=display_unit, data_scope="latest"))


@admin_required
def cockpit_export(request):
    cycle = _cycle(request)
    report_code = _report(request)
    metric_code = _metric(request, report_code)
    project_id = _selected_project_id(request, cycle)
    display_unit = _display_unit(request, metric_code, report_code)
    payload = build_trend(cycle, metric_code, report_code, project_id=project_id, display_unit=display_unit, data_scope="latest") if cycle else {}
    if request.GET.get("format", "csv").lower() == "json":
        return JsonResponse(export_payload(payload))
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=["scope", "label", "year", "kind", "month", "value", "included_projects", "active_projects", "annual_only"])
    writer.writeheader()
    writer.writerows(flatten_export(payload))
    response = HttpResponse("\ufeff" + stream.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="cockpit-{metric_code}.csv"'
    return response


@admin_required
def cockpit_project(request, project_id):
    cycle = _cycle(request)
    project = get_object_or_404(Project, pk=project_id)
    pc = next((item for item in latest_report_uploads(cycle, project_id=project.id)), None) if cycle else None
    metrics = []
    if cycle and pc:
        for code, metric in METRICS.items():
            if any(item["row_code"] for item in metric_choices(_report(request)) if item["code"] == code):
                metrics.append({"code": code, "label": metric.get("label", code), "trend": build_trend(cycle, code, _report(request), project_id=project.id, data_scope="latest")})
    return render(request, "budgeting/cockpit_project.html", {"cycle": cycle, "project": project, "project_cycle": pc, "metrics": metrics, "report_code": _report(request)})


@admin_required
def cockpit_drilldown(request):
    cycle = _cycle(request)
    report_code = _report(request)
    metric_code = _metric(request, report_code)
    project_filter = _selected_project_id(request, cycle)
    display_unit = _display_unit(request, metric_code, report_code)
    year, kind, month = _drilldown_dimension(request, cycle)
    payload = build_drilldown(cycle, metric_code, report_code, year=year, kind=kind, month=month, project_id=project_filter, display_unit=display_unit, data_scope="latest")
    payload["cycle"] = cycle.id if cycle else None
    payload["rows"] = payload.get("projects", [])
    if request.GET.get("format", "").lower() == "json":
        return JsonResponse(payload)
    return render(request, "budgeting/cockpit_drilldown.html", {"cycle": cycle, "metric_code": metric_code, "report_code": report_code, "metric_label": payload.get("metric_label", (METRICS.get(metric_code) or {}).get("label", metric_code)), "display_unit": display_unit, "project_id": project_filter, "year": year, "kind": kind, "month": month, "drilldown": payload, "rows": payload.get("projects", [])})


def _current_value(value_id=None, upload_id=None, report_code=None, row_code=None, period=None):
    qs = NormalizedValue.objects.select_related("upload__project", "upload__cycle")
    if value_id:
        value = qs.filter(pk=value_id).first()
    else:
        value = qs.filter(upload_id=upload_id, report_code=report_code, row_code=row_code, period=period).first()
    if not value or value.upload.status != UploadVersion.Status.APPROVED:
        return None
    current = ProjectCycle.objects.filter(cycle=value.upload.cycle, project=value.upload.project, current_upload=value.upload, current_upload__status=UploadVersion.Status.APPROVED).exists()
    return value if current else None


def _snapshot(value):
    return {
        "value_id": value.pk,
        "upload_id": str(value.upload_id),
        "project_id": value.upload.project_id,
        "project_code": value.upload.project.code,
        "project_name": value.upload.project.name,
        "cycle_id": value.upload.cycle_id,
        "budget_year": value.upload.cycle.budget_year,
        "report_code": value.report_code,
        "row_code": value.row_code,
        "row_label": value.row_label,
        "period": value.period,
        "data_year": value.data_year,
        "data_kind": value.data_kind,
        "month": value.month,
        "unit": value.unit,
        "value_int": value.value_int,
        "ratio_num": value.ratio_num,
        "ratio_den": value.ratio_den,
        "source_sheet": value.source_sheet,
        "source_cell": value.source_cell,
        "source_formula": value.source_formula,
    }


def _question_visible(question, user):
    return _admin(user) or (getattr(user, "role", "") == "PROJECT" and question.upload.project_id == user.project_id)


def _frozen(question):
    return question.upload.cycle.status == BudgetCycle.Status.FROZEN


@cockpit_user_required
def cockpit_questions(request):
    qs = ManagementQuestion.objects.select_related("upload__project", "upload__cycle", "created_by").prefetch_related("replies__actor").order_by("-created_at")
    if not _admin(request.user):
        qs = qs.filter(upload__project_id=request.user.project_id)
    status = request.GET.get("status")
    if status in {"OPEN", "ANSWERED", "CLOSED"}:
        qs = qs.filter(status=status)
    return render(request, "budgeting/cockpit_questions.html", {"questions": qs, "status": status or "", "is_admin": _admin(request.user)})


@cockpit_user_required
def cockpit_question(request, question_id=None):
    question = get_object_or_404(ManagementQuestion.objects.select_related("upload__project", "upload__cycle", "created_by").prefetch_related("replies__actor"), pk=question_id) if question_id else None
    if question and not _question_visible(question, request.user):
        return HttpResponseForbidden("无权访问该问题。")
    if request.method == "GET":
        initial = {}
        context = None
        if request.GET.get("value_id"):
            value = _current_value(value_id=request.GET.get("value_id"))
            if value and (_admin(request.user) or value.upload.project_id == request.user.project_id):
                initial = {"value_id": value.pk, "upload_id": str(value.upload_id), "report_code": value.report_code, "row_code": value.row_code, "period": value.period}
                context = _question_context(value)
        question_context = _question_snapshot_context(question) if question else context
        return render(request, "budgeting/cockpit_question.html", {"question": question, "form": QuestionForm(initial=initial), "context_form": _question_context_form(_cycle(request)), "context": question_context, "context_bound": bool(context or question), "reply_form": ReplyForm(), "is_admin": _admin(request.user), "frozen": _frozen(question) if question else False})
    if question and _frozen(question):
        return HttpResponse("本周期已冻结，只读。", status=409)
    action = request.POST.get("action", "")
    if question and action in {"close", "reopen"} or question and request.POST.get("status") in {"OPEN", "CLOSED"}:
        if not _admin(request.user):
            return HttpResponseForbidden("仅管理端可关闭或重开问题。")
        target = action or request.POST.get("status")
        new_status = "CLOSED" if target in {"close", "CLOSED"} else "OPEN"
        with transaction.atomic():
            locked = ManagementQuestion.objects.select_for_update().get(pk=question.pk)
            locked.status = new_status
            locked.save(update_fields=["status", "updated_at"])
            audit(request.user, "QUESTION_CLOSED" if new_status == "CLOSED" else "QUESTION_REOPENED", "ManagementQuestion", locked.pk, {"status": new_status}, project=locked.upload.project, cycle=locked.upload.cycle, upload=locked.upload)
        return redirect("cockpit_question", question_id=question.pk)
    if question and action in {"reply", "answer"}:
        form = ReplyForm(request.POST)
        if not form.is_valid():
            return render(request, "budgeting/cockpit_question.html", {"question": question, "form": QuestionForm(), "reply_form": form, "is_admin": _admin(request.user), "frozen": False}, status=400)
        with transaction.atomic():
            locked = ManagementQuestion.objects.select_for_update().select_related("upload__project", "upload__cycle").get(pk=question.pk)
            if locked.status == "CLOSED":
                return HttpResponse("问题已关闭。", status=409)
            reply = QuestionReply.objects.create(question=locked, actor=request.user, body=form.cleaned_data["body"])
            locked.status = "ANSWERED"
            locked.save(update_fields=["status", "updated_at"])
            audit(request.user, "QUESTION_REPLIED", "QuestionReply", reply.pk, {"question_id": locked.pk}, project=locked.upload.project, cycle=locked.upload.cycle, upload=locked.upload)
        return redirect("cockpit_question", question_id=question.pk)
    if question:
        return HttpResponse("不支持的操作。", status=400)
    if not _admin(request.user):
        return HttpResponseForbidden("仅管理端可创建问题。")
    context = None
    context_form = None
    if request.POST.get("value_id"):
        form = QuestionForm(request.POST)
        if not form.is_valid():
            return render(request, "budgeting/cockpit_question.html", {"question": None, "form": form, "context_form": _question_context_form(_cycle(request)), "context": context, "context_bound": True, "reply_form": ReplyForm(), "is_admin": True, "frozen": False}, status=400)
        value = _current_value(form.cleaned_data.get("value_id"), form.cleaned_data.get("upload_id"), form.cleaned_data["report_code"], form.cleaned_data["row_code"], form.cleaned_data["period"])
    else:
        cycle = _cycle(request)
        context_form = _question_context_form(cycle, data=request.POST)
        if not context_form.is_valid():
            return render(request, "budgeting/cockpit_question.html", {"question": None, "form": QuestionForm(), "context_form": context_form, "context": context, "context_bound": False, "reply_form": ReplyForm(), "is_admin": True, "frozen": False}, status=400)
        project_id = int(context_form.cleaned_data["project_id"])
        report_code = context_form.cleaned_data["report_code"]
        metric_code = context_form.cleaned_data["metric"]
        metric = METRICS.get(metric_code) or {}
        row_code = (metric.get("rows") or {}).get(report_code)
        pc = next(iter(approved_current_uploads(cycle, project_id=project_id)), None) if cycle and row_code else None
        value = _current_value(None, str(pc.current_upload_id) if pc else None, report_code, row_code, context_form.cleaned_data["period"])
        if not value:
            context_form.add_error("period", "该项目当前批准版本没有对应的标准化值，请从趋势穿透页选择源单元格。")
            return render(request, "budgeting/cockpit_question.html", {"question": None, "form": QuestionForm(), "context_form": context_form, "context": context, "context_bound": False, "reply_form": ReplyForm(), "is_admin": True, "frozen": False}, status=400)
        form = QuestionForm({"value_id": value.pk, "upload_id": str(value.upload_id), "report_code": value.report_code, "row_code": value.row_code, "period": value.period, "body": context_form.cleaned_data["body"]})
        if not form.is_valid():
            return render(request, "budgeting/cockpit_question.html", {"question": None, "form": form, "context_form": context_form, "context": context, "context_bound": True, "reply_form": ReplyForm(), "is_admin": True, "frozen": False}, status=400)
        context = _question_context(value)
    if not value:
        form.add_error("value_id", "必须选择当前已批准版本中的真实标准化值。")
        return render(request, "budgeting/cockpit_question.html", {"question": None, "form": form, "context_form": context_form or _question_context_form(_cycle(request)), "context": context, "context_bound": bool(context), "reply_form": ReplyForm(), "is_admin": True, "frozen": False}, status=400)
    if value.upload.cycle.status == BudgetCycle.Status.FROZEN:
        return HttpResponse("本周期已冻结，只读。", status=409)
    snapshot = _snapshot(value)
    with transaction.atomic():
        question = ManagementQuestion.objects.create(upload=value.upload, report_code=value.report_code, row_code=value.row_code, period=value.period, snapshot=snapshot, body=form.cleaned_data["body"], status="OPEN", created_by=request.user)
        audit(request.user, "QUESTION_CREATED", "ManagementQuestion", question.pk, {"snapshot": snapshot}, project=value.upload.project, cycle=value.upload.cycle, upload=value.upload)
    return redirect("cockpit_question", question_id=question.pk)


__all__ = [
    "QuestionForm",
    "ReplyForm",
    "cockpit_drilldown",
    "cockpit_export",
    "cockpit_project",
    "cockpit_question",
    "cockpit_questions",
    "cockpit_trend_data",
    "management_dashboard",
    "management_trend",
]
