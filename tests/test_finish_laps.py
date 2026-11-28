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


    def test_edited_finish_shown_on_heat_card(self):
        rid, hid = self._setup(laps=2, finish=True)
        race = self.conn.execute('SELECT * FROM races WHERE id = ?', (rid,)).fetchone()
        new_finish = datetime.fromisoformat(race['start_time']) + timedelta(minutes=30)
        self.client.post(f'/races/{rid}/edit_finish', data={'finish_time': _iso(new_finish)})
        html = self.client.get('/races').get_data(as_text=True)
        self.assertIn('00:30:00', html)

    def test_edit_last_lap_moves_finish(self):
        rid, hid = self._setup(laps=2, finish=True)
        ids = self._lap_ids(rid)
        race = self.conn.execute('SELECT * FROM races WHERE id = ?', (rid,)).fetchone()
        new_last = datetime.fromisoformat(race['start_time']) + timedelta(minutes=7)
        self.client.post(f'/laps/{ids[2]}/edit', data={'lap_time': _iso(new_last)})
        race = self.conn.execute('SELECT * FROM races WHERE id = ?', (rid,)).fetchone()
        self.assertEqual(race['finish_time'], _iso(new_last))
        html = self.client.get('/races').get_data(as_text=True)
        self.assertIn('00:07:00', html)


    def test_edit_lap_by_elapsed_duration(self):
        rid, hid = self._setup(laps=3, finish=True)
        ids = self._lap_ids(rid)
        race = self.conn.execute('SELECT * FROM races WHERE id = ?', (rid,)).fetchone()
        start = datetime.fromisoformat(race['start_time'])
        # третий круг: предыдущий — старт+2 мин, прохождение 4 мин → старт+6 мин
        self.client.post(f'/laps/{ids[3]}/edit', data={'elapsed': '00:04:00'})
        laps = dict(self._laps(rid))
        self.assertEqual(laps[3], _iso(start + timedelta(minutes=6)))

    def test_lap_edit_writes_journal(self):
        rid, hid = self._setup(laps=3, finish=True)
        ids = self._lap_ids(rid)
        race = self.conn.execute('SELECT * FROM races WHERE id = ?', (rid,)).fetchone()
        start = datetime.fromisoformat(race['start_time'])
        old_dt = start + timedelta(minutes=3)
        new_dt = start + timedelta(minutes=9)
        self.client.post(f'/laps/{ids[3]}/edit', data={'lap_time': _iso(new_dt)})
        html = self.client.get('/logs').get_data(as_text=True)
        self.assertIn('правка круга', html)
        self.assertIn(old_dt.strftime('%H:%M:%S'), html)
        self.assertIn(new_dt.strftime('%H:%M:%S'), html)
        log = self.conn.execute(
            "SELECT * FROM timing_log WHERE action = 'lap_edit' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(log)
        self.assertIn('→', log['detail'])

    def test_lap_delete_writes_journal(self):
        rid, hid = self._setup(laps=3, finish=True)
        ids = self._lap_ids(rid)
        self.client.post(f'/laps/{ids[2]}/delete')
        html = self.client.get('/logs').get_data(as_text=True)
        self.assertIn('удаление круга', html)
        log = self.conn.execute(
            "SELECT * FROM timing_log WHERE action = 'lap_delete' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(log)
        self.assertIn('удалён К2', log['detail'])

    def test_finish_edit_adjusts_last_lap(self):
        rid, hid = self._setup(laps=2, finish=True)
        race = self.conn.execute('SELECT * FROM races WHERE id = ?', (rid,)).fetchone()
        start = datetime.fromisoformat(race['start_time'])
        new_finish = start + timedelta(minutes=12)
        self.client.post(f'/races/{rid}/edit_finish',
                         data={'finish_time': _iso(new_finish), 'adjust_last_lap': '1'})
        race = self.conn.execute('SELECT * FROM races WHERE id = ?', (rid,)).fetchone()
        self.assertEqual(race['finish_time'], _iso(new_finish))
        laps = dict(self._laps(rid))
        self.assertEqual(laps[2], _iso(new_finish))
        log = self.conn.execute(
            "SELECT * FROM timing_log WHERE action = 'finish_edit' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIn('последний круг', log['detail'])

    def test_finish_edit_without_adjust_keeps_lap(self):
        rid, hid = self._setup(laps=2, finish=True)
        ids = self._lap_ids(rid)
        before = dict(self._laps(rid))
        race = self.conn.execute('SELECT * FROM races WHERE id = ?', (rid,)).fetchone()
        start = datetime.fromisoformat(race['start_time'])
        new_finish = start + timedelta(minutes=15)
        self.client.post(f'/races/{rid}/edit_finish',
                         data={'finish_time': _iso(new_finish), 'adjust_last_lap': '0'})
        race = self.conn.execute('SELECT * FROM races WHERE id = ?', (rid,)).fetchone()
        self.assertEqual(race['finish_time'], _iso(new_finish))
        self.assertEqual(dict(self._laps(rid))[2], before[2])

    def test_finish_edit_adjust_validation(self):
        rid, hid = self._setup(laps=2, finish=True)
        before = self.conn.execute('SELECT finish_time FROM races WHERE id = ?', (rid,)).fetchone()
        race = self.conn.execute('SELECT * FROM races WHERE id = ?', (rid,)).fetchone()
        start = datetime.fromisoformat(race['start_time'])
        too_early = start + timedelta(seconds=30)  # раньше первого круга
        self.client.post(f'/races/{rid}/edit_finish',
                         data={'finish_time': _iso(too_early), 'adjust_last_lap': '1'})
        after = self.conn.execute('SELECT finish_time FROM races WHERE id = ?', (rid,)).fetchone()
        self.assertEqual(before['finish_time'], after['finish_time'])

    def test_dialogs_present(self):
        self.login_admin()
        html = self.client.get('/races').get_data(as_text=True)
        self.assertIn('id="lap-dialog"', html)
        self.assertIn('id="fd-adjust"', html)
        self.assertIn('Прохождение', html)
        self.assertIn('window.editLap = function', html)
        self.assertIn('function deleteLap(', html)

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
        # onclick в одинарных кавычках: внутри JSON-строка в двойных — иначе атрибут ломается
        self.assertIn("onclick='deleteLap(", html)
        self.assertNotIn('onclick="deleteLap(', html)
