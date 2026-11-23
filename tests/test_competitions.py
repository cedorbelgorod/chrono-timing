# -*- coding: utf-8 -*-
"""Страница «Соревнование»: дашборд подготовки и перенос данных протокола."""
from tests.support import ChronoTestCase


class CompetitionsDashboardTest(ChronoTestCase):
    def test_dashboard_sections(self):
        self.login_admin()
        html = self.client.get('/competitions').get_data(as_text=True)
        for text in ('Готовность к старту', 'Быстрые действия', 'Категории',
                     'Участники', 'Номера выданы', 'Старты', 'Протокол'):
            self.assertIn(text, html, text)
        self.assertIn('/manage', html)  # ссылка на управление соревнованиями

    def test_protocol_form_moved_to_protocol_page(self):
        self.login_admin()
        dashboard = self.client.get('/competitions').get_data(as_text=True)
        self.assertNotIn('name="proto_title"', dashboard)
        protocol = self.client.get('/protocol').get_data(as_text=True)
        self.assertIn('name="proto_title"', protocol)
        self.assertIn('Данные для протокола', protocol)

    def test_checklist_counts(self):
        self.login_admin()
        cat = self.add_category('Юноши', 2)
        self.add_participant(1, 'Первый', cat)
        self.add_participant(2, 'Второй', cat)
        html = self.client.get('/competitions').get_data(as_text=True)
        self.assertIn('0 / 2', html)       # номера: выдано 0 из 2
        self.assertIn('заполнить', html)   # протокол ещё не заполнен

    def test_protocol_save_returns_to_protocol(self):
        self.login_admin()
        resp = self.client.post('/competition/protocol-settings',
                                data={'proto_title': 'Тестовый заголовок',
                                      'proto_venue': 'Белгород'})
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/protocol', resp.headers['Location'])

    def test_protocol_filled_marker(self):
        self.login_admin()
        self.client.post('/competition/protocol-settings',
                         data={'proto_title': 'Тестовый заголовок'})
        html = self.client.get('/competitions').get_data(as_text=True)
        self.assertIn('заполнен', html)
