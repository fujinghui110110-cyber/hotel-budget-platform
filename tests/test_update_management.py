from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from budgeting.models import AuditEvent, User


@override_settings(PUBLIC_ACCESS=False, ALLOWED_HOSTS=['127.0.0.1', 'localhost', 'public.example'])
class UpdateManagementTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='update-admin', role=User.Role.ADMIN)
        self.client.force_login(self.admin)
        self.headers = {'HTTP_HOST': '127.0.0.1:8768', 'REMOTE_ADDR': '127.0.0.1'}

    @patch('budgeting.update_views.system_update.status', return_value={'current_version': 'v1', 'token': 'secret', 'configured': True})
    def test_status_whitelists_fields_and_prevents_cache(self, status):
        response = self.client.get('/management/update/status/', **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['current_version'], 'v1')
        self.assertNotIn('token', response.json())
        self.assertIn('no-store', response['Cache-Control'])

    @patch('budgeting.update_views.system_update.start_update', return_value={'busy': True})
    @patch('budgeting.update_views.system_update.check', return_value={'available_version': 'v2'})
    @patch('budgeting.update_views.system_update.configure')
    @patch('budgeting.update_views.system_update.status', return_value={'configured': True})
    def test_actions_and_credential_redaction(self, status, configure, check, install):
        self.assertEqual(self.client.get('/management/update/action/', **self.headers).status_code, 405)
        self.assertEqual(self.client.post('/management/update/action/', {'action': 'other'}, **self.headers).status_code, 400)
        self.assertEqual(self.client.post('/management/update/action/', {'action': 'configure'}, **self.headers).status_code, 400)
        result = self.client.post('/management/update/action/', {'action': 'configure', 'token': 'private-token'}, **self.headers)
        self.assertEqual(result.status_code, 200)
        configure.assert_called_once_with('private-token')
        self.assertNotIn('private-token', result.content.decode())
        self.assertFalse(AuditEvent.objects.filter(action__contains='private-token').exists())
        self.assertTrue(AuditEvent.objects.filter(action='system_update_configure').exists())
        self.assertEqual(self.client.post('/management/update/action/', {'action': 'check'}, **self.headers).json()['available_version'], 'v2')
        response = self.client.post('/management/update/action/', {'action': 'install'}, **self.headers)
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()['busy'])
        check.assert_called_once_with()
        install.assert_called_once_with()

    @patch('budgeting.update_views.system_update.configure', side_effect=RuntimeError('token=private-token'))
    def test_exception_does_not_expose_credentials(self, configure):
        result = self.client.post('/management/update/action/', {'action': 'configure', 'token': 'private-token'}, **self.headers)
        self.assertEqual(result.status_code, 503)
        self.assertNotIn('private-token', result.content.decode())

    @patch('budgeting.update_views.system_update.start_update')
    def test_remote_proxy_public_and_project_cannot_control(self, install):
        for headers in (
            {**self.headers, 'REMOTE_ADDR': '10.0.0.2'},
            {**self.headers, 'HTTP_HOST': 'public.example'},
            {**self.headers, 'HTTP_CF_CONNECTING_IP': '127.0.0.1'},
            {**self.headers, 'HTTP_X_FORWARDED_FOR': '127.0.0.1'},
            {**self.headers, 'HTTP_FORWARDED': 'for=127.0.0.1'},
        ):
            self.assertEqual(self.client.get('/management/update/status/', **headers).status_code, 403)
            for action in ('configure', 'check', 'install'):
                self.assertEqual(self.client.post('/management/update/action/', {'action': action}, **headers).status_code, 403)
        with override_settings(PUBLIC_ACCESS=True):
            self.assertEqual(self.client.post('/management/update/action/', {'action': 'install'}, **self.headers).status_code, 403)
            self.assertNotContains(self.client.get('/management/update/', **self.headers), 'id="update-form"')
        project = User.objects.create_user(username='update-project')
        self.client.force_login(project)
        for url in ('/management/update/', '/management/update/status/'):
            self.assertEqual(self.client.get(url, **self.headers).status_code, 403)
        self.assertEqual(self.client.post('/management/update/action/', {'action': 'install'}, **self.headers).status_code, 403)
        install.assert_not_called()

    @patch('budgeting.update_views.system_update.start_update')
    def test_csrf_and_login(self, install):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post('/management/update/action/', {'action': 'install'}, **self.headers).status_code, 403)
        install.assert_not_called()
        self.assertContains(self.client.get('/management/update/', **self.headers), 'id="update-form"')
        self.assertContains(self.client.get('/management/update/', **self.headers), 'budgeting/update.js')
        self.client.logout()
        self.assertEqual(self.client.get('/management/update/', **self.headers).status_code, 302)
