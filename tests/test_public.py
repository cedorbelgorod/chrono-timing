# -*- coding: utf-8 -*-
"""Публичный хаб: страницы, изоляция соревнований, 404 для скрытых и архивных."""
import os

from tests.support import ChronoTestCase, chrono


class PublicHubTest(ChronoTestCase):
    def _add_competition(self, name, slug, hidden=False, archived=False):
        path = os.path.join(chrono.COMPS_DIR, slug + '.db')
        chrono._create_competition_db(path, name, 'admin', 'admin')
        reg = chrono._registry_load()
        reg['competitions'] = [c for c in reg['competitions'] if c.get('slug') != slug]
        reg['competitions'].append({
            'slug': slug, 'name': name, 'db': chrono._rel_db_path(path),
            'created': '2026-01-01T00:00:00', 'read_only': False,
            'archived': archived, 'hidden': hidden})
        chrono._registry_save(reg)
        return path

    def test_hub_lists_all_heats(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        html = self.public_client().get(f"/p/{self.comp['slug']}").get_data(as_text=True)
        self.assertIn('Стартовые протоколы', html)
        self.assertIn(f"/p/{self.comp['slug']}/sl/{hid}", html)
        self.assertIn('Результаты', html)

    def test_registration_button_toggles(self):
        slug = self.comp['slug']
        anon = self.public_client()
        self.assertNotIn('Зарегистрироваться', anon.get(f'/p/{slug}').get_data(as_text=True))
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('reg_enabled', '1')")
        with chrono._settings_lock:
            chrono._settings_cache.clear()
        self.assertIn('Зарегистрироваться', anon.get(f'/p/{slug}').get_data(as_text=True))

    def test_hub_links_to_registered_list(self):
        self.itt_heat(laps=(2, 3))
        slug = self.comp['slug']
        anon = self.public_client()
        html = anon.get(f'/p/{slug}').get_data(as_text=True)
        self.assertIn(f'/reg/{slug}/list', html)
        self.assertIn('Зарегистрированные (2)', html)
        self.assertEqual(anon.get(f'/reg/{slug}/list').status_code, 200)

    def test_hub_shows_heat_name_without_number(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        with self.conn:
            self.conn.execute("UPDATE heats SET name = 'Заезд А' WHERE id = ?", (hid,))
        html = self.public_client().get(f"/p/{self.comp['slug']}").get_data(as_text=True)
        self.assertIn('Заезд А', html)
        self.assertNotIn('Старт №1 · Заезд А', html)

    def test_hub_results_counter_and_live_indicator(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        slug = self.comp['slug']
        anon = self.public_client()
        html = anon.get(f'/p/{slug}').get_data(as_text=True)
        self.assertIn('live-dot off', html)
        self.assertIn('Результаты', html)

        self.board('begin')
        self.board('start')
        html = anon.get(f'/p/{slug}').get_data(as_text=True)
        self.assertIn('live-dot on', html)

        self.client.post(f'/heats/{hid}/force_finish')
        html = anon.get(f'/p/{slug}').get_data(as_text=True)
        self.assertIn('Результаты (1)', html)
        self.assertIn('live-dot off', html)

    def test_hidden_and_archived_are_404(self):
        self._add_competition('Скрытое', 'hidden-one', hidden=True)
        self._add_competition('Архивное', 'archive-one', archived=True)
        anon = self.public_client()
        self.assertEqual(anon.get('/p/hidden-one').status_code, 404)
        self.assertEqual(anon.get('/p/archive-one').status_code, 404)

    def test_results_are_isolated_between_competitions(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        self.board('begin')
        self.board('start')
        self.lap(1)
        self.lap(1)

        path = self._add_competition('Другое', 'other-one')
        conn = chrono._connect(path)
        try:
            with conn:
                cid = conn.execute('SELECT id FROM competitions LIMIT 1').fetchone()['id']
                cur = conn.execute(
                    "INSERT INTO participants (competition_id, number, full_name, gender) "
                    "VALUES (?, 99, 'Чужой Участник', 'M')", (cid,))
                oh = conn.execute(
                    "INSERT INTO heats (competition_id, heat_number, status, format, started_at, finished_at) "
                    "VALUES (?, 1, 'finished', 'itt', '2026-01-01T10:00:00', '2026-01-01T10:05:00')",
                    (cid,)).lastrowid
                conn.execute(
                    "INSERT INTO heat_participants (heat_id, participant_id, status) VALUES (?, ?, 'active')",
                    (oh, cur.lastrowid))
                conn.execute(
                    "INSERT INTO races (participant_id, competition_id, heat_id, laps_total, planned_laps, "
                    "start_time, finish_time, status) VALUES (?, ?, ?, 1, 1, ?, ?, 'ok')",
                    (cur.lastrowid, cid, oh, '2026-01-01T10:00:00', '2026-01-01T10:05:00'))
        finally:
            conn.close()

        anon = self.public_client()
        own = anon.get(f"/p/{self.comp['slug']}/results").get_data(as_text=True)
        other = anon.get('/p/other-one/results').get_data(as_text=True)
        self.assertNotIn('Чужой Участник', own)
        self.assertIn('Чужой Участник', other)
        self.assertNotIn('Первый Участник', other)

    def test_startlist_by_hid_and_foreign_hid(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        anon = self.public_client()
        self.assertEqual(anon.get(f"/p/{self.comp['slug']}/sl/{hid}").status_code, 200)
        self.assertEqual(anon.get(f"/p/{self.comp['slug']}/sl/9999").status_code, 404)

        path = self._add_competition('Другое', 'other-one')
        conn = chrono._connect(path)
        try:
            with conn:
                cid = conn.execute('SELECT id FROM competitions LIMIT 1').fetchone()['id']
                conn.execute(
                    "INSERT INTO heats (id, competition_id, heat_number, status, format) "
                    "VALUES (500, ?, 1, 'pending', 'itt')", (cid,))
        finally:
            conn.close()
        self.assertEqual(anon.get(f"/p/{self.comp['slug']}/sl/500").status_code, 404)

    def test_live_page_uses_own_data_url(self):
        self.itt_heat(laps=(2, 3))
        html = self.public_client().get(f"/p/{self.comp['slug']}/live").get_data(as_text=True)
        self.assertIn(f"/p/{self.comp['slug']}/live/data", html)
