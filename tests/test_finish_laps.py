# -*- coding: utf-8 -*-
"""Отмена финиша и редактирование кругов."""
from datetime import datetime, timedelta

from tests.support import ChronoTestCase


def _iso(dt):
    return dt.isoformat(timespec='seconds')


class FinishUndoTest(ChronoTestCase):
    def _setup_race(self, laps_total=2, laps_done=0, status='running', finish=False,
                    heat_status='running'):
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
                "start_time, status) VALUES (?, ?, ?, ?, ?, ?, 'running')",
                (pid, cid, hid, laps_total, laps_total, _iso(start)))
        rid = self.conn.execute('SELECT MAX(id) FROM races').fetchone()[0]
        for n in range(1, laps_done + 1):
            with self.conn:
                self.conn.execute(
                    'INSERT INTO laps (race_id, lap_number, lap_time) VALUES (?, ?, ?)',
                    (rid, n, _iso(start + timedelta(minutes=n))))
        if finish:
            with self.conn:
                self.conn.execute(
                    "UPDATE races SET status = 'ok', finish_time = ? WHERE id = ?",
                    (_iso(start + timedelta(minutes=laps_done)), rid))
        return rid, hid, pid

    def _race(self, rid):
        return self.conn.execute('SELECT * FROM races WHERE id = ?', (rid,)).fetchone()

    def _laps(self, rid):
        return [r['lap_number'] for r in self.conn.execute(
            'SELECT lap_number FROM laps WHERE race_id = ? ORDER BY lap_number', (rid,)).fetchall()]

    def test_undo_finish_removes_finishing_lap(self):
        rid, hid, pid = self._setup_race()
        cid = self.comp['id']
        r1 = self.client.post('/manual/action', data={'competition_id': cid, 'number': 7, 'json': '1'})
        self.assertTrue(r1.get_json()['ok'])
        self.assertEqual(self._laps(rid), [1])
        r2 = self.client.post('/manual/action', data={'competition_id': cid, 'number': 7, 'json': '1'})
        self.assertTrue(r2.get_json()['is_finish'])
        self.assertEqual(self._laps(rid), [1, 2])
        self.assertEqual(self._race(rid)['status'], 'ok')

        undo = self.client.post('/manual/undo', data={'competition_id': cid})
        self.assertEqual(undo.status_code, 302)
        race = self._race(rid)
        self.assertEqual(race['status'], 'running')
        self.assertIsNone(race['finish_time'])
        self.assertEqual(self._laps(rid), [1])

        r3 = self.client.post('/manual/action', data={'competition_id': cid, 'number': 7, 'json': '1'})
        self.assertTrue(r3.get_json()['is_finish'])
        self.assertEqual(self._laps(rid), [1, 2])
        race = self._race(rid)
        self.assertEqual(race['status'], 'ok')


class LapAdminTest(ChronoTestCase):
    def _setup(self, laps=3, finish=True, heat_status=None):
        self.login_admin()
        cid = self.comp['id']
        cat = self.add_category('Категория', laps, 1)
        pid = self.add_participant(5, 'Гонщик', cat)
        hid = self.add_heat(number=1, fmt='itt')
        self.add_to_heat(hid, [pid])
        start = datetime.now() - timedelta(minutes=20)
        with self.conn:
            self.conn.execute("UPDATE heats SET status = ?, min_lap_time = 0 WHERE id = ?",
                              (heat_status or ('finished' if finish else 'running'), hid))
            self.conn.execute(
                'INSERT INTO races (participant_id, competition_id, heat_id, laps_total, planned_laps, '
                "start_time, status, finish_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, cid, hid, laps, laps, _iso(start),
                 'ok' if finish else 'running',
                 _iso(start + timedelta(minutes=laps)) if finish else None))
        rid = self.conn.execute('SELECT MAX(id) FROM races').fetchone()[0]
        with self.conn:
            for n in range(1, laps + 1):
                self.conn.execute(
                    'INSERT INTO laps (race_id, lap_number, lap_time) VALUES (?, ?, ?)',
                    (rid, n, _iso(start + timedelta(minutes=n))))
        return rid, hid

    def _lap_ids(self, rid):
        return {r['lap_number']: r['id'] for r in self.conn.execute(
            'SELECT id, lap_number FROM laps WHERE race_id = ?', (rid,)).fetchall()}

    def _laps(self, rid):
        return [(r['lap_number'], r['lap_time']) for r in self.conn.execute(
            'SELECT lap_number, lap_time FROM laps WHERE race_id = ? ORDER BY lap_number',
            (rid,)).fetchall()]

    def test_delete_middle_lap_renumbers_and_keeps_finish(self):
        rid, hid = self._setup(laps=3, finish=True)
        ids = self._lap_ids(rid)
        before = self._laps(rid)
        resp = self.client.post(f'/laps/{ids[2]}/delete')
        self.assertEqual(resp.status_code, 302)
        after = self._laps(rid)
        self.assertEqual([n for n, _ in after], [1, 2])
        self.assertEqual(after[0][1], before[0][1])
        self.assertEqual(after[1][1], before[2][1])
        race = self.conn.execute('SELECT * FROM races WHERE id = ?', (rid,)).fetchone()
        self.assertEqual(race['status'], 'ok')
        self.assertIsNotNone(race['finish_time'])

    def test_delete_last_lap_clears_finish(self):
        rid, hid = self._setup(laps=2, finish=True)
        ids = self._lap_ids(rid)
        self.client.post(f'/laps/{ids[2]}/delete')
        race = self.conn.execute('SELECT * FROM races WHERE id = ?', (rid,)).fetchone()
        self.assertEqual(race['status'], 'running')
        self.assertIsNone(race['finish_time'])
        self.assertEqual([n for n, _ in self._laps(rid)], [1])
        heat = self.conn.execute('SELECT status FROM heats WHERE id = ?', (hid,)).fetchone()
        self.assertEqual(heat['status'], 'running')

    def test_edit_middle_lap(self):
        rid, hid = self._setup(laps=3, finish=True)
        ids = self._lap_ids(rid)
        start = datetime.now() - timedelta(minutes=20)
        new_time = _iso(start + timedelta(minutes=2, seconds=30))
        self.client.post(f'/laps/{ids[2]}/edit', data={'lap_time': new_time})
        laps = dict(self._laps(rid))
        self.assertEqual(laps[2], new_time)

    def test_edit_lap_validation(self):
        rid, hid = self._setup(laps=3, finish=True)
        ids = self._lap_ids(rid)
        before = self._laps(rid)
        start = datetime.now() - timedelta(minutes=20)
        too_early = _iso(start + timedelta(seconds=30))  # раньше первого круга
        self.client.post(f'/laps/{ids[2]}/edit', data={'lap_time': too_early})
        self.assertEqual(self._laps(rid), before)

    def test_finished_heat_shows_lap_controls(self):
        rid, hid = self._setup(laps=2, finish=True)
        ids = self._lap_ids(rid)
        html = self.client.get('/races').get_data(as_text=True)
        self.assertIn('времена', html)
        self.assertIn('editLap(', html)
        self.assertIn('deleteLap(', html)
        # кнопки привязаны к конкретным кругам
        self.assertIn(f'deleteLap({ids[1]}', html)
        self.assertIn(f'editLap({ids[2]}', html)
