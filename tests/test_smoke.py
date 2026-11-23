# -*- coding: utf-8 -*-
"""Смоук: страницы и PDF открываются без ошибок."""
from tests.support import ChronoTestCase

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
