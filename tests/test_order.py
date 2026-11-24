# -*- coding: utf-8 -*-
"""Порядок участников, плановое время старта и сброс порядка."""
from tests.support import ChronoTestCase


class OrderTest(ChronoTestCase):
    def test_save_order(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        resp = self.client.post(f'/heats/{hid}/order', data={'order': f'{p2},{p1}'})
        payload = resp.get_json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['saved'], 2)
        rows = self.conn.execute(
            'SELECT participant_id FROM heat_participants WHERE heat_id = ? ORDER BY sort_order',
            (hid,)).fetchall()
        self.assertEqual([r['participant_id'] for r in rows], [p2, p1])

    def test_rejects_started_participant(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        self.board('begin')
        self.board('start')
        resp = self.client.post(f'/heats/{hid}/order', data={'order': f'{p1},{p2}'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('уже стартовал', resp.get_json()['message'])

    def test_rejects_empty_order(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        resp = self.client.post(f'/heats/{hid}/order', data={'order': ''})
        self.assertEqual(resp.status_code, 400)

    def test_start_at_saved(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        resp = self.client.post(f'/heats/{hid}/start_at',
                                data={'start_at': '2026-11-24T10:00'})
        payload = resp.get_json()
        self.assertTrue(payload['ok'])
        self.assertIn('10:00', payload['display'])
        self.assertTrue(self.heat(hid)['start_at'].startswith('2026-11-24T10:00'))

    def test_reset_order(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        self.client.post(f'/heats/{hid}/order', data={'order': f'{p2},{p1}'})
        resp = self.client.post(f'/heats/{hid}/reset_order', data={'mode': 'number'})
        self.assertTrue(resp.get_json()['ok'])
        self.assertTrue(self.heat(hid)['start_order'].startswith('number:asc'))
        rows = self.conn.execute(
            'SELECT sort_order FROM heat_participants WHERE heat_id = ?', (hid,)).fetchall()
        self.assertTrue(all(r['sort_order'] is None for r in rows))

    def test_rename_autosave_json(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        resp = self.client.post(f'/heats/{hid}/rename', data={'name': 'Заезд 1'},
                                headers={'X-Requested-With': 'fetch'})
        self.assertTrue(resp.get_json()['ok'])
        self.assertEqual(self.heat(hid)['name'], 'Заезд 1')
        html = self.client.get('/races').get_data(as_text=True)
        self.assertIn('value="Заезд 1"', html)

    def test_edit_start_autosave_json(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        self.board('begin')
        self.board('start')
        resp = self.client.post(f'/heats/{hid}/edit_start',
                                data={'start_time': '2026-11-24T10:05:00'},
                                headers={'X-Requested-With': 'fetch'})
        data = resp.get_json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['display'], '10:05:00')
        self.assertTrue(self.heat(hid)['started_at'].startswith('2026-11-24T10:05'))

    def test_mass_heat_accepts_order(self):
        """В групповом старте порядок — это порядок вызова участников."""
        self.login_admin()
        self.set_format('mass')
        cat = self.add_category('Массовая', 1)
        p1 = self.add_participant(1, 'Один', cat)
        p2 = self.add_participant(2, 'Два', cat)
        hid = self.add_heat(1, 'mass')
        self.add_to_heat(hid, [p1, p2])
        resp = self.client.post(f'/heats/{hid}/order', data={'order': f'{p2},{p1}'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['ok'])
        rows = self.conn.execute(
            'SELECT participant_id FROM heat_participants WHERE heat_id = ? ORDER BY sort_order',
            (hid,)).fetchall()
        self.assertEqual([r['participant_id'] for r in rows], [p2, p1])
