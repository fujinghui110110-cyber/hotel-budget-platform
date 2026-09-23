from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from budgeting.models import ProcessingJob, UploadVersion
from budgeting.services.processing_runs import enqueue_processing_job
from budgeting.services.workflow import process_upload_now


@login_required
@require_POST
def retry_upload(request, upload_id):
    upload = get_object_or_404(UploadVersion, pk=upload_id)
    is_admin = request.user.is_superuser or request.user.role == "ADMIN"
    if not is_admin and not (request.user.role == "PROJECT" and request.user.project_id == upload.project_id):
        return HttpResponseForbidden("无权重新处理其他项目的预算。")
    latest_job = ProcessingJob.objects.filter(upload=upload).order_by("-created_at", "-pk").first()
    if upload.status != UploadVersion.Status.REJECTED or not latest_job or latest_job.status != ProcessingJob.Status.FAILED:
        messages.error(request, "仅处理任务失败的预算可重试；校验不通过请修正底稿后重新上传。")
        return redirect("project_upload_detail", upload.id)
    try:
        job, created = enqueue_processing_job(upload, actor=request.user, reason="用户重试失败的处理任务")
        if created and settings.BUDGET_PROCESS_UPLOAD_INLINE:
            process_upload_now(upload)
        messages.success(request, "已保留原件并重新安排处理，请查看本次处理结果。")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect("project_upload_detail", upload.id)
