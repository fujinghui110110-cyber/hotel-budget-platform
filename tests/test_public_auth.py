import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from django.contrib.auth import authenticate
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings

from budgeting.auth_security import LoginRateLimitMiddleware, client_address, reserve_attempt
from budgeting.forms import ProjectAccountForm, ResetPasswordForm
from budgeting.models import User


class PublicAuthTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings_context = override_settings(PUBLIC_ACCESS=True, LOGIN_RATE_LIMIT_PATH=Path(self.directory.name) / "limits.sqlite3")
        self.settings_context.enable()
        self.addCleanup(self.settings_context.disable)

    def test_weak_existing_password_rejected_only_on_public(self):
        User.objects.create_user(username="legacy", password="admin123")
        self.assertIsNone(authenticate(username="legacy", password="admin123"))
        with override_settings(PUBLIC_ACCESS=False):
            self.assertIsNotNone(authenticate(username="legacy", password="admin123"))

    def test_strong_password_works(self):
        User.objects.create_user(username="finance", password="T8#fH3!pQ9$zR2@w")
        self.assertIsNotNone(authenticate(username="finance", password="T8#fH3!pQ9$zR2@w"))

    def test_account_and_ip_limits_and_expiry(self):
        for _ in range(10):
            self.assertTrue(reserve_attempt("same", "1", now=100))
        self.assertFalse(reserve_attempt("same", "2", now=101))
        self.assertTrue(reserve_attempt("same", "1", now=1001))
        for index in range(50):
            self.assertTrue(reserve_attempt(str(index), "shared", now=1001))
        self.assertFalse(reserve_attempt("fresh-account", "shared", now=1001))

    def test_parallel_attempts_share_atomic_limit(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: reserve_attempt("parallel", "shared"), range(20)))
        self.assertEqual(sum(results), 10)

    def test_untrusted_cloudflare_header_ignored(self):
        factory = RequestFactory()
        self.assertEqual(client_address(factory.get("/", REMOTE_ADDR="8.8.8.8", HTTP_CF_CONNECTING_IP="1.1.1.1")), "8.8.8.8")
        self.assertEqual(client_address(factory.get("/", REMOTE_ADDR="127.0.0.1", HTTP_CF_CONNECTING_IP="1.1.1.1")), "1.1.1.1")

    def test_both_login_routes_are_limited(self):
        middleware = LoginRateLimitMiddleware(lambda request: HttpResponse("ok"))
        factory = RequestFactory()
        for _ in range(10):
            self.assertEqual(middleware(factory.post("/login/", {"username": "a"})).status_code, 200)
        self.assertEqual(middleware(factory.post("/admin/login/", {"username": "a"})).status_code, 429)

    def test_account_maintenance_validates_passwords(self):
        User.objects.create_user(username="existing")
        form = ResetPasswordForm({"username": "existing", "new_password": "project123"})
        self.assertFalse(form.is_valid())
        self.assertIn("new_password", form.errors)
        form = ProjectAccountForm({"username": "new", "password": "admin123", "is_admin": True})
        self.assertFalse(form.is_valid())
        self.assertIn("password", form.errors)
