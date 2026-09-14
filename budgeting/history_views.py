from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP

from django import forms
from django.conf import settings
from django.contrib import messages
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render

from budgeting.cockpit_views import admin_required
from budgeting.models import HistoricalImport, REPORTS
from budgeting.services.historical_data import stage_history, confirm_history


def _display(value):
    read = value.get if isinstance(value, dict) else lambda key: getattr(value, key)
    number = Decimal(read("value_int") or 0)
    if read("unit") == "RATIO":
        denominator = read("ratio_den")
        return (f"{Decimal(read('ratio_num') or 0) / Decimal(denominator):.1%}" if denominator else "—")
    if read("unit") == "MONEY":
        return f"{(number / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP):,} 元"
    return f"{number:,.0f}"


class HistoryUploadForm(forms.Form):
    file = forms.FileField(label="损益汇总文件", help_text="文件名包含项目全称或项目代码；支持 .xlsx、.xlsm。")
    data_year = forms.IntegerField(label="数据年度", min_value=2000, max_value=2199)
    data_kind = forms.ChoiceField(label="数据口径", choices=[("ACTUAL", "实际"), ("FORECAST", "预测")])
    report_code = forms.ChoiceField(label="报表口径", choices=list(REPORTS.items()))
    money_unit = forms.ChoiceField(label="原表金额单位", choices=[("yuan", "元"), ("wan", "万元")])


@admin_required
def history_management(request):
    from budgeting.services.batch_import import match_project_filename
    form = HistoryUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            data = form.cleaned_data
            project = match_project_filename(data["file"].name)
            batch = stage_history(project, data["file"], request.user, data["data_year"],
                                  data["data_kind"], data["report_code"], data["money_unit"])
            return redirect("history_review", batch_id=batch.pk)
        except (ValueError, OSError) as exc:
            form.add_error(None, str(exc))
    return render(request, "budgeting/history_management.html", {
        "form": form, "batches": HistoricalImport.objects.select_related("project").all()[:100],
    })


@admin_required
def history_review(request, batch_id):
    batch = get_object_or_404(HistoricalImport.objects.select_related("project"), pk=batch_id)
    rows = []
    for index, original in enumerate(batch.proposal.get("rows", [])):
        row = dict(original)
        row["field"] = f"mapping_{index}"
        row["selected"] = request.POST.get(row["field"], "") if request.method == "POST" else (row.get("suggested_code") if row.get("include") else "")
        row["value_count"] = len(row.get("values", []))
        row["preview"] = [{**value, "display": _display(value)} for value in row.get("values", [])[:13]]
        rows.append(row)
    if request.method == "POST":
        try:
            confirm_history(batch, {row["source_key"]: row["selected"] for row in rows}, request.user)
            messages.success(request, "历史损益数据已确认生效，已同步到未冻结预算版本的总览、报表、趋势和测算。")
            return redirect("history_review", batch_id=batch.pk)
        except ValueError as exc:
            messages.error(request, str(exc))
    values = list(batch.values.all().order_by("row_code", "period")) if batch.confirmed_at else []
    for value in values:
        value.display = _display(value)
    return render(request, "budgeting/history_review.html", {
        "batch": batch, "rows": rows, "canonical": batch.proposal.get("canonical", []),
        "values": values,
        "report_name": REPORTS.get(batch.report_code, batch.report_code),
    })


@admin_required
def history_original(request, batch_id):
    batch = get_object_or_404(HistoricalImport, pk=batch_id)
    path = Path(settings.BUDGET_STORAGE_ROOT) / batch.original_path
    return FileResponse(path.open("rb"), as_attachment=True, filename=batch.original_name)
