import json
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from budgeting.excel.money import cents_to_yuan, yuan_to_cents
from budgeting.models import BudgetCycle, BudgetScenario, Project, ProjectCycle, REPORTS, UploadVersion
from budgeting.services import active_cycle
from budgeting.services.planning_context import scenario_planning_context
from budgeting.services.scenarios import (
    COST_MODE_LABELS,
    COST_MODES,
    DRIVER_LABELS,
    DRIVERS,
    MONTHS,
    ScenarioError,
    calculate_scenario,
    clone_scenario,
    issue_scenario,
    scenario_source_identity,
)


DRIVER_UNITS = {
    "OCC": "%",
    "ADR": "元",
    "ROOMS": "间",
    "ROOM_REV": "万元",
}
UNIT_LABELS = {"MONEY": "万元", "COUNT": "数量", "RATIO": "%"}
PER_ROOM_METRIC_CODES = frozenset({"R0030", "R0013", "R0031", "R0014"})


def _admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_admin_role:
            raise Http404()
        return view(request, *args, **kwargs)

    return wrapped


def _cycle_from_request(request, *, key="cycle"):
    value = request.POST.get(key) if request.method == "POST" else request.GET.get(key)
    if value:
        try:
            return BudgetCycle.objects.get(pk=value)
        except (BudgetCycle.DoesNotExist, ValueError, TypeError):
            raise ScenarioError("预算周期不存在")
    return active_cycle()


def _scenario_queryset():
    return (
        BudgetScenario.objects.select_related(
            "baseline",
            "baseline__project",
            "baseline__cycle",
            "created_by",
            "batch",
        )
        .order_by("-updated_at", "-created_at")
    )


def _project_baselines(cycle):
    if cycle is None:
        return []
    pcs = {
        item.project_id: item
        for item in ProjectCycle.objects.filter(cycle=cycle).select_related("project", "current_upload")
    }
    rows = []
    for project in Project.objects.filter(is_active=True).order_by("code"):
        pc = pcs.get(project.pk)
        baseline = pc.current_upload if pc else None
        approved = bool(baseline and baseline.status == UploadVersion.Status.APPROVED)
        rows.append({"project": project, "project_obj": project, "project_cycle": pc, "baseline": baseline, "approved": approved})
    return rows


def _project_and_cycle(request):
    cycle = _cycle_from_request(request)
    project_id = request.POST.get("project") or request.POST.get("project_id")
    project = Project.objects.filter(pk=project_id, is_active=True).first() if project_id else None
    if project is None:
        raise ScenarioError("请选择项目")
    if cycle is None:
        raise ScenarioError("当前没有预算周期")
    return project, cycle


def _new_scenario(request):
    project, cycle = _project_and_cycle(request)
    name = (request.POST.get("name") or request.POST.get("scenario_name") or "").strip()
    if not name:
        raise ScenarioError("请输入场景名称")
    return clone_scenario(project=project, cycle=cycle, name=name, created_by=request.user)


def _request_inputs(request):
    driver = (request.POST.get("driver") or "").strip().upper()
    if driver not in DRIVERS:
        raise ScenarioError("请选择场景驱动")
    cost_mode = (request.POST.get("cost_mode") or "fixed").strip().lower()
    if cost_mode not in COST_MODES:
        raise ScenarioError("请选择成本联动模式")

    uniform_value = (request.POST.get("uniform_value") or "").strip()

    def _normalise_month_value(raw, label):
        raw = str(raw).strip()
        if driver == "ROOM_REV":
            try:
                return yuan_to_cents(Decimal(raw) * Decimal("10000"))
            except (InvalidOperation, TypeError, ValueError):
                raise ScenarioError(f"{label}客房收入不是有效金额")
        return raw

    monthly = {}
    if uniform_value and driver in {"ADR", "OCC", "ROOMS", "ROOM_REV"}:
        value = _normalise_month_value(uniform_value, "全年统一")
        monthly = {month: value for month in MONTHS}
    else:
        for month in MONTHS:
            raw = request.POST.get(f"month_{month}")
            if raw is None:
                raw = request.POST.get(f"monthly_{month}")
            if raw is None:
                raw = request.POST.get(month)
            if raw is None or not str(raw).strip():
                continue
            monthly[month] = _normalise_month_value(raw, f"{month}月")

    data = {"driver": driver, "cost_mode": cost_mode, "monthly": monthly}
    if uniform_value:
        data["uniform_value"] = uniform_value
    annual_room_rev = (request.POST.get("annual_room_rev") or request.POST.get("annual_target") or "").strip()
    annual_room_rev_delta = (request.POST.get("annual_room_rev_delta") or request.POST.get("annual_delta") or "").strip()
    if annual_room_rev:
        data["annual_room_rev"] = annual_room_rev
    if annual_room_rev_delta:
        data["annual_room_rev_delta"] = annual_room_rev_delta

    fixed_costs = {}
    fixed_costs_raw = (request.POST.get("fixed_costs") or "").strip()
    if fixed_costs_raw:
        try:
            parsed = json.loads(fixed_costs_raw)
        except (TypeError, ValueError):
            raise ScenarioError("固定成本参数必须是有效 JSON")
        if not isinstance(parsed, dict):
            raise ScenarioError("固定成本参数必须是对象")
        fixed_costs.update(parsed)
    for key, value in request.POST.items():
        if key.startswith("fixed_cost_") and str(value).strip():
            fixed_costs[key[11:]] = str(value).strip()
    data["fixed_costs"] = fixed_costs
    return data


def _format_cell(cell, unit=None):
    if not cell:
        return "—"
    unit = str(unit or cell.get("unit") or "MONEY")
    if unit == "RATIO":
        denominator = int(cell.get("ratio_den") or 0)
        numerator = int(cell.get("ratio_num") or 0)
        ratio = Decimal(numerator) / Decimal(denominator) if denominator else Decimal(cell.get("value_int") or 0) / Decimal("10000")
        return f"{ratio * 100:.2f}%"
    value = int(cell.get("value_int") or 0)
    if unit == "COUNT":
        return f"{value:,}"
    if unit == "MONEY_PER_ROOM":
        return f"{cents_to_yuan(value):,.2f}"
    return f"{cents_to_yuan(value) / Decimal('10000'):,.2f}"


def _report_context(report_code, report):
    periods = list(report.get("periods") or [*MONTHS, "YEAR"])
    rows = []
    for row in report.get("rows") or []:
        unit = row.get("unit") or "MONEY"
        before = row.get("before") or {}
        after = row.get("after") or {}
        code = row.get("row_code") or row.get("code") or ""
        display_unit = "MONEY_PER_ROOM" if code in PER_ROOM_METRIC_CODES else unit
        unit_label = "元/房晚" if display_unit == "MONEY_PER_ROOM" else UNIT_LABELS.get(unit, unit)
        if unit == "COUNT":
            rooms = "R0009" if report_code.startswith("PL_ZZ") else "R0023"
            nights = {"R0010", "R0011"} if report_code.startswith("PL_ZZ") else {"R0024", "R0025", "R0026", "R0027", "R0028"}
            unit_label = "间" if code == rooms else "房晚" if code in nights else "数量"
        rows.append({
            "code": code,
            "label": row.get("label") or code,
            "unit": unit,
            "unit_label": unit_label,
            "before": {period: _format_cell(before.get(period), display_unit) for period in periods},
            "after": {period: _format_cell(after.get(period), display_unit) for period in periods},
            "changed": bool(row.get("changed")),
        })
    return {
        "code": report_code,
        "name": report.get("report_name") or REPORTS.get(report_code, report_code),
        "available": bool(report.get("available", True)),
        "periods": periods,
        "period_columns": [{"key": period, "label": "年度" if period == "YEAR" else f"{int(period)}月"} for period in periods],
        "rows": rows,
        "monthly": report.get("monthly") or [],
        "annual": report.get("annual") or {},
    }


def _form_data(scenario):
    inputs = scenario.inputs or {}
    driver = inputs.get("driver") or "OCC"
    cost_mode = inputs.get("cost_mode") or "fixed"
    monthly = inputs.get("monthly") or {}
    months = []
    for month in MONTHS:
        value = monthly.get(month, "")
        if driver == "ROOM_REV" and value not in (None, "") and not inputs.get("annual_room_rev") and not inputs.get("annual_room_rev_delta"):
            try:
                value = f"{cents_to_yuan(int(value)) / Decimal('10000'):.2f}"
            except (TypeError, ValueError, InvalidOperation):
                pass
        months.append({"key": month, "label": f"{int(month)}月", "value": value})
    return {
        "driver": driver,
        "cost_mode": cost_mode,
        "uniform_value": inputs.get("uniform_value") or "",
        "annual_room_rev": inputs.get("annual_room_rev") or "",
        "annual_room_rev_delta": inputs.get("annual_room_rev_delta") or "",
        "months": months,
    }


def _detail_context(scenario, *, error="", posted=None):
    results = scenario.results or {}
    reports = results.get("reports") or {}
    report_cards = [_report_context(code, reports.get(code) or {"report_code": code, "available": False}) for code in REPORTS]
    form_data = _form_data(scenario)
    if posted is not None:
        form_data["driver"] = posted.get("driver") or form_data["driver"]
        form_data["cost_mode"] = posted.get("cost_mode") or form_data["cost_mode"]
        form_data["uniform_value"] = posted.get("uniform_value", form_data["uniform_value"])
        form_data["annual_room_rev"] = posted.get("annual_room_rev", form_data["annual_room_rev"])
        form_data["annual_room_rev_delta"] = posted.get("annual_room_rev_delta", form_data["annual_room_rev_delta"])
        for month in form_data["months"]:
            raw = posted.get(f"month_{month['key']}")
            if raw is not None:
                month["value"] = raw
    source = scenario_source_identity(scenario)
    baseline = scenario.baseline
    project_cycle = ProjectCycle.objects.filter(project_id=baseline.project_id, cycle_id=baseline.cycle_id).first()
    planning_context = scenario_planning_context(scenario)
    return {
        "scenario": scenario,
        "cycle": baseline.cycle,
        "project": baseline.project,
        "baseline": baseline,
        "baseline_current": bool(project_cycle and project_cycle.current_upload_id == baseline.pk),
        "source_identity": source,
        "report_cards": report_cards,
        "reports": report_cards,
        "calculated": bool(reports),
        "results": results,
        **planning_context,
        "form_data": form_data,
        "driver_options": [{"code": code, "label": DRIVER_LABELS[code], "unit": DRIVER_UNITS[code]} for code in DRIVERS],
        "cost_mode_options": [{"code": code, "label": COST_MODE_LABELS[code]} for code in COST_MODES],
        "cost_mode": form_data["cost_mode"],
        "error": error or scenario.error,
        "costs_fixed_banner": results.get("costs_fixed_banner") or "固定成本假设：成本费用绝对额保持基线不变。",
    }


@_admin_required
def scenario_list(request):
    cycle = _cycle_from_request(request)
    error = ""
    if request.method == "POST":
        try:
            scenario = _new_scenario(request)
        except (ScenarioError, PermissionError, ValueError) as exc:
            error = str(exc)
        else:
            messages.success(request, f"已创建场景「{scenario.name}」。")
            return redirect("scenario_detail", scenario_id=scenario.pk)
    scenarios = _scenario_queryset().filter(baseline__cycle=cycle) if cycle else _scenario_queryset().none()
    scenarios = scenarios.exclude(pk__in=BudgetScenario.objects.filter(inputs__kind="summary_annual").values("pk"))
    context = {
        "cycle": cycle,
        "cycles": BudgetCycle.objects.order_by("-budget_year", "-revision_no"),
        "scenarios": scenarios,
        "project_baselines": _project_baselines(cycle),
        "projects": _project_baselines(cycle),
        "driver_options": [{"code": code, "label": DRIVER_LABELS[code], "unit": DRIVER_UNITS[code]} for code in DRIVERS],
        "error": error,
    }
    return render(request, "budgeting/scenario_list.html", context)


@_admin_required
def scenario_new(request):
    cycle = _cycle_from_request(request)
    error = ""
    if request.method == "POST":
        try:
            scenario = _new_scenario(request)
        except (ScenarioError, PermissionError, ValueError) as exc:
            error = str(exc)
            cycle = _cycle_from_request(request)
        else:
            messages.success(request, f"已创建场景「{scenario.name}」。")
            return redirect("scenario_detail", scenario_id=scenario.pk)
    return render(request, "budgeting/scenario_new.html", {
        "cycle": cycle,
        "cycles": BudgetCycle.objects.order_by("-budget_year", "-revision_no"),
        "project_baselines": _project_baselines(cycle),
        "projects": _project_baselines(cycle),
        "error": error,
        "name": request.POST.get("name", "") if request.method == "POST" else "",
    })


@_admin_required
def scenario_detail(request, scenario_id):
    scenario = get_object_or_404(_scenario_queryset(), pk=scenario_id)
    if scenario.inputs.get("kind") == "summary_annual":
        return redirect("summary_detail", scenario_id=scenario.pk)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "calculate":
            return scenario_calculate(request, scenario_id)
        if action == "issue":
            return scenario_issue(request, scenario_id)
    return render(request, "budgeting/scenario_detail.html", _detail_context(scenario))


@_admin_required
def scenario_calculate(request, scenario_id):
    scenario = get_object_or_404(_scenario_queryset(), pk=scenario_id)
    if request.method != "POST":
        return redirect("scenario_detail", scenario_id=scenario.pk)
    try:
        inputs = _request_inputs(request)
        calculate_scenario(scenario, inputs=inputs, actor=request.user)
    except (ScenarioError, PermissionError, InvalidOperation, TypeError, ValueError) as exc:
        scenario.refresh_from_db()
        return render(request, "budgeting/scenario_detail.html", _detail_context(scenario, error=str(exc), posted=request.POST))
    messages.success(request, "场景已完成测算，可核对四张报表后下发。")
    return redirect("scenario_detail", scenario_id=scenario.pk)


@_admin_required
def scenario_issue(request, scenario_id):
    scenario = get_object_or_404(_scenario_queryset(), pk=scenario_id)
    if request.method != "POST":
        return redirect("scenario_detail", scenario_id=scenario.pk)
    try:
        batch = issue_scenario(scenario, actor=request.user)
    except (ScenarioError, PermissionError, InvalidOperation, TypeError, ValueError) as exc:
        scenario.refresh_from_db()
        return render(request, "budgeting/scenario_detail.html", _detail_context(scenario, error=str(exc)))
    messages.success(request, f"场景已下发，生成调整批次 #{batch.pk}。")
    return redirect("scenario_detail", scenario_id=scenario.pk)


management_scenarios = scenario_list
management_scenario_new = scenario_new
management_scenario_detail = scenario_detail
management_scenario_calculate = scenario_calculate
management_scenario_issue = scenario_issue
scenario_create = scenario_new


__all__ = [
    "scenario_list",
    "scenario_new",
    "scenario_create",
    "scenario_detail",
    "scenario_calculate",
    "scenario_issue",
    "management_scenarios",
    "management_scenario_new",
    "management_scenario_detail",
    "management_scenario_calculate",
    "management_scenario_issue",
]
