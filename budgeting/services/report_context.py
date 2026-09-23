"""Explicit immutable dimensions shared by reports, drilldown and exports."""
from dataclasses import dataclass
from urllib.parse import urlencode

from budgeting.models import REPORTS


@dataclass(frozen=True)
class ReportContext:
    budget_year: int
    cycle_id: int
    report_code: str
    source_mode: str
    project_ids: tuple[int, ...]
    period: str = "YEAR"
    snapshot_id: str | None = None
    data_year: int | None = None
    data_kind: str = "BUDGET"

    def __post_init__(self):
        if self.data_kind not in {"BUDGET", "ACTUAL", "FORECAST"}:
            raise ValueError("无效的数据类型。")
        if self.data_kind == "BUDGET" and self.data_year not in {None, self.budget_year}:
            raise ValueError("预算数据年度必须等于预算计划年度。")
        if self.data_kind != "BUDGET" and (self.data_year is None or self.data_year >= self.budget_year):
            raise ValueError("历史对照必须明确指定早于预算年度的年度。")
        if self.report_code not in REPORTS:
            raise ValueError("请选择四套受控报表中的一个口径。")
        if self.source_mode not in {"WORKING", "APPROVED", "FROZEN"}:
            raise ValueError("必须明确选择工作稿、正式稿或冻结快照。")
        if self.period not in {"YEAR", *[f"{m:02}" for m in range(1, 13)]}:
            raise ValueError("报表期间必须是 YEAR 或 01 至 12。")
        if not self.project_ids or len(set(self.project_ids)) != len(self.project_ids):
            raise ValueError("必须明确提供不重复的项目集合。")
        object.__setattr__(self, "project_ids", tuple(sorted(int(p) for p in self.project_ids)))
        if (self.source_mode == "FROZEN") != bool(self.snapshot_id):
            raise ValueError("冻结模式必须指定快照，其他模式不得指定快照。")

    def query_string(self):
        return urlencode({"year": self.budget_year, "cycle": self.cycle_id,
                          "report": self.report_code, "source_mode": self.source_mode,
                          "projects": ",".join(map(str, self.project_ids)),
                          "period": self.period, "data_year": self.data_year or self.budget_year, "data_kind": self.data_kind, "snapshot": self.snapshot_id or ""})
