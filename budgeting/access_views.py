from ipaddress import ip_address

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from budgeting.models import AuditEvent
from scripts import public_access


def local_control_allowed(request):
    if settings.PUBLIC_ACCESS:
        return False
    if any(request.META.get(key) for key in (
        'HTTP_CF_CONNECTING_IP', 'HTTP_X_FORWARDED_FOR', 'HTTP_FORWARDED',
    )):
        return False
    try:
        return ip_address(request.META.get('REMOTE_ADDR', '')).is_loopback and request.get_host().split(':')[0] in ('127.0.0.1', 'localhost')
    except ValueError:
        return False


def _state():
    state = public_access.status()
    return {key: state.get(key, default) for key, default in (
        ('status', 'stopped'), ('running', False), ('busy', False),
        ('url', ''), ('error', ''),
    )}


@login_required
@never_cache
@require_GET
def access_management(request):
    if not request.user.is_admin_role:
        return HttpResponseForbidden('仅管理员可以管理公网访问。')
    return render(request, 'budgeting/access_management.html', {
        'local_control': local_control_allowed(request),
    })


@login_required
@never_cache
@require_GET
def access_status(request):
    if not request.user.is_admin_role or not local_control_allowed(request):
        return HttpResponseForbidden('请在服务器电脑上使用本机地址，以管理员身份操作。')
    return JsonResponse(_state())


@login_required
@never_cache
@require_POST
def access_action(request):
    if not request.user.is_admin_role or not local_control_allowed(request):
        return HttpResponseForbidden('请在服务器电脑上使用本机地址，以管理员身份操作。')
    action = request.POST.get('action')
    if action not in ('restart', 'stop'):
        return JsonResponse({'error': '不支持的操作。'}, status=400)
    try:
        public_access.request_action(action)
    except (OSError, RuntimeError, ValueError):
        return JsonResponse({'error': '公网服务控制失败，请检查服务器日志后重试。'}, status=503)
    AuditEvent.objects.create(actor=request.user, action='public_access_' + action)
    return JsonResponse(_state(), status=202)
