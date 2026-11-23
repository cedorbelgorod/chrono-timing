# -*- coding: utf-8 -*-
"""Разделка: круги по категории, хронометраж, RFID, закрытие старта, результаты."""
from datetime import datetime, timedelta

from tests.support import ChronoTestCase, chrono


class IttLapsTest(ChronoTestCase):
    def test_race_created_with_category_laps(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        self.board('begin')
        self.board('start')
        row = self.race_row(p1)
        self.assertEqual((row['laps_total'], row['planned_laps']), (2, 2))
        self.board('start')
        row = self.race_row(p2)
        self.assertEqual((row['laps_total'], row['planned_laps']), (3, 3))

    def test_laps_and_finish_on_last(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        self.board('begin')
        self.board('start')
        first = self.lap(1)
        self.assertFalse(first['is_finish'])
        self.assertIn('Круг 1/2', first['action'])
        second = self.lap(1)
        self.assertTrue(second['is_finish'])
        row = self.race_row(p1)
        self.assertEqual(row['status'], 'ok')
        self.assertTrue(row['finish_time'])

    def test_skip_creates_dns_with_category_laps(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        self.board('begin')
        self.board('start')
        resp = self.board('skip').get_json()
        self.assertIn('Пропущен', resp['message'])
        row = self.race_row(p2)
        self.assertEqual(row['status'], 'dns')
        self.assertEqual((row['laps_total'], row['planned_laps']), (3, 3))

    def test_heat_closes_after_all_finished(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        self.board('begin')
        self.board('start')
        self.lap(1)
        self.lap(1)
        self.board('start')
        self.lap(2)
        self.lap(2)
        self.lap(2)
        self.assertEqual(self.heat(hid)['status'], 'finished')

    def test_manual_page_shows_lap_counter(self):
        hid, p1, p2 = self.itt_heat(laps=(3, 3))
        self.board('begin')
        self.board('start')
        self.lap(1)
        html = self.client.get('/manual').get_data(as_text=True)
        self.assertIn('Круги: 1 / 3', html)
        self.assertIn('КРУГ 2', html)

    def test_start_board_shows_laps(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        html = self.client.get('/manual?mode=start').get_data(as_text=True)
        self.assertIn('Круги: 2', html)
        state = self.board_state()
        self.assertEqual([q['laps'] for q in state['queue']], [2, 3])


class IttRfidTest(ChronoTestCase):
    def test_rfid_marks_each_scan_as_lap(self):
        self.login_admin()
        self.set_format('itt')
        cat = self.add_category('Категория', 3, 1)
        pid = self.add_participant(7, 'Меточный Участник', cat)
        hid = self.add_heat(1, 'itt', auto=True)
        self.add_to_heat(hid, [pid])
        self.add_tag(7, 'ABC001')
        self.board('begin')
        self.board('start')

        conn = chrono._connect(self.comp['path'])
        try:
            for i in range(1, 4):
                with conn:
                    res = chrono._process_tag_scan(conn, 'ABC001',
                                                   datetime.now() + timedelta(seconds=i))
                self.assertTrue(res['ok'], res)
                if i < 3:
                    self.assertFalse(res['is_finish'])
                    self.assertIn('Круг %d/3' % i, res['message'])
                else:
                    self.assertTrue(res['is_finish'])
            row = conn.execute('SELECT status FROM races WHERE participant_id = ?', (pid,)).fetchone()
            self.assertEqual(row['status'], 'ok')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM auto_laps').fetchone()[0], 3)
        finally:
            conn.close()


class IttResultsTest(ChronoTestCase):
    def test_results_have_laps_and_place_now(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        self.board('begin')
        self.board('start')
        self.lap(1)
        self.lap(1)
        self.board('start')

        with self.request_ctx():
            blocks = chrono._build_results_detail(self.conn, hid)
        rows = {}
        for block in blocks:
            for row in block['rows']:
                rows[row['race']['number']] = row

        self.assertEqual(rows[1]['laps_done'], 2)
        self.assertEqual(rows[1]['laps_total'], 2)
        self.assertEqual(rows[2]['laps_done'], 0)
        self.assertEqual(rows[2]['laps_total'], 3)
        self.assertEqual(rows[1]['place'], 1)
        # разные категории — у каждой свой зачёт
        self.assertEqual(rows[1]['place_now'], 1)
        # у второго ещё нет пройденных кругов — места «сейчас» нет
        self.assertIsNone(rows[2]['place_now'])

    def test_place_now_ranks_by_pace_within_category(self):
        self.login_admin()
        self.set_format('itt')
        cat = self.add_category('Общая', 2, 1)
        p1 = self.add_participant(1, 'Первый', cat)
        p2 = self.add_participant(2, 'Второй', cat)
        p3 = self.add_participant(3, 'Третий', cat)
        hid = self.add_heat(1, 'itt')
        self.add_to_heat(hid, [p1, p2, p3])

        cid = self.comp['id']
        with self.conn:
            # финишировавший: 120 с (два круга по 60 с)
            race1 = self.conn.execute(
                "INSERT INTO races (participant_id, competition_id, heat_id, laps_total, planned_laps, "
                "start_time, finish_time, status) VALUES (?, ?, ?, 2, 2, ?, ?, 'ok')",
                (p1, cid, hid, '2026-11-23T10:00:00', '2026-11-23T10:02:00')).lastrowid
            self.conn.execute(
                "INSERT INTO laps (race_id, lap_number, lap_time) VALUES (?, 1, ?)",
                (race1, '2026-11-23T10:01:00'))
            self.conn.execute(
                "INSERT INTO laps (race_id, lap_number, lap_time) VALUES (?, 2, ?)",
                (race1, '2026-11-23T10:02:00'))
            # едущий: один круг за 50 с → прогноз 100 с
            race2 = self.conn.execute(
                "INSERT INTO races (participant_id, competition_id, heat_id, laps_total, planned_laps, "
                "start_time, status) VALUES (?, ?, ?, 2, 2, ?, 'running')",
                (p2, cid, hid, '2026-11-23T10:00:00')).lastrowid
            self.conn.execute(
                "INSERT INTO laps (race_id, lap_number, lap_time) VALUES (?, 1, ?)",
                (race2, '2026-11-23T10:00:50'))
            # едущий без кругов — места нет
            self.conn.execute(
                "INSERT INTO races (participant_id, competition_id, heat_id, laps_total, planned_laps, "
                "start_time, status) VALUES (?, ?, ?, 2, 2, ?, 'running')",
                (p3, cid, hid, '2026-11-23T10:00:00'))

        with self.request_ctx():
            blocks = chrono._build_results_detail(self.conn, hid)
        rows = {row['race']['number']: row for block in blocks for row in block['rows']}
        self.assertEqual(rows[1]['place'], 1)
        self.assertEqual(rows[1]['place_now'], 2)
        self.assertEqual(rows[2]['place_now'], 1)
        self.assertIsNone(rows[3]['place_now'])

    def test_results_page_has_columns(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        self.board('begin')
        self.board('start')
        self.lap(1)
        self.lap(1)
        html = self.client.get(f'/results?target=heat:{hid}').get_data(as_text=True)
        self.assertIn('Место (сейчас)', html)
        self.assertIn('Круги', html)
