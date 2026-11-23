# -*- coding: utf-8 -*-
"""Массовый старт: заезды, круги, «финиш после лидера»."""
from tests.support import ChronoTestCase, chrono


class MassHeatTest(ChronoTestCase):
    def _mass_heat(self, laps=2, finish_on_leader=0, riders=2):
        self.login_admin()
        self.set_format('mass')
        cat = self.add_category('Массовая', laps, 1)
        pids = [self.add_participant(i + 1, f'Гонщик {i + 1}', cat) for i in range(riders)]
        hid = self.add_heat(1, 'mass', finish_on_leader=finish_on_leader)
        self.add_to_heat(hid, pids)
        row = self.heat(hid)
        self.assertIsNotNone(chrono._start_heat(self.conn, row))
        return hid, pids

    def test_start_creates_races_with_laps(self):
        hid, pids = self._mass_heat(laps=2)
        self.assertEqual(self.heat(hid)['status'], 'running')
        for pid in pids:
            row = self.race_row(pid)
            self.assertEqual((row['laps_total'], row['planned_laps'], row['status']), (2, 2, 'running'))

    def test_all_riders_finish_and_heat_closes(self):
        hid, pids = self._mass_heat(laps=2)
        for number in (1, 2):
            for _ in range(2):
                resp = self.lap(number)
                self.assertTrue(resp['ok'], resp)
        for pid in pids:
            self.assertEqual(self.race_row(pid)['status'], 'ok')
        self.assertEqual(self.heat(hid)['status'], 'finished')

    def test_finish_on_leader_cuts_remaining_laps(self):
        hid, pids = self._mass_heat(laps=3, finish_on_leader=1)
        for _ in range(3):
            self.lap(1)
        self.assertEqual(self.race_row(pids[0])['status'], 'ok')
        # отстающему остался один круг
        self.assertEqual(self.race_row(pids[1])['laps_total'], 1)
        resp = self.lap(2)
        self.assertTrue(resp['is_finish'])
        self.assertEqual(self.race_row(pids[1])['status'], 'ok')
        self.assertEqual(self.heat(hid)['status'], 'finished')

    def test_dns_participant_does_not_hold_heat(self):
        self.login_admin()
        self.set_format('mass')
        cat = self.add_category('Массовая', 1, 1)
        p1 = self.add_participant(1, 'Активный', cat)
        p2 = self.add_participant(2, 'Отказался', cat)
        hid = self.add_heat(1, 'mass')
        self.add_to_heat(hid, [p1])
        with self.conn:
            self.conn.execute(
                "INSERT INTO heat_participants (heat_id, participant_id, status) VALUES (?, ?, 'dns')",
                (hid, p2))
        self.assertIsNotNone(chrono._start_heat(self.conn, self.heat(hid)))
        self.lap(1)
        self.assertEqual(self.heat(hid)['status'], 'finished')
