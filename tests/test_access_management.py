from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from budgeting.models import User


@override_settings(PUBLIC_ACCESS=False, ALLOWED_HOSTS=['127.0.0.1', 'localhost', 'testserver'])
class AccessManagementTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='access-admin', role=User.Role.ADMIN)
        self.client.force_login(self.admin)
        self.headers = {'HTTP_HOST': '127.0.0.1:8768', 'REMOTE_ADDR': '127.0.0.1'}

    @patch('budgeting.access_views.public_access.status', return_value={'status': 'started', 'running': True, 'url': 'https://test-link.trycloudflare.com', 'pid': 123})
    def test_local_admin_can_read_safe_state(self, status):
        result = self.client.get('/management/access/status/', **self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['url'], 'https://test-link.trycloudflare.com')
        self.assertNotIn('pid', result.json())
        self.assertIn('no-store', result['Cache-Control'])

    @patch('budgeting.access_views.public_access.request_action')
    @patch('budgeting.access_views.public_access.status', return_value={'status': 'restarting', 'busy': True})
    def test_post_only_and_action_allowlist(self, status, action):
        self.assertEqual(self.client.get('/management/access/action/', **self.headers).status_code, 405)
        self.assertEqual(self.client.post('/management/access/action/', {'action': 'shell'}, **self.headers).status_code, 400)
        result = self.client.post('/management/access/action/', {'action': 'restart'}, **self.headers)
        self.assertEqual(result.status_code, 202)
        self.assertTrue(result.json()['busy'])
        action.assert_called_once_with('restart')

    @patch('budgeting.access_views.public_access.request_action')
    def test_project_and_nonlocal_requests_cannot_control(self, action):
        for headers in (
            {**self.headers, 'REMOTE_ADDR': '192.168.1.2'},
            {**self.headers, 'HTTP_HOST': 'testserver'},
            {**self.headers, 'HTTP_CF_CONNECTING_IP': '8.8.8.8'},
            {**self.headers, 'HTTP_X_FORWARDED_FOR': '127.0.0.1'},
        ):
            self.assertEqual(self.client.post('/management/access/action/', {'action': 'restart'}, **headers).status_code, 403)
            self.assertEqual(self.client.get('/management/access/status/', **headers).status_code, 403)
        with override_settings(PUBLIC_ACCESS=True):
            self.assertEqual(self.client.post('/management/access/action/', {'action': 'restart'}, **self.headers).status_code, 403)
            self.assertNotContains(self.client.get('/management/access/', **self.headers), 'id="access-form"')
        project_user = User.objects.create_user(username='access-project')
        self.client.force_login(project_user)
        for url in ('/management/access/', '/management/access/status/'):
            self.assertEqual(self.client.get(url, **self.headers).status_code, 403)
        self.assertEqual(self.client.post('/management/access/action/', {'action': 'restart'}, **self.headers).status_code, 403)
        action.assert_not_called()

    @patch('budgeting.access_views.public_access.request_action')
    def test_csrf_required(self, action):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post('/management/access/action/', {'action': 'restart'}, **self.headers).status_code, 403)
        action.assert_not_called()

    def test_page_and_anonymous_login(self):
        self.assertContains(self.client.get('/management/access/', **self.headers), '生成公网链接')
        self.client.logout()
        result = self.client.get('/management/access/', **self.headers)
        self.assertEqual(result.status_code, 302)
        self.assertIn('/login/', result.url)
