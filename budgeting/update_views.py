"""Local administrator controls for installing a published system release."""
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from budgeting.access_views import local_control_allowed
from budgeting.models import AuditEvent
from scripts import system_update


def _allowed(request):
    return request.user.is_admin_role and local_control_allowed(request)


def _state(state=None):
    state = system_update.status() if state is None else state
    return {key: state.get(key, default) for key, default in (
        ('current_version', ''), ('available_version', ''), ('release_notes', ''),
        ('update_available', False), ('configured', False), ('status', 'idle'),
        ('busy', False), ('error', ''), ('message', ''),
    )}


@login_required
@never_cache
@require_GET
def update_management(request):
    if not request.user.is_admin_role:
        return HttpResponseForbidden('仅管理员可以管理系统更新。')
    return render(request, 'budgeting/update_management.html', {
        'local_control': local_control_allowed(request),
    })


@login_required
@never_cache
@require_GET
def update_status(request):
    if not _allowed(request):
        return HttpResponseForbidden('请在服务器电脑上使用本机地址，以管理员身份操作。')
    return JsonResponse(_state())


@login_required
@never_cache
@require_POST
def update_action(request):
    if not _allowed(request):
        return HttpResponseForbidden('请在服务器电脑上使用本机地址，以管理员身份操作。')
    action = request.POST.get('action')
    if action not in ('configure', 'check', 'install'):
        return JsonResponse({'error': '不支持的操作。'}, status=400)
    try:
        if action == 'configure':
            token = request.POST.get('token', '').strip()
            if not token or len(token) > 512:
                return JsonResponse({'error': '请输入有效的 GitHub 只读下载凭据。'}, status=400)
            system_update.configure(token)
            result = None
        elif action == 'check':
            result = system_update.check()
        else:
            result = system_update.start_update()
    except system_update.UpdateError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    except (OSError, RuntimeError, ValueError):
        # Do not echo exceptions: upstream HTTP errors may include credentials.
        return JsonResponse({'error': '操作未完成，请检查 GitHub 凭据、仓库读取权限和网络后重试。'}, status=503)
    AuditEvent.objects.create(actor=request.user, action='system_update_' + action)
    return JsonResponse(_state(result), status=202 if action == 'install' else 200)
