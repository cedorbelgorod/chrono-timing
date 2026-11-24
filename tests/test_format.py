# -*- coding: utf-8 -*-
"""Тип старта: задаётся при создании, не меняется, бейдж на странице входа."""
from tests.support import ChronoTestCase, chrono


class FormatCreationTest(ChronoTestCase):
    def manage_login(self):
        resp = self.client.post('/manage/login', data={'username': 'cedor', 'password': 'cedor'})
        self.assertIn(resp.status_code, (200, 302))

    def test_create_with_itt_format(self):
        self.manage_login()
        resp = self.client.post('/manage/create', data={
            'name': 'Разделка-тест', 'admin_login': 'admin', 'admin_password': 'admin',
            'format': 'itt'})
        self.assertEqual(resp.status_code, 302)
        entries = chrono._registry_load()['competitions']
        entry = next(c for c in entries if c['name'] == 'Разделка-тест')
        self.assertEqual(entry.get('format'), 'itt')
        conn = chrono._connect(chrono._comp_db_path(entry))
        try:
            comp = conn.execute('SELECT * FROM competitions ORDER BY id LIMIT 1').fetchone()
        finally:
            conn.close()
        self.assertEqual(comp['format'], 'itt')

    def test_chooser_badge(self):
        self.manage_login()
        self.client.post('/manage/create', data={
            'name': 'Разделка-бейдж', 'admin_login': 'admin', 'admin_password': 'admin',
            'format': 'itt'})
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('Разделка', html)
        self.assertIn('Массовый', html)  # у базового соревнования

    def test_format_not_editable_after_creation(self):
        self.login_admin()
        self.client.post('/competition/format',
                         data={'format': 'itt', 'itt_start_interval': '20'})
        self.conn.execute('SELECT 1')
        comp = self.conn.execute('SELECT format FROM competitions ORDER BY id LIMIT 1').fetchone()
        self.assertEqual(comp['format'], 'mass')
        html = self.client.get('/competitions').get_data(as_text=True)
        self.assertNotIn('name="format"', html)
        self.assertIn('при создании', html)
