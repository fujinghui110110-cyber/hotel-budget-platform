import os
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse


class UpdateMaintenanceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        runtime_root = Path(os.environ.get('BUDGET_RUNTIME_ROOT', settings.BASE_DIR / '.runtime'))
        if request.path not in ('/healthz', '/management/update/status/') and (runtime_root / 'update-maintenance').exists():
            return HttpResponse('系统正在更新，请稍后刷新。', status=503, headers={'Retry-After': '5'})
        return self.get_response(request)
