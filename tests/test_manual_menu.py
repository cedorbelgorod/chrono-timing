# -*- coding: utf-8 -*-
"""Меню участника в хронометраже: отмена и DNF, возврат со «Стартов»."""
from datetime import datetime, timedelta

from tests.support import ChronoTestCase


def _iso(dt):
    return dt.isoformat(timespec='seconds')


class ManualMenuTest(ChronoTestCase):
    def _setup(self, laps_total=2, laps_done=0, status='running', finish=None, heat_status='running'):
        self.login_admin()
        cid = self.comp['id']
        cat = self.add_category('Категория', laps_total, 1)
        pid = self.add_participant(7, 'Тестовый Спортсмен', cat)
        hid = self.add_heat(number=1, fmt='itt')
        self.add_to_heat(hid, [pid])
        start = datetime.now() - timedelta(minutes=10)
        with self.conn:
            self.conn.execute("UPDATE heats SET status = ?, format = 'itt', min_lap_time = 0 WHERE id = ?",
                              (heat_status, hid))
            self.conn.execute(
                'INSERT INTO races (participant_id, competition_id, heat_id, laps_total, planned_laps, '
                "start_time, status, finish_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, cid, hid, laps_total, laps_total, _iso(start), status, finish))
        rid = self.conn.execute('SELECT MAX(id) FROM races').fetchone()[0]
        with self.conn:
            for n in range(1, laps_done + 1):
                self.conn.execute(
                    'INSERT INTO laps (race_id, lap_number, lap_time) VALUES (?, ?, ?)',
                    (rid, n, _iso(start + timedelta(minutes=n))))
        return rid, hid

    def _race(self, rid):
        return self.conn.execute('SELECT * FROM races WHERE id = ?', (rid,)).fetchone()

    def _laps(self, rid):
        return [r['lap_number'] for r in self.conn.execute(
            'SELECT lap_number FROM laps WHERE race_id = ? ORDER BY lap_number', (rid,)).fetchall()]

    def test_manual_page_has_no_undo_button_or_finish_popup(self):
        self._setup()
        html = self.client.get('/manual').get_data(as_text=True)
        self.assertNotIn('m-undo-form-inline', html)
        self.assertNotIn('m-undo-btn-inline', html)
        self.assertNotIn('showFinishOverlay', html)
        self.assertNotIn('class="finish-overlay"', html)
        self.assertNotIn('Ctrl+Z', html)

    def test_manual_page_has_hidden_row_actions(self):
        self._setup()
        html = self.client.get('/manual').get_data(as_text=True)
        self.assertIn('m-row-menu-btn', html)
        self.assertIn('m-row-actions', html)
        self.assertIn('data-action="undo"', html)
        self.assertIn('data-action="dnf"', html)
        # действия скрыты, пока не открыты; автообновление не перерисовывает открытую строку
        self.assertIn('m-actions-open', html)
        self.assertIn('if (expandedRaceId) return;', html)

    def test_dnf_from_manual_removes_participant(self):
        rid, hid = self._setup(laps_done=1)
        resp = self.client.post(f'/manual/race/{rid}/dnf')
        data = resp.get_json()
        self.assertTrue(data['ok'])
        race = self._race(rid)
        self.assertEqual(race['status'], 'dnf')
        self.assertIsNone(race['finish_time'])
        html = self.client.get('/manual/active?cid=1').get_data(as_text=True)
        self.assertNotIn('data-number="7"', html)
        log = self.conn.execute(
            "SELECT * FROM timing_log WHERE action = 'dnf' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(log)
        self.assertIn('DNF', log['detail'])

    def test_undo_finish_from_menu(self):
        rid, hid = self._setup(laps_done=2, status='ok', finish='2026-11-28T10:02:00', heat_status='finished')
        resp = self.client.post(f'/manual/race/{rid}/undo')
        self.assertTrue(resp.get_json()['ok'])
        race = self._race(rid)
        self.assertEqual(race['status'], 'running')
        self.assertIsNone(race['finish_time'])
        self.assertEqual(self._laps(rid), [1])
        heat = self.conn.execute('SELECT status FROM heats WHERE id = ?', (hid,)).fetchone()
        self.assertEqual(heat['status'], 'running')

    def test_undo_last_lap_from_menu(self):
        rid, hid = self._setup(laps_done=1)
        resp = self.client.post(f'/manual/race/{rid}/undo')
        self.assertTrue(resp.get_json()['ok'])
        self.assertEqual(self._laps(rid), [])
        self.assertEqual(self._race(rid)['status'], 'running')

    def test_undo_nothing(self):
        rid, hid = self._setup()
        resp = self.client.post(f'/manual/race/{rid}/undo')
        self.assertFalse(resp.get_json()['ok'])

    def test_restore_after_dnf_from_races(self):
        rid, hid = self._setup(laps_done=1)
        self.client.post(f'/manual/race/{rid}/dnf')
        html = self.client.get('/races').get_data(as_text=True)
        self.assertIn(f'/races/{rid}/undnf', html)
        self.client.post(f'/races/{rid}/undnf')
        race = self._race(rid)
        self.assertEqual(race['status'], 'running')
        self.assertIsNone(race['finish_time'])
        html = self.client.get('/manual/active?cid=1').get_data(as_text=True)
        self.assertIn('data-number="7"', html)
        log = self.conn.execute(
            "SELECT * FROM timing_log WHERE action = 'undnf' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(log)
