"""Views for administrator-owned, versioned management metric history.

The parser and persistence rules live in budgeting.services.management_metrics.
These views only handle request validation, staged uploads, presentation and
CSV export. In particular, an imported workbook is never written directly to
the database by a view and an earlier confirmed batch is never replaced.
"""

import csv
import json
import secrets
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, OperationalError
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from budgeting.models import BudgetCycle, IndicatorProject, ManagementMetricBatch, Project, REPORTS
from budgeting.services import management_metrics as service


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
PREVIEW_TTL_SECONDS = 60 * 60
STAGING_SESSION_KEY = "management_metric_preview"

REPORT_CHOICES = (
    ("PL_TOTAL_WINE", "损益汇总表（含名酒）"),
    ("PL_TOTAL_NOWINE", "损益汇总表（不含名酒）"),
    ("PL_ZZ_WINE", "自营损益表（含名酒）"),
    ("PL_ZZ_NOWINE", "自营损益表（不含名酒）"),
)
DATA_KIND_CHOICES = (
    ("ACTUAL", "实际"),
    ("FORECAST", "预测"),
)
MONEY_UNIT_CHOICES = (("WAN", "万元"), ("YUAN", "元"))


def _is_admin(user):
    return bool(user.is_authenticated and (user.is_superuser or getattr(user, "role", "") == "ADMIN"))


def _year(value, default):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if 1900 <= value <= 2200 else default


def _positive_id(value, *, default=None):
    if value in (None, ""):
        return default
    value = str(value)
    if not value.isascii() or not value.isdecimal() or len(value) > 10:
        raise Http404("对象不存在")
    parsed = int(value)
    return parsed if parsed > 0 else default


def _signed_id(value):
    value = str(value)
    if not value or not value.isascii() or len(value) > 11:
        raise Http404("项目不存在")
    digits = value[1:] if value.startswith("-") else value
    if not digits.isdecimal() or int(digits) == 0:
        raise Http404("项目不存在")
    return int(value)


def _month(value, default=0):
    try:
        month = int(value)
    except (TypeError, ValueError):
        return default
    return month if 0 <= month <= 12 else default


def _compare_index(value, default=2):
    try:
        index = int(value)
    except (TypeError, ValueError):
        return default
    return index if index in (0, 1, 2) else default


def _visible_projects(user):
    projects = IndicatorProject.objects.select_related("project").order_by("name", "pk")
    if _is_admin(user):
        return projects
    project_id = getattr(user, "project_id", None)
    return projects.filter(project_id=project_id) if project_id else projects.none()


def _project_id_for_request(request, projects):
    is_admin = _is_admin(request.user)
    bound_project_id = getattr(request.user, "project_id", None)
    if not is_admin and not bound_project_id:
        raise PermissionDenied("项目账号必须绑定项目。")
    raw = request.GET.get("project", "")
    if not raw:
        if is_admin:
            return None
        identity = projects.filter(project_id=bound_project_id).first()
        return identity.pk if identity is not None else -bound_project_id
    if raw.startswith("-"):
        project_id = _signed_id(raw)
        # Negative IDs are transient identities created by the comparison
        # service for budget projects that have no imported history row.
        project = get_object_or_404(Project, pk=abs(project_id))
        if not is_admin and bound_project_id != project.pk:
            raise Http404("项目不存在")
        return project_id
    project_id = _positive_id(raw)
    if not projects.filter(pk=project_id).exists():
        raise Http404("项目不存在")
    if not is_admin and not projects.filter(pk=project_id, project_id=bound_project_id).exists():
        raise Http404("项目不存在")
    return project_id


def _choices_from_service(name, fallback):
    choices = getattr(service, name, None)
    if not choices:
        return fallback
    try:
        return tuple((str(item[0]), str(item[1])) for item in choices)
    except (TypeError, IndexError):
        return fallback


def _metric_choices():
    return _choices_from_service("METRIC_CHOICES", (("TOTAL_REV", "收入合计"),))


def _report_choices():
    return _choices_from_service("REPORT_CHOICES", tuple(REPORTS.items()) or REPORT_CHOICES)


def _metric_label(metric, result=None):
    if result and result.get("metric_label"):
        return str(result["metric_label"])
    return dict(_metric_choices()).get(metric, metric or "")


def _as_decimal(value):
    if value in (None, "", "—", "-"):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _is_ratio_metric(metric, result=None):
    unit = str((result or {}).get("unit", "")).upper()
    code = str(metric or "").upper()
    return (
        "%" in unit
        or "RATIO" in unit
        or unit == "RATIO"
        or code in {"OCC", "OCCUPANCY", "OCCUPANCY_RATE", "RATE", "MARGIN"}
        or code.endswith("_RATE")
    )


def _is_adr_metric(metric):
    code = str(metric or "").upper()
    return code == "ADR" or code.endswith("_ADR")


def _unit_label(metric, result=None):
    """Return a business-facing unit label for the comparison page/export."""
    return "百分比" if _is_ratio_metric(metric, result) else "元"


def _format_amount(value, *, ratio=False, adr=False):
    number = _as_decimal(value)
    if number is None:
        return "—"
    if ratio:
        display = number * Decimal("100")
        return f"{display:,.2f}%"
    if adr:
        return f"{number:,.2f}"
    return f"{number:,.0f}"


def _format_change(value, *, ratio=False):
    number = _as_decimal(value)
    if number is None:
        return "—"
    if ratio:
        display = number * Decimal("100")
        return f"{display:+,.2f} 个百分点"
    return f"{number:+,.2f}%"


def _format_delta(value, *, ratio=False, adr=False):
    if ratio:
        number = _as_decimal(value)
        if number is None:
            return "—"
        return f"{number * Decimal('100'):+,.2f} 个百分点"
    return _format_amount(value, adr=adr)


def _periods(result, year, base_year):
    periods = result.get("periods") if isinstance(result, dict) else None
    if not periods:
        periods = [f"{base_year}实际", f"{base_year + 1}实际", f"{year - 1}预测", f"{year}预算"]
    normalized = []
    for index, period in enumerate(periods):
        if isinstance(period, dict):
            item = dict(period)
            item.setdefault("label", item.get("name", str(item.get("year", index + 1))))
        else:
            item = {"label": str(period)}
        item["label"] = str(item.get("label", ""))
        normalized.append(item)
    return normalized


def _display_row(row, *, metric, result=None):
    item = dict(row) if isinstance(row, dict) else {"name": str(row)}
    values = (list(item.get("values") or []) + [None] * 4)[:4]
    ratio = _is_ratio_metric(metric, result)
    adr = _is_adr_metric(metric)
    item["display_values"] = [_format_amount(value, ratio=ratio, adr=adr) for value in values]
    item["display_delta"] = _format_delta(item.get("delta"), ratio=ratio, adr=adr)
    item["display_growth"] = _format_change(item.get("growth"), ratio=ratio)
    item.setdefault("name", item.get("project_name") or item.get("label") or "未命名项目")
    item.setdefault("project_id", item.get("id"))
    return item


def _display_trend(row, *, metric, result=None):
    item = dict(row) if isinstance(row, dict) else {"label": str(row)}
    values = (list(item.get("values") or []) + [None] * 4)[:4]
    ratio = _is_ratio_metric(metric, result)
    adr = _is_adr_metric(metric)
    item["display_values"] = [_format_amount(value, ratio=ratio, adr=adr) for value in values]
    item.setdefault("label", item.get("month") or "")
    return item


def _comparison_context(request):
    cycles = BudgetCycle.objects.order_by("-budget_year", "-revision_no")
    cycle_raw = request.GET.get("cycle", "")
    cycle = None
    cycle_unbound = str(cycle_raw).lower() in {"unbound", "none", "off"}
    requested_year = _year(request.GET.get("year"), timezone.now().year + 1)
    if cycle_raw and not cycle_unbound:
        cycle = get_object_or_404(cycles, pk=_positive_id(cycle_raw))
    elif not cycle_raw:
        # Open on the latest round for the selected budget year. An explicit
        # unbound choice remains available for history-only comparisons.
        cycle = cycles.filter(budget_year=requested_year).first()
    year = cycle.budget_year if cycle else requested_year
    base_year = _year(request.GET.get("base_year"), year - 3)
    report_code = request.GET.get("report_code", "PL_TOTAL_NOWINE")
    if report_code not in dict(_report_choices()):
        return None, HttpResponseBadRequest("请选择有效的报表口径。")
    metric = request.GET.get("metric", "") or _metric_choices()[0][0]
    if metric not in dict(_metric_choices()):
        return None, HttpResponseBadRequest("请选择有效的管理指标。")
    month = _month(request.GET.get("month"), 0)
    compare_index = _compare_index(request.GET.get("compare_index"), 2)
    projects = _visible_projects(request.user)
    project_id = _project_id_for_request(request, projects)
    allowed_project_ids = None if _is_admin(request.user) else {request.user.project_id}
    result = service.comparison_data(
        cycle=cycle,
        year=year,
        report_code=report_code,
        metric=metric,
        month=month,
        base_year=base_year,
        project_id=project_id,
        allowed_project_ids=allowed_project_ids,
        compare_index=compare_index,
    )
    result = dict(result or {})
    periods = _periods(result, year, base_year)
    rows = [_display_row(row, metric=metric, result=result) for row in result.get("rows", [])]
    totals = result.get("totals")
    if isinstance(totals, dict):
        totals.setdefault("name", "合计")
        totals = _display_row(totals, metric=metric, result=result)
    trend = [_display_trend(row, metric=metric, result=result) for row in result.get("trend", [])]
    coverage = list((totals or {}).get("coverage") or []) if isinstance(totals, dict) else []
    expected = (totals or {}).get("expected") if isinstance(totals, dict) else None
    coverage_items = [
        {
            "label": periods[index].get("label", "") if index < len(periods) else f"第{index + 1}期",
            "included": count,
            "expected": expected,
        }
        for index, count in enumerate(coverage)
    ]
    result.update(
        rows=rows,
        totals=totals,
        periods=periods,
        trend=trend,
        metric=metric,
        metric_label=_metric_label(metric, result),
        unit_label=_unit_label(metric, result),
        report_code=report_code,
        year=year,
        base_year=base_year,
        month=month,
        compare_index=compare_index,
        is_ratio=_is_ratio_metric(metric, result),
        coverage_items=coverage_items,
    )
    batches = list(ManagementMetricBatch.objects.order_by("-created_at")[:20]) if _is_admin(request.user) else []
    report_labels = dict(_report_choices())
    for batch in batches:
        batch.data_kind_label = "实际" if batch.data_kind == "ACTUAL" else "预测"
        batch.report_label = report_labels.get(batch.report_code, batch.report_code)
    return {
        "cycles": cycles,
        "cycle": cycle,
        "cycle_unbound": cycle_unbound,
        "projects": projects,
        "selected_project": project_id,
        "comparison": result,
        "periods": periods,
        "metric_choices": _metric_choices(),
        "report_choices": _report_choices(),
        "selected_metric": metric,
        "selected_report_code": report_code,
        "selected_year": year,
        "selected_base_year": base_year,
        "selected_month": month,
        "selected_compare_index": compare_index,
        "compare_choices": tuple(
            (index, period.get("label", ""))
            for index, period in enumerate(periods[:3])
        ),
        "month_options": tuple({"value": index, "label": f"{index}月"} for index in range(1, 13)),
        "is_admin": _is_admin(request.user),
        "batches": batches,
    }, None


@login_required
def management_metrics(request):
    context, error = _comparison_context(request)
    if error:
        return error
    return render(request, "budgeting/management_metrics.html", context)


def _staging_dir():
    directory = Path(settings.BUDGET_STORAGE_ROOT) / "management_metrics" / "previews"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _token_is_valid(token):
    return len(token) == 48 and token.isascii() and all(char in "0123456789abcdef" for char in token)


def _preview_rows(preview):
    money_unit = preview.get("money_unit", "WAN")
    rows = []
    for raw in (preview.get("rows") or [])[:500]:
        row = dict(raw) if isinstance(raw, dict) else {"value": raw}
        row["project_label"] = row.get("project_name") or row.get("name") or row.get("project") or "—"
        row["metric_label"] = row.get("metric_label") or dict(_metric_choices()).get(row.get("metric"), row.get("metric") or "—")
        row["year_label"] = row.get("year") or "—"
        month = row.get("month", 0)
        row["month_label"] = "全年" if month in (0, "0", None, "") else f"{month}月"
        metric = row.get("metric")
        ratio = _is_ratio_metric(metric, {"unit": row.get("unit", "")})
        adr = _is_adr_metric(metric)
        value = _as_decimal(row.get("value"))
        if value is not None and not ratio and not adr and money_unit == "WAN":
            value /= Decimal("10000")
        row["value_display"] = _format_amount(value, ratio=ratio, adr=adr)
        rows.append(row)
    return rows


def _preview_context(preview):
    return {
        "preview_rows": _preview_rows(preview),
        "preview_projects": preview.get("projects") or [],
        "preview_years": preview.get("years") or [],
        "preview_value_count": len(preview.get("rows") or []),
        "preview_sha256": preview.get("sha256", ""),
        "unlinked_projects": preview.get("unlinked_projects") or [],
        "preview_unit_label": (
            "万元（ADR、RevPAR按元）" if preview.get("money_unit", "WAN") == "WAN"
            else "元（ADR、RevPAR按元）"
        ),
    }


@login_required
def management_metric_import(request):
    if not _is_admin(request.user):
        return HttpResponseForbidden("只有管理端可以导入管理指标底稿。")
    context = {
        "report_choices": _report_choices(),
        "data_kind_choices": DATA_KIND_CHOICES,
        "money_unit_choices": MONEY_UNIT_CHOICES,
        "latest_batch_id": service.latest_batch_id(),
        "default_report_code": "PL_TOTAL_NOWINE",
    }
    if request.method != "POST":
        return render(request, "budgeting/management_metric_import.html", context)

    upload = request.FILES.get("file")
    suffix = Path(upload.name).suffix.lower() if upload else ""
    if not upload or suffix != ".xlsx" or upload.size > MAX_UPLOAD_BYTES:
        context["error"] = "请选择不超过 20 MB 的 .xlsx 管理指标底稿。"
        return render(request, "budgeting/management_metric_import.html", context)
    money_unit = request.POST.get("money_unit", "WAN")
    data_kind = request.POST.get("data_kind", "ACTUAL")
    report_code = request.POST.get("report_code", "PL_TOTAL_NOWINE")
    if money_unit not in dict(MONEY_UNIT_CHOICES) or data_kind not in dict(DATA_KIND_CHOICES):
        return HttpResponseBadRequest("金额单位或数据类型无效。")
    if report_code not in dict(_report_choices()):
        return HttpResponseBadRequest("年度或报表口径无效。")

    token = secrets.token_hex(24)
    source = _staging_dir() / f"{token}.xlsx"
    meta_path = _staging_dir() / f"{token}.json"
    with source.open("wb") as destination:
        for chunk in upload.chunks():
            destination.write(chunk)
    try:
        preview = service.preview_import(source, money_unit=money_unit, data_kind=data_kind, report_code=report_code)
    except (OSError, ValueError, TypeError) as exc:
        source.unlink(missing_ok=True)
        context["error"] = str(exc)
        return render(request, "budgeting/management_metric_import.html", context)

    preview = dict(preview or {})
    preview.update(
        original_name=Path(upload.name).name,
        money_unit=money_unit,
        data_kind=data_kind,
        report_code=report_code,
        report_label=dict(_report_choices()).get(report_code, report_code),
    )
    meta = {
        "preview": preview,
        "created": time.time(),
        "owner": request.user.pk,
        "expected_latest_id": service.latest_batch_id(),
    }
    meta_path.write_text(json.dumps(meta, cls=DjangoJSONEncoder), encoding="utf-8")
    request.session[STAGING_SESSION_KEY] = token
    context.update(preview=preview, token=token, **_preview_context(preview))
    return render(request, "budgeting/management_metric_import.html", context)


@login_required
@require_POST
def management_metric_confirm(request):
    if not _is_admin(request.user):
        return HttpResponseForbidden("只有管理端可以确认管理指标导入。")
    token = request.POST.get("token", "")
    if not _token_is_valid(token) or request.session.get(STAGING_SESSION_KEY) != token:
        return HttpResponseBadRequest("预览已失效，请重新上传。")
    meta_path = _staging_dir() / f"{token}.json"
    source = _staging_dir() / f"{token}.xlsx"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return HttpResponseBadRequest("预览已失效，请重新上传。")
    if meta.get("owner") != request.user.pk or time.time() - float(meta.get("created", 0)) > PREVIEW_TTL_SECONDS:
        return HttpResponseBadRequest("预览已过期，请重新上传。")
    reason = request.POST.get("reason", "").strip()
    if not reason:
        context = {
            "error": "确认导入必须填写修订原因。",
            "preview": meta.get("preview", {}),
            "token": token,
            "report_choices": _report_choices(),
            "data_kind_choices": DATA_KIND_CHOICES,
            "money_unit_choices": MONEY_UNIT_CHOICES,
            "latest_batch_id": service.latest_batch_id(),
        }
        context.update(_preview_context(context["preview"]))
        return render(request, "budgeting/management_metric_import.html", context, status=400)
    if not source.is_file():
        return HttpResponseBadRequest("预览原件已不存在，请重新上传。")

    preview = dict(meta.get("preview") or {})
    expected_latest_id = meta.get("expected_latest_id")
    posted_latest_id = request.POST.get("expected_latest_id", "")
    if posted_latest_id:
        try:
            if int(posted_latest_id) != int(expected_latest_id):
                return HttpResponseBadRequest("最新批次已变化，请重新预览后确认。")
        except (TypeError, ValueError):
            return HttpResponseBadRequest("预览批次信息无效，请重新上传。")

    claimed = meta_path.with_suffix(".claimed")
    try:
        meta_path.rename(claimed)
    except FileNotFoundError:
        return HttpResponseBadRequest("此预览已经确认，请勿重复提交。")
    try:
        batch = service.confirm_import(
            preview,
            source_path=source,
            user=request.user,
            reason=reason,
            expected_latest_id=expected_latest_id,
        )
    except (ValueError, PermissionError, OSError) as exc:
        claimed.rename(meta_path)
        return HttpResponseBadRequest(str(exc))
    except (IntegrityError, OperationalError):
        claimed.rename(meta_path)
        return HttpResponseBadRequest("其他操作正在更新管理指标，请重新预览后再确认。")

    claimed.unlink(missing_ok=True)
    source.unlink(missing_ok=True)
    request.session.pop(STAGING_SESSION_KEY, None)
    messages.success(request, "管理指标已导入，历史记录已保留。")
    return redirect("management_metrics")


@login_required
def management_metric_source(request, batch_id):
    if not _is_admin(request.user):
        return HttpResponseForbidden("只有管理端可以下载管理指标原件。")
    batch = get_object_or_404(ManagementMetricBatch, pk=_positive_id(batch_id))
    try:
        handle = batch.original.open("rb")
    except (OSError, ValueError):
        raise Http404("原始底稿不存在")
    return FileResponse(handle, as_attachment=True, filename=batch.original_name or "管理指标底稿.xlsx")


def _csv_value(value, *, ratio=False, adr=False):
    if value is None:
        return "—"
    number = _as_decimal(value)
    if number is None:
        return "—"
    if ratio:
        display = number * Decimal("100")
        return f"{display:.2f}%"
    if adr:
        return f"{number:.2f}"
    return f"{number:.0f}"


def _csv_safe_text(value):
    text = "" if value is None else str(value)
    return f"'{text}" if text[:1] in ("=", "+", "-", "@") else text


def _csv_delta(value, *, ratio=False, adr=False):
    if ratio:
        number = _as_decimal(value)
        return "—" if number is None else f"{number * Decimal('100'):+,.2f} 个百分点"
    return _csv_value(value, adr=adr)


@login_required
def management_metric_export(request):
    context, error = _comparison_context(request)
    if error:
        return error
    comparison = context["comparison"]
    metric = comparison["metric"]
    ratio = _is_ratio_metric(metric, comparison)
    adr = _is_adr_metric(metric)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response.write("﻿")
    response["Content-Disposition"] = f'attachment; filename="management_metrics_{comparison["year"]}.csv"'
    writer = csv.writer(response)
    period_labels = [str(item.get("label", "")) for item in comparison["periods"]]
    growth_label = "变化（百分点）" if ratio else "增长百分比"
    unit_label = comparison.get("unit_label") or _unit_label(metric, comparison)
    report_label = dict(_report_choices()).get(comparison.get("report_code"), comparison.get("report_code", ""))
    comparison_label = comparison.get("comparison_label") or "比较基期"
    metadata_headers = ["指标", "金额单位", "报表口径", "比较基期", "预算年度"]
    delta_label = "变化（百分点）" if ratio else "差额"
    writer.writerow(["项目", *metadata_headers, *period_labels, delta_label, *([] if ratio else [growth_label])])
    for row in comparison.get("rows", []):
        line = [
            _csv_safe_text(row.get("name", "")),
            _csv_safe_text(comparison.get("metric_label", metric)),
            _csv_safe_text(unit_label),
            _csv_safe_text(report_label),
            _csv_safe_text(comparison_label),
            _csv_safe_text(comparison.get("year", "")),
            *[_csv_value(value, ratio=ratio, adr=adr) for value in (list(row.get("values") or []) + [None] * 4)[:4]],
            _csv_delta(row.get("delta"), ratio=ratio, adr=adr),
        ]
        if not ratio:
            line.append(_format_change(row.get("growth"), ratio=ratio))
        writer.writerow(line)
    totals = comparison.get("totals")
    if isinstance(totals, dict):
        line = [
            _csv_safe_text(totals.get("name", "合计")),
            _csv_safe_text(comparison.get("metric_label", metric)),
            _csv_safe_text(unit_label),
            _csv_safe_text(report_label),
            _csv_safe_text(comparison_label),
            _csv_safe_text(comparison.get("year", "")),
            *[_csv_value(value, ratio=ratio, adr=adr) for value in (list(totals.get("values") or []) + [None] * 4)[:4]],
            _csv_delta(totals.get("delta"), ratio=ratio, adr=adr),
        ]
        if not ratio:
            line.append(_format_change(totals.get("growth"), ratio=ratio))
        writer.writerow(line)
    return response
