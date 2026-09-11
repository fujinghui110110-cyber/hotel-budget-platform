from django.conf import settings
from django.http import HttpResponse


class UpdateMaintenanceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path not in ('/healthz', '/management/update/status/') and (settings.BASE_DIR / '.runtime/update-maintenance').exists():
            return HttpResponse('系统正在更新，请稍后刷新。', status=503, headers={'Retry-After': '5'})
        return self.get_response(request)
