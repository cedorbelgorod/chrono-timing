# -*- coding: utf-8 -*-
"""Единый вид: токены, общие компоненты, терминология."""
from tests.support import ChronoTestCase


class UiTokensTest(ChronoTestCase):
    def test_tokens_present_in_base(self):
        self.login_admin()
        for url in ('/competitions', f'/competitions/{self.comp["id"]}', '/races'):
            html = self.client.get(url).get_data(as_text=True)
            self.assertIn('--primary: #2563eb', html, url)
            self.assertIn('--radius: 8px', html, url)

    def test_common_button_classes(self):
        self.login_admin()
        self.add_category('Категория А', 1, 1)
        with self.conn:
            self.conn.execute(
                "INSERT INTO participants (competition_id, number, full_name, gender) "
                "VALUES (?, 1, 'Тестовый Участник', 'M')", (self.comp['id'],))
        html = self.client.get(f'/competitions/{self.comp["id"]}').get_data(as_text=True)
        self.assertIn('class="btn btn-ghost"', html)


class UiButtonsTest(ChronoTestCase):
    def test_protocol_buttons_use_common_classes(self):
        self.login_admin()
        html = self.client.get('/protocol').get_data(as_text=True)
        self.assertIn('class="btn btn-secondary"', html)
        self.assertNotIn('class="btn-save"', html)

    def test_toolbar_buttons_use_common_classes(self):
        self.login_admin()
        cid = self.comp['id']
        tags = self.client.get(f'/competitions/{cid}/tags').get_data(as_text=True)
        self.assertIn('class="btn btn-sm btn-ghost"', tags)
        self.assertIn('class="btn btn-primary"', tags)
        regs = self.client.get('/registrations').get_data(as_text=True)
        self.assertIn('btn btn-sm btn-ghost', regs)

    def test_links_page_buttons_use_common_classes(self):
        self.login_admin()
        html = self.client.get(f'/competitions/{self.comp["id"]}/links').get_data(as_text=True)
        self.assertIn('btn btn-sm btn-secondary', html)
        self.assertIn('btn btn-sm btn-ghost', html)
        self.assertNotIn('link-btn', html)

    def test_planned_start_time_saved_automatically(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        html = self.client.get('/races').get_data(as_text=True)
        self.assertNotIn('so-start-at-save', html)
        self.assertIn('Время старта сохранено', html)


class UiSettingsLayoutTest(ChronoTestCase):
    def test_settings_are_compact_rows(self):
        self.login_admin()
        html = self.client.get('/competitions').get_data(as_text=True)
        self.assertIn('class="set-row"', html)
        self.assertIn('class="set-label"', html)
        self.assertIn('class="set-inline-fields"', html)
        self.assertNotIn('class="comp-grid"', html)
        self.assertNotIn('class="name-form"', html)


class UiSettingsAutosaveTest(ChronoTestCase):
    def test_settings_markup(self):
        self.login_admin()
        html = self.client.get('/competitions').get_data(as_text=True)
        self.assertIn('data-autosave-url', html)
        self.assertIn('id="settings-status"', html)
        self.assertIn('id="start-date-input"', html)
        self.assertIn('id="date-confirm"', html)
        self.assertIn('data-autosave-url', self.client.get('/races').get_data(as_text=True))

    def test_name_saved_by_fetch(self):
        self.login_admin()
        resp = self.client.post(
            f'/competitions/{self.comp["id"]}/edit', data={'name': 'Новое название'},
            headers={'X-Requested-With': 'fetch'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {'ok': True, 'name': 'Новое название'})
        self.assertIn('Новое название', self.client.get('/competitions').get_data(as_text=True))

    def test_empty_name_rejected_by_fetch(self):
        self.login_admin()
        resp = self.client.post(
            f'/competitions/{self.comp["id"]}/edit', data={'name': '   '},
            headers={'X-Requested-With': 'fetch'})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()['ok'])

    def test_start_date_saved_by_fetch(self):
        self.login_admin()
        resp = self.client.post('/competition/start-date', data={'start_date': '2027-05-15'},
                                headers={'X-Requested-With': 'fetch'})
        data = resp.get_json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['date'], '2027-05-15')
        # смена года при наличии участников — просит подтвердить пересчёт
        with self.conn:
            self.conn.execute(
                "INSERT INTO participants (competition_id, number, full_name, gender) "
                "VALUES (?, 1, 'Тестовый Участник', 'M')", (self.comp['id'],))
        resp = self.client.post('/competition/start-date', data={'start_date': '2026-05-15'},
                                headers={'X-Requested-With': 'fetch'})
        data = resp.get_json()
        self.assertTrue(data['need_confirm'])
        self.assertEqual(data['old_year'], 2027)
        self.assertEqual(data['new_year'], 2026)
        # подтверждённый вариант сохраняется
        resp = self.client.post('/competition/start-date',
                                data={'start_date': '2026-05-15', 'recalc': '0'},
                                headers={'X-Requested-With': 'fetch'})
        self.assertTrue(resp.get_json()['ok'])


class UiTerminologyTest(ChronoTestCase):
    def test_participants_use_category_wording(self):
        self.login_admin()
        self.add_category('Категория А', 1, 1)
        html = self.client.get(f'/competitions/{self.comp["id"]}').get_data(as_text=True)
        self.assertIn('Назначить категорию', html)
        self.assertIn('— без категории —', html)
        self.assertNotIn('без группы', html)

    def test_nav_menu_opens_one_at_a_time(self):
        self.login_admin()
        html = self.client.get('/competitions').get_data(as_text=True)
        self.assertIn("group.addEventListener('mouseenter'", html)
        self.assertIn("menu.classList.add('right')", html)
        # :focus-within держал бы первое меню открытым после клика
        self.assertNotIn('.nav-group:focus-within .nav-menu', html)

    def test_heading_without_emoji(self):
        self.login_admin()
        cid = self.comp['id']
        html = self.client.get(f'/competitions/{cid}/tags').get_data(as_text=True)
        self.assertIn('<h2>Метки на номерах', html)
        self.assertNotIn('<h2>📡', html)
