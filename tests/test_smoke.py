# -*- coding: utf-8 -*-
"""Смоук: страницы и PDF открываются без ошибок."""
import unittest
from unittest import mock

from tests.support import ChronoTestCase, chrono

ADMIN_PAGES = ('/', '/competitions', '/races', '/results', '/protocol', '/manual',
               '/dashboard', '/summary', '/logs', '/users', '/finish-screen')


class SmokeTest(ChronoTestCase):
    def test_admin_pages(self):
        self.login_admin()
        for path in ADMIN_PAGES:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_public_pages_without_login(self):
        slug = self.comp['slug']
        for path in ('/', '/app', f'/p/{slug}', f'/p/{slug}/results', f'/p/{slug}/live',
                     f'/p/{slug}/live/data'):
            with self.subTest(path=path):
                self.assertEqual(self.public_client().get(path).status_code, 200, path)

    def test_protocol_pdf(self):
        self.login_admin()
        resp = self.client.get('/protocol.pdf')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.startswith(b'%PDF-'))

    def test_unknown_public_competition_is_404(self):
        self.assertEqual(self.public_client().get('/p/no-such-competition').status_code, 404)


class ServerStartTest(ChronoTestCase):
    """Запуск `python app.py`: chrono.server.run должен инициализироваться без NameError."""

    def test_run_initialises_without_errors(self):
        from chrono import server

        class DummyServer:
            def serve_forever(self):
                pass

            def shutdown(self):
                pass

        with mock.patch('werkzeug.serving.make_server', return_value=DummyServer()), \
                mock.patch.object(server.time, 'sleep', side_effect=KeyboardInterrupt), \
                mock.patch.object(server, '_people_auto_refresh'), \
                mock.patch.object(server, 'start_swrfid_listener'):
            server.run(chrono.app)  # не должно бросить NameError/TypeError

