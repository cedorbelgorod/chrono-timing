# -*- coding: utf-8 -*-
"""DNS можно снять только у незапущенного старта."""
from tests.support import ChronoTestCase


class HeatUndnsTest(ChronoTestCase):
    def _setup(self):
        self.login_admin()
        cat = self.add_category('Категория', 1, 1)
        p1 = self.add_participant(1, 'Первый', cat)
        p2 = self.add_participant(2, 'Второй', cat)
        hid = self.add_heat(number=1)
        self.add_to_heat(hid, [p1, p2])
        with self.conn:
            self.conn.execute(
                "UPDATE heat_participants SET status = 'dns' "
                'WHERE heat_id = ? AND participant_id = ?', (hid, p2))
            self.conn.execute(
                'INSERT INTO races (participant_id, competition_id, heat_id, laps_total, '
                "planned_laps, start_time, status) VALUES (?, ?, ?, 1, 1, ?, 'dns')",
                (p2, self.comp['id'], hid, '2026-01-01T10:00:00'))
        return hid, p1, p2

    def _hp(self, hid, pid):
        return self.conn.execute(
            'SELECT status FROM heat_participants WHERE heat_id = ? AND participant_id = ?',
            (hid, pid)).fetchone()

    def test_returns_participant_to_start(self):
        hid, p1, p2 = self._setup()
        resp = self.client.post(f'/heats/{hid}/undns', data={'pid': p2},
                                headers={'X-Requested-With': 'fetch'})
        self.assertTrue(resp.get_json()['ok'])
        self.assertEqual(self._hp(hid, p2)['status'], 'active')
        races = self.conn.execute(
            'SELECT * FROM races WHERE heat_id = ? AND participant_id = ?', (hid, p2)).fetchall()
        self.assertEqual(races, [])

    def test_forbidden_when_heat_running(self):
        hid, p1, p2 = self._setup()
        with self.conn:
            self.conn.execute("UPDATE heats SET status = 'running' WHERE id = ?", (hid,))
        resp = self.client.post(f'/heats/{hid}/undns', data={'pid': p2},
                                headers={'X-Requested-With': 'fetch'})
        self.assertFalse(resp.get_json()['ok'])
        self.assertIn('запущен', resp.get_json()['message'])
        self.assertEqual(self._hp(hid, p2)['status'], 'dns')

    def test_unknown_participant(self):
        hid, p1, p2 = self._setup()
        resp = self.client.post(f'/heats/{hid}/undns', data={'pid': 999999},
                                headers={'X-Requested-With': 'fetch'})
        self.assertFalse(resp.get_json()['ok'])


    def test_mark_dns_before_start(self):
        hid, p1, p2 = self._setup()
        # p1 активен — отмечаем его DNS до старта
        resp = self.client.post(f'/heats/{hid}/set_dns', data={'pid': p1},
                                headers={'X-Requested-With': 'fetch'})
        self.assertTrue(resp.get_json()['ok'])
        self.assertEqual(self._hp(hid, p1)['status'], 'dns')

    def test_mark_dns_forbidden_when_running(self):
        hid, p1, p2 = self._setup()
        with self.conn:
            self.conn.execute("UPDATE heats SET status = 'running' WHERE id = ?", (hid,))
        resp = self.client.post(f'/heats/{hid}/set_dns', data={'pid': p1},
                                headers={'X-Requested-With': 'fetch'})
        self.assertFalse(resp.get_json()['ok'])
        self.assertEqual(self._hp(hid, p1)['status'], 'active')

    def test_dns_then_return_roundtrip(self):
        hid, p1, p2 = self._setup()
        self.client.post(f'/heats/{hid}/set_dns', data={'pid': p1},
                         headers={'X-Requested-With': 'fetch'})
        self.assertEqual(self._hp(hid, p1)['status'], 'dns')
        self.client.post(f'/heats/{hid}/undns', data={'pid': p1},
                         headers={'X-Requested-With': 'fetch'})
        self.assertEqual(self._hp(hid, p1)['status'], 'active')

    def test_dns_button_shown_for_pending_heat(self):
        hid, p1, p2 = self._setup()
        html = self.client.get('/races').get_data(as_text=True)
        self.assertIn('class="so-dns"', html)
        with self.conn:
            self.conn.execute("UPDATE heats SET status = 'running' WHERE id = ?", (hid,))
        html = self.client.get('/races').get_data(as_text=True)
        self.assertNotIn('class="so-dns"', html)

    def test_button_shown_only_for_pending_heat(self):
        hid, p1, p2 = self._setup()
        html = self.client.get('/races').get_data(as_text=True)
        self.assertIn('so-undns', html)
        self.assertIn('postRowAction', html)
        with self.conn:
            self.conn.execute("UPDATE heats SET status = 'running' WHERE id = ?", (hid,))
        html = self.client.get('/races').get_data(as_text=True)
        self.assertNotIn('class="so-undns"', html)
