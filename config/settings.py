import os
import shutil
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "local-mvp-change-before-network-deploy")
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = [value.strip() for value in os.getenv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if value.strip()]
if not DEBUG:
    if len(SECRET_KEY) < 50 or SECRET_KEY == "local-mvp-change-before-network-deploy":
        raise ImproperlyConfigured("正式运行必须配置至少50字符的独立 DJANGO_SECRET_KEY")
    if not os.getenv("DJANGO_ALLOWED_HOSTS") or "*" in ALLOWED_HOSTS:
        raise ImproperlyConfigured("正式运行必须配置明确的 DJANGO_ALLOWED_HOSTS，不能使用通配符")
CSRF_TRUSTED_ORIGINS = [value.strip() for value in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if value.strip()]
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "0" if DEBUG else "1") == "1"
CSRF_COOKIE_SECURE = os.getenv("CSRF_COOKIE_SECURE", "0" if DEBUG else "1") == "1"
SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "0" if DEBUG else "1") == "1"
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "0"))
if os.getenv("TRUST_PROXY", "0") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
BUDGET_PROCESS_UPLOAD_INLINE = os.getenv("BUDGET_PROCESS_UPLOAD_INLINE", "1" if DEBUG else "0") == "1"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "budgeting.apps.BudgetingConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": Path(os.getenv("DATABASE_PATH", BASE_DIR / "db.sqlite3")),
        "OPTIONS": {"timeout": 30},
    }
}
AUTH_USER_MODEL = "budgeting.User"
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_ROOT = Path(os.getenv("BUDGET_STORAGE_ROOT", BASE_DIR / "storage"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"
BUDGET_STORAGE_ROOT = Path(os.getenv("BUDGET_STORAGE_ROOT", BASE_DIR / "storage"))
SOURCE_WORKBOOK = Path(os.getenv("SOURCE_WORKBOOK", BASE_DIR / "source" / "template.xlsx"))
SOURCE_SHA256 = "bd62ff23f236b373c5f2cf38b146b7e7e5f099b38560c191fbb1ad51bf8141dc"
SOFFICE_BIN = os.getenv("SOFFICE_BIN", shutil.which("soffice") or "/Applications/LibreOffice.app/Contents/MacOS/soffice")
WORKER_HEARTBEAT_MAX_AGE_SECONDS = 30
DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024
