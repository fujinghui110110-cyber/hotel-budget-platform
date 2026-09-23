import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from django.conf import settings
from django.db import transaction

from budgeting.excel.business_checks import TOTAL_RULES, ZZ_RULES
from budgeting.models import (
    AdjustmentBatch,
    AdjustmentLine,
    BudgetCycle,
    BudgetScenario,
    NormalizedValue,
    REPORTS,
    UploadVersion,
)
from budgeting.services.pnl_graph import (
    CROSS_REF,
    _eval,
    annual_formula_overrides_rollup,
    build_report_graph,
    parse_formula,
)
from budgeting.services.budget_versions import (
    REPORTABLE_UPLOAD_STATUSES,
    project_open_cycle,
    selected_project_upload,
)
from budgeting.services.scenarios import ScenarioError
from budgeting.services.template_paths import resolve_template_path


RULE_VERSION = "SUMMARY_ANNUAL_V1"
ELIGIBLE_UPLOAD_STATUSES = REPORTABLE_UPLOAD_STATUSES


def _require_admin(actor):
    if actor is not None and not (
        getattr(actor, "is_superuser", False) or getattr(actor, "role", "") == "ADMIN"
    ):
        raise PermissionError("只有管理端可以操作汇总表测算")


def _source_identity(upload):
    template = upload.template
    return {
        "baseline_upload_id": str(upload.pk),
        "baseline_sha256": str(upload.sha256),
        "project_id": upload.project_id,
        "cycle_id": upload.cycle_id,
        "template_id": upload.template_id,
        "template_version": str(getattr(template, "version", "")),
        "rule_version": RULE_VERSION,
    }


def _manifest(upload):
    template = upload.template
    if not template or not template.manifest_path:
        raise ScenarioError("该上传版本没有模板清单，不能验证汇总表公式")
    path = resolve_template_path(template.manifest_path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScenarioError("模板清单不可读取，不能验证汇总表公式") from exc


def _rules(report_code):
    return ZZ_RULES if report_code.startswith("PL_ZZ") else TOTAL_RULES


def _rule_formula(dependencies):
    expression = []
    for number, sign in dependencies:
        code = f"R{number:04d}"
        if not expression:
            expression.append(code if sign > 0 else f"-{code}")
        else:
            expression.append((" + " if sign > 0 else " - ") + code)
    return "".join(expression)


def _template_formula(metadata):
    annual = metadata.get("annual") or ""
    if annual and annual_formula_overrides_rollup(metadata) and not CROSS_REF.search(annual):
        return annual
    formula = metadata.get("monthly") or annual
    if formula and not CROSS_REF.search(formula):
        return formula
    if metadata.get("aggregation") in {"RATIO", "DERIVED"} and annual and not CROSS_REF.search(annual):
        return annual
    return ""


def _annual_values(upload, report_code):
    rows = {}
    history = {}
    queryset = NormalizedValue.objects.filter(upload=upload, report_code=report_code, month__isnull=True)
    for value in queryset.order_by("row_code", "data_year", "id"):
        if value.data_kind == "BUDGET" and value.data_year == upload.cycle.budget_year:
            rows[value.row_code] = value
        elif value.data_kind in {"ACTUAL", "FORECAST"} and value.data_year:
            history[(value.row_code, value.data_year, value.data_kind)] = value
    return rows, history


def _engine_value(value):
    if value.unit == NormalizedValue.Unit.RATIO:
        if value.ratio_den:
            return Decimal(value.ratio_num or 0) / Decimal(value.ratio_den)
        return Decimal(value.value_int or 0) / Decimal(10_000)
    return Decimal(value.value_int)


def _model_stored_value(value):
    if value is None:
        return None
    return _stored_value(value.unit, _engine_value(value))


def _stored_value(unit, value):
    number = Decimal(str(value))
    if unit == NormalizedValue.Unit.RATIO:
        number *= Decimal(10_000)
    return int(number.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _input_value(unit, stored):
    if stored is None:
        return None
    if unit == NormalizedValue.Unit.MONEY:
        return format(Decimal(stored) / Decimal(100), ".2f")
    if unit == NormalizedValue.Unit.RATIO:
        return format(Decimal(stored) / Decimal(100), ".2f")
    return str(stored)


def _display_value(unit, stored):
    if stored is None:
        return None
    if unit == NormalizedValue.Unit.MONEY:
        return f"{Decimal(stored) / Decimal(100):,.2f}"
    if unit == NormalizedValue.Unit.RATIO:
        return f"{Decimal(stored) / Decimal(100):,.2f}%"
    return f"{stored:,}"


def _parse_input(unit, value):
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        raise ScenarioError(f"数值无效：{value}")
    if not number.is_finite():
        raise ScenarioError(f"数值无效：{value}")
    if unit == NormalizedValue.Unit.MONEY:
        return int((number * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if unit == NormalizedValue.Unit.RATIO:
        if not Decimal("0") <= number <= Decimal("100"):
            raise ScenarioError("百分比必须在0至100之间")
        return int((number * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    rounded = number.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if rounded != number:
        raise ScenarioError("数量必须为整数")
    return int(rounded)


def _report_definition(upload, report_code):
    if report_code not in REPORTS:
        raise ScenarioError("未知汇总报表")
    manifest = _manifest(upload)
    try:
        graph, number_to_code = build_report_graph(manifest, report_code)
    except ValueError as exc:
        raise ScenarioError(str(exc)) from exc
    formula_targets = {code for code, metadata in graph.items() if metadata["kind"] == "derived" and _template_formula(metadata)}
    rules = {} if manifest.get("legacy_rehearsal") else {
        number: deps for number, deps in _rules(report_code).items() if f"R{number:04d}" not in formula_targets
    }
    rule_targets = {f"R{number:04d}": deps for number, deps in rules.items()}
    derived = formula_targets
    return graph, number_to_code, rule_targets, derived


def _check_upload(upload, *, require_latest=True, write=False):
    upload = UploadVersion.objects.select_related("project", "cycle", "template").get(pk=upload.pk)
    if upload.status not in ELIGIBLE_UPLOAD_STATUSES:
        raise ScenarioError("只能选择已校验、已提交或已批准的预算版本")
    if require_latest:
        latest = selected_project_upload(upload.project, upload.cycle)
        if not latest or latest.pk != upload.pk:
            raise ScenarioError("该基准已不是本项目在此预算版本中的最新合格上传，请重新选择")
    if write and upload.cycle.status not in {BudgetCycle.Status.OPEN, BudgetCycle.Status.ADJUSTING}:
        raise ScenarioError("当前预算版本不允许测算或下发")
    if write:
        open_cycle = project_open_cycle(upload.project)
        if open_cycle is None or open_cycle.pk != upload.cycle_id:
            raise ScenarioError("该项目当前已不在此预算版本开放填报，不能测算或下发")
    return upload


def build_summary_editor(upload, report_code):
    upload = _check_upload(upload, require_latest=False)
    graph, _number_to_code, rule_targets, derived = _report_definition(upload, report_code)
    values, history = _annual_values(upload, report_code)
    years = (
        (upload.cycle.budget_year - 3, "ACTUAL"),
        (upload.cycle.budget_year - 2, "ACTUAL"),
        (upload.cycle.budget_year - 1, "FORECAST"),
    )
    result = []
    for code, metadata in sorted(graph.items(), key=lambda item: item[1]["row_num"]):
        value = values.get(code)
        unit = str(value.unit if value else metadata["unit"])
        before = _model_stored_value(value)
        formula = ""
        formula_kind = ""
        if code in rule_targets:
            formula = _rule_formula(rule_targets[code])
            formula_kind = "RECONCILIATION"
        elif code in derived:
            formula = metadata.get("annual") or ""
            formula_kind = "DERIVED"
        row_history = []
        for year, kind in years:
            prior = history.get((code, year, kind))
            prior_stored = _model_stored_value(prior)
            row_history.append(
                {
                    "year": year,
                    "kind": kind,
                    "value_int": prior_stored,
                    "value_display": _display_value(unit, prior_stored),
                }
            )
        result.append(
            {
                "code": code,
                "label": (value.row_label if value and value.row_label else metadata["label"]),
                "unit": unit,
                "before": before,
                "before_display": _display_value(unit, before),
                "input_value": _input_value(unit, before),
                "editable": value is not None and not formula,
                "formula": formula,
                "formula_kind": formula_kind,
                "missing": value is None,
                "history": row_history,
            }
        )
    return result


def create_summary_scenario(upload, name, actor, report_code, reason=""):
    _require_admin(actor)
    upload = _check_upload(upload, write=True)
    _report_definition(upload, report_code)
    name = str(name or "").strip()
    if not name:
        raise ScenarioError("请输入测算名称")
    return BudgetScenario.objects.create(
        baseline=upload,
        name=name,
        created_by=actor,
        rule_version=RULE_VERSION,
        inputs={
            "kind": "summary_annual",
            "informational_only": True,
            "requirements_source": "annual_targets",
            "report_code": report_code,
            "reason": str(reason or "").strip(),
            "source_identity": _source_identity(upload),
        },
    )


def _verify_scenario_source(scenario, upload):
    expected = (scenario.inputs or {}).get("source_identity") or {}
    if expected != _source_identity(upload):
        raise ScenarioError("测算基准身份不一致，请基于最新上传重新创建测算")


def _calculate(upload, report_code, overrides):
    graph, number_to_code, rule_targets, derived = _report_definition(upload, report_code)
    source, _history = _annual_values(upload, report_code)
    if not source:
        raise ScenarioError("该报表没有预算年度数据")
    before_engine = {code: _engine_value(value) for code, value in source.items()}
    before_stored = {code: _model_stored_value(value) for code, value in source.items()}
    after_engine = dict(before_engine)
    entered = set()
    overrides = overrides or {}
    if not isinstance(overrides, dict):
        raise ScenarioError("年度调整必须是行代码与目标值的对应表")
    for code, raw in overrides.items():
        if raw is None or str(raw).strip() == "":
            continue
        if code not in graph:
            raise ScenarioError(f"未知行代码：{code}")
        if code not in source:
            raise ScenarioError(f"{code} 基准缺失，不能用零替代后直接调整")
        if code in rule_targets or code in derived:
            raise ScenarioError(f"{source[code].row_label or code} 是公式联动项，不能直接编辑")
        stored = _parse_input(source[code].unit, raw)
        after_engine[code] = (
            Decimal(stored) / Decimal(10_000)
            if source[code].unit == NormalizedValue.Unit.RATIO
            else Decimal(stored)
        )
        if stored != before_stored[code]:
            entered.add(code)

    visiting = set()
    done = set()

    def compute(code, *, baseline=False):
        values = before_engine if baseline else after_engine
        if code in done and not baseline:
            return values[code]
        if code in visiting:
            raise ScenarioError(f"年度公式依赖存在循环：{code}")
        if code not in source:
            raise ScenarioError(f"年度公式来源缺失：{report_code}:{code}；不能按零计算")
        visiting.add(code)
        try:
            if code in rule_targets:
                total = Decimal(0)
                for number, sign in rule_targets[code]:
                    dependency = f"R{number:04d}"
                    total += compute(dependency, baseline=baseline) * sign
                values[code] = total
            elif code in derived:
                metadata = graph[code]
                for dependency in metadata.get("deps") or ():
                    if dependency in rule_targets or dependency in derived:
                        compute(dependency, baseline=baseline)
                    elif dependency not in values:
                        raise ScenarioError(
                            f"年度公式来源缺失：{report_code}:{dependency}；不能按零计算"
                        )
                formula = _template_formula(metadata)
                if not formula:
                    raise ScenarioError(f"年度公式来源缺失：{report_code}:{code}；不能按零计算")
                ast = parse_formula(formula)

                def evaluate(value_map):
                    return Decimal(
                        str(
                            _eval(
                                ast,
                                lambda _col, row: value_map[number_to_code[row]]
                                if row in number_to_code and number_to_code[row] in value_map
                                else (_raise_missing(report_code, row)),
                                lambda _c1, r1, _c2, r2: [
                                    value_map[number_to_code[row]]
                                    if row in number_to_code and number_to_code[row] in value_map
                                    else (_raise_missing(report_code, row))
                                    for row in range(r1, r2 + 1)
                                ],
                            )
                        )
                    )

                values[code] = evaluate(values)
            return values[code]
        except (ValueError, ZeroDivisionError) as exc:
            raise ScenarioError(f"年度公式无法计算：{report_code}:{code}：{exc}") from exc
        finally:
            visiting.discard(code)
            if not baseline:
                done.add(code)

    for code in rule_targets.keys() & source.keys():
        expected = compute(code, baseline=True)
        if _stored_value(source[code].unit, expected) != before_stored[code]:
            raise ScenarioError(f"基准年度勾稽不一致：{report_code}:{code}，请先修订并重新上传")
    for code in derived & source.keys():
        expected = _stored_value(source[code].unit, compute(code, baseline=True))
        tolerance = 1 if source[code].unit == NormalizedValue.Unit.RATIO else 0
        if abs(expected - before_stored[code]) > tolerance:
            raise ScenarioError(f"基准年度公式不一致：{report_code}:{code}，请先修订并重新上传")
    visiting.clear()
    for code in rule_targets.keys() & source.keys():
        compute(code)
    for code in derived & source.keys():
        compute(code)

    rows = []
    changed_rows = []
    for code, metadata in sorted(graph.items(), key=lambda item: item[1]["row_num"]):
        if code not in source:
            continue
        unit = str(source[code].unit)
        before = before_stored[code]
        after = _stored_value(unit, after_engine[code])
        formula = ""
        formula_kind = ""
        if code in rule_targets:
            formula = _rule_formula(rule_targets[code])
            formula_kind = "RECONCILIATION"
        elif code in derived:
            formula = _template_formula(metadata)
            formula_kind = "DERIVED"
        row = {
            "code": code,
            "label": source[code].row_label or metadata["label"],
            "unit": unit,
            "before": before,
            "after": after,
            "delta": after - before,
            "before_display": _display_value(unit, before),
            "after_display": _display_value(unit, after),
            "changed": after != before,
            "entered": code in entered,
            "editable": not formula,
            "formula": formula,
            "formula_kind": formula_kind,
        }
        rows.append(row)
        if row["changed"]:
            changed_rows.append(row)
    return rows, changed_rows


def _raise_missing(report_code, row):
    raise ScenarioError(f"年度公式来源缺失：{report_code}:R{row:04d}；不能按零计算")


def _raise_range(report_code, code):
    raise ScenarioError(f"年度公式 {report_code}:{code} 含未经验证的区间引用")


@transaction.atomic
def calculate_summary_scenario(scenario, overrides, actor, reason=""):
    _require_admin(actor)
    scenario = (
        BudgetScenario.objects.select_for_update()
        .select_related("baseline", "baseline__project", "baseline__cycle", "baseline__template")
        .get(pk=getattr(scenario, "pk", scenario))
    )
    if scenario.status == "ISSUED":
        raise ScenarioError("已下发的测算不能覆盖，请新建测算")
    upload = _check_upload(scenario.baseline, write=True)
    _verify_scenario_source(scenario, upload)
    report_code = str((scenario.inputs or {}).get("report_code") or "")
    rows, changed_rows = _calculate(upload, report_code, overrides)
    saved_inputs = dict(scenario.inputs or {})
    saved_inputs["overrides"] = {str(key): str(value) for key, value in (overrides or {}).items()}
    saved_inputs["reason"] = str(reason or saved_inputs.get("reason") or "").strip()
    scenario.inputs = saved_inputs
    scenario.results = {
        "kind": "summary_annual",
        "rule_version": RULE_VERSION,
        "source_identity": _source_identity(upload),
        "report_code": report_code,
        "annual_only": True,
        "requires_monthly_refill": True,
        "rows": rows,
        "changed_rows": changed_rows,
    }
    scenario.rule_version = RULE_VERSION
    scenario.status = "READY"
    scenario.error = ""
    scenario.save(update_fields=["inputs", "results", "rule_version", "status", "error", "updated_at"])
    return scenario


@transaction.atomic
def issue_summary_scenario(scenario, actor):
    _require_admin(actor)
    scenario = (
        BudgetScenario.objects.select_for_update()
        .select_related("baseline", "baseline__project", "baseline__cycle", "baseline__template", "batch")
        .get(pk=getattr(scenario, "pk", scenario))
    )
    if scenario.status == "ISSUED" and scenario.batch_id:
        return scenario.batch
    if scenario.status != "READY":
        raise ScenarioError("只有已完成测算的汇总方案可以下发")
    upload = _check_upload(scenario.baseline, write=True)
    _verify_scenario_source(scenario, upload)
    results = scenario.results or {}
    if results.get("source_identity") != _source_identity(upload):
        raise ScenarioError("测算结果的基准版本已过时，请重新测算")
    changes = list(results.get("changed_rows") or [])
    if not changes:
        raise ScenarioError("没有发生变化的年度目标可下发")
    report_code = str(results.get("report_code") or "")
    conflicts = AdjustmentLine.objects.filter(
        cycle=upload.cycle,
        project=upload.project,
        report_code=report_code,
        row_code__in=[row["code"] for row in changes],
        period="YEAR",
        status=AdjustmentLine.Status.OPEN,
    )
    if conflicts.exists():
        raise ScenarioError("存在尚未完成的同年度调整任务，不能覆盖旧下发任务")
    reason = str((scenario.inputs or {}).get("reason") or "年度汇总表大数调整").strip()
    entered_changes = [row for row in changes if row["entered"]]
    first = next(
        (row for row in entered_changes if row["unit"] == "MONEY"),
        entered_changes[0] if entered_changes else changes[0],
    )
    cascade_changes = [
        {
            "row_code": row["code"],
            "label": row["label"],
            "unit": row["unit"],
            "before": row["before"],
            "after": row["after"],
            "delta": row["delta"],
            "entered": row["entered"],
            "formula": row["formula"],
            "formula_kind": row["formula_kind"],
            "reason": reason,
        }
        for row in changes
    ]
    batch = AdjustmentBatch.objects.create(
        cycle=upload.cycle,
        project=upload.project,
        driver="SUMMARY_ANNUAL",
        driver_label="年度汇总大数调整",
        report_code=report_code,
        row_code=first["code"],
        period="YEAR",
        baseline_total_cents=first["before"],
        delta_cents=first["delta"],
        reason=reason,
        cascade={
            "kind": "summary_annual",
            "informational_only": True,
            "requirements_source": "annual_targets",
            "scenario_id": str(scenario.pk),
            "rule_version": RULE_VERSION,
            "source_identity": _source_identity(upload),
            "report_code": report_code,
            "annual_only": True,
            "requires_monthly_refill": True,
            "changes": cascade_changes,
        },
    )
    AdjustmentLine.objects.bulk_create(
        [
            AdjustmentLine(
                batch=batch,
                cycle=upload.cycle,
                project=upload.project,
                report_code=report_code,
                row_code=row["code"],
                period="YEAR",
                baseline_cents=row["before"],
                weight=1,
                allocated_delta_cents=row["delta"],
                target_cents=row["after"],
            )
            for row in changes
        ]
    )
    from budgeting.services.workflow import audit, issue_adjustment

    audit(
        actor,
        "SUMMARY_SCENARIO_DRAFTED",
        "BudgetScenario",
        scenario.pk,
        {"batch_id": batch.pk, "source_identity": _source_identity(upload)},
        project=upload.project,
        cycle=upload.cycle,
        upload=upload,
    )
    issue_adjustment(batch, actor)
    batch.refresh_from_db()
    scenario.batch = batch
    scenario.status = "ISSUED"
    scenario.error = ""
    scenario.save(update_fields=["batch", "status", "error", "updated_at"])
    return batch


__all__ = [
    "RULE_VERSION",
    "ELIGIBLE_UPLOAD_STATUSES",
    "build_summary_editor",
    "create_summary_scenario",
    "calculate_summary_scenario",
    "issue_summary_scenario",
]
