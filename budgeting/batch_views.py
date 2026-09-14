from __future__ import annotations

from django.contrib import messages
from django.shortcuts import render

from budgeting.cockpit_views import admin_required
from budgeting.models import BudgetCycle
from budgeting.services.batch_import import (
    MAX_BATCH_BYTES,
    MAX_BATCH_FILES,
    enqueue_batch_upload,
    preflight_batch,
    record_batch_preflight,
)
from budgeting.services.budget_versions import version_label
from budgeting.services.workflow import active_cycle


def _cycles():
    return BudgetCycle.objects.order_by("-budget_year", "-revision_no", "-created_at")


def _selected_cycle(request):
    cycle_id = request.POST.get("cycle") or request.GET.get("cycle")
    if cycle_id:
        if not str(cycle_id).isdigit():
            return None
        return BudgetCycle.objects.filter(pk=cycle_id).first()
    return active_cycle()


@admin_required
def batch_upload(request):
    cycle = _selected_cycle(request)
    cycles = list(_cycles())
    preflight = None
    outcome = None
    if request.method == "POST":
        files = request.FILES.getlist("files") or request.FILES.getlist("file")
        preflight = preflight_batch(files, cycle)
        if preflight.valid:
            outcome = enqueue_batch_upload(preflight, actor=request.user, process_inline=False)
            accepted = sum(1 for item in outcome.items if item.accepted)
            if accepted:
                messages.success(request, f"已接收 {accepted} 个预算文件，处理任务已进入队列。")
            else:
                messages.error(request, "本批文件未能加入队列，请查看逐文件错误。")
        else:
            record_batch_preflight(preflight, actor=request.user)
            messages.error(request, "批量上传预检未通过，未写入任何文件。")
    return render(
        request,
        "budgeting/batch_upload.html",
        {
            "cycle": cycle,
            "cycles": cycles,
            "preflight": preflight,
            "outcome": outcome,
            "max_files": MAX_BATCH_FILES,
            "max_total_mb": MAX_BATCH_BYTES // (1024 * 1024),
            "version_label": version_label(cycle) if cycle else "",
        },
    )
