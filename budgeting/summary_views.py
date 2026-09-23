from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from budgeting.models import BudgetCycle, BudgetScenario, Project, REPORTS
from budgeting.scenario_views import _admin_required, _cycle_from_request
from budgeting.services.scenarios import ScenarioError
from budgeting.services.budget_versions import selected_project_upload
from budgeting.services.project_scope import cycle_projects


def _display(value, unit):
    if value is None:
        return "—"
    divisor = 100 if unit in {"MONEY", "RATIO"} else 1
    number = Decimal(value) / divisor
    return f"{number:,.2f}" if divisor == 100 else f"{number:,.0f}"


def _baselines(cycle):
    return {project.pk: selected_project_upload(project, cycle) for project in cycle_projects(cycle)}


def _editor_context(scenario, *, error="", posted=None):
    from budgeting.services.summary_scenarios import build_summary_editor

    report = scenario.inputs.get("report_code") or scenario.results.get("report_code", "PL_TOTAL_WINE")
    rows = build_summary_editor(scenario.baseline, report)
    calculated = {row["code"]: row for row in scenario.results.get("rows", [])}
    for row in rows:
        result = calculated.get(row["code"], {})
        after = result.get("after", row["before"])
        row["after_display"] = _display(after, row["unit"])
        row["before_display"] = _display(row["before"], row["unit"])
        row["changed"] = result.get("changed", False)
        row["delta_display"] = _display(result.get("delta"), row["unit"])
        row["input_value"] = "" if after is None else _display(after, row["unit"])
        if posted is not None and row["editable"]:
            row["input_value"] = posted.get(f"value_{row['code']}", row["input_value"])
        row["unit_label"] = {"MONEY": "元", "COUNT": "数量", "RATIO": "%"}.get(row["unit"], "")
        for history in row.get("history", []):
            history["display"] = _display(history.get("value_int"), row["unit"])
    year = scenario.baseline.cycle.budget_year
    return {
        "scenario": scenario, "cycle": scenario.baseline.cycle,
        "report_name": REPORTS.get(report, report), "rows": rows,
        "history_years": [year - 3, year - 2, year - 1],
        "error": error, "issued": scenario.status == "ISSUED",
        "reason": posted.get("reason", "") if posted is not None else scenario.inputs.get("reason", ""),
        "changed_count": sum(bool(row["changed"]) for row in rows),
    }


@_admin_required
def summary_list(request):
    cycle = _cycle_from_request(request)
    error = ""
    baselines = _baselines(cycle) if cycle else {}
    if request.method == "POST":
        from budgeting.services.summary_scenarios import create_summary_scenario

        try:
            project_id = int(request.POST.get("project", ""))
            upload = baselines.get(project_id)
            report = request.POST.get("report", "PL_TOTAL_WINE")
            if upload is None or report not in REPORTS:
                raise ValueError("请选择已有可用预算的项目和汇总表。")
            scenario = create_summary_scenario(
                upload=upload, name=f"{upload.project.name} · {cycle.budget_year} R{cycle.revision_no} 汇总调整",
                actor=request.user, report_code=report,
            )
            return redirect("summary_detail", scenario_id=scenario.pk)
        except (ScenarioError, PermissionError, ValueError) as exc:
            error = str(exc)
    projects = [{"project": project, "upload": baselines.get(project.pk)} for project in cycle_projects(cycle)]
    scenarios = BudgetScenario.objects.filter(baseline__cycle=cycle, inputs__kind="summary_annual").select_related("baseline__project").order_by("-updated_at")
    return render(request, "budgeting/summary_list.html", {
        "cycle": cycle, "cycles": BudgetCycle.objects.order_by("-budget_year", "-revision_no"),
        "projects": projects, "reports": REPORTS.items(), "scenarios": scenarios, "error": error,
    })


@_admin_required
def summary_detail(request, scenario_id):
    from budgeting.services.summary_scenarios import calculate_summary_scenario, issue_summary_scenario

    scenario = get_object_or_404(BudgetScenario.objects.select_related("baseline__project", "baseline__cycle"), pk=scenario_id)
    if scenario.inputs.get("kind") != "summary_annual":
        raise Http404()
    error = ""
    if request.method == "POST":
        try:
            if request.POST.get("action") == "issue":
                current = _editor_context(scenario)
                for row in current["rows"]:
                    if row["editable"]:
                        posted = request.POST.get(f"value_{row['code']}", "").strip()
                        saved = str(row["input_value"]).strip()
                        if (not posted) != (not saved) or (posted and Decimal(posted.replace(",", "")) != Decimal(saved.replace(",", ""))):
                            raise ValueError("存在未保存的改动，请先保存并计算，再下发项目。")
                if request.POST.get("reason", "").strip() != current["reason"].strip():
                    raise ValueError("调整说明尚未保存，请先保存并计算。")
                issue_summary_scenario(scenario, actor=request.user)
                messages.success(request, "已下发。项目端可查看重点调整、联动结果及调整原因。")
            else:
                overrides = {key.removeprefix("value_"): value for key, value in request.POST.items() if key.startswith("value_")}
                calculate_summary_scenario(scenario, overrides=overrides, actor=request.user, reason=request.POST.get("reason", ""))
                messages.success(request, "已保存并按汇总表公式计算，请核对变化后下发。")
            return redirect("summary_detail", scenario_id=scenario.pk)
        except (ScenarioError, PermissionError, InvalidOperation, ValueError) as exc:
            error = str(exc)
            scenario.refresh_from_db()
    return render(request, "budgeting/summary_detail.html", _editor_context(scenario, error=error, posted=request.POST if error else None))
