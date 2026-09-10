"""Public login safeguards shared by normal and Django admin authentication."""
import hashlib
import ipaddress
import sqlite3
import time

from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.http import HttpResponse


class PublicModelBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        user = super().authenticate(request, username=username, password=password, **kwargs)
        if user is not None and settings.PUBLIC_ACCESS:
            try:
                validate_password(password, user)
            except ValidationError:
                return None
        return user


def client_address(request):
    peer = request.META.get("REMOTE_ADDR", "")
    try:
        is_loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        is_loopback = False
    # Cloudflare overwrites this header; trust it only on the local tunnel origin.
    if settings.PUBLIC_ACCESS and is_loopback:
        candidate = request.META.get("HTTP_CF_CONNECTING_IP", "")
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return peer


def reserve_attempt(username, address, now=None):
    """Atomically count account AND IP requests across workers, before password hashing.

    SQLite's write transaction prevents parallel requests from bypassing the limit.
    Count successful attempts too, to avoid any reset-based bypass. Fixed windows
    last 15 minutes from the first attempt; no untrusted identifiers are persisted.
    """
    now = time.time() if now is None else now
    keys = [
        ("account:" + hashlib.sha256(username.casefold().strip().encode()).hexdigest(), 10),
        ("ip:" + hashlib.sha256(address.encode()).hexdigest(), 50),
    ]
    with sqlite3.connect(str(settings.LOGIN_RATE_LIMIT_PATH), timeout=5) as db:
        db.execute("CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM attempts WHERE expires <= ?", (now,))
        blocked = False
        for key, limit in keys:
            db.execute("INSERT INTO attempts VALUES (?, 1, ?) ON CONFLICT(key) DO UPDATE SET count = count + 1", (key, now + 900))
            count = db.execute("SELECT count FROM attempts WHERE key = ?", (key,)).fetchone()[0]
            blocked = blocked or count > limit
        return not blocked


class LoginRateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.PUBLIC_ACCESS and request.method == "POST" and request.path in {"/login/", "/admin/login/"}:
            try:
                allowed = reserve_attempt(request.POST.get("username", ""), client_address(request))
            except sqlite3.Error:
                # Do not expose unthrottled login if the shared store is unavailable.
                return HttpResponse("登录保护暂不可用，请稍后重试。", status=503)
            if not allowed:
                response = HttpResponse("登录尝试过于频繁，请在15分钟后重试。", status=429)
                response["Retry-After"] = "900"
                return response
        return self.get_response(request)
