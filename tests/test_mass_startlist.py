# -*- coding: utf-8 -*-
"""Стартовый протокол группового старта: порядок вызова, сортировка, плановое время."""
from tests.support import ChronoTestCase, chrono
from chrono import timing


class MassStartlistTest(ChronoTestCase):
    def _mass_heat(self):
        self.login_admin()
        self.set_format('mass')
        cat_a = self.add_category('А', 1, 1)
        cat_b = self.add_category('Б', 1, 2)
        p1 = self.add_participant(5, 'Яковлев Иван', cat_a)
        p2 = self.add_participant(1, 'Абрамов Пётр', cat_b)
        p3 = self.add_participant(3, 'Сидоров Лев', cat_a)
        hid = self.add_heat(1, 'mass')
        self.add_to_heat(hid, [p1, p2, p3])
        return hid, [p1, p2, p3]

    def _numbers(self, hid):
        return [r['number'] for r in timing._heat_start_rows(self.conn, self.heat(hid))]

    def test_manual_order_saved(self):
        hid, pids = self._mass_heat()
        resp = self.client.post(f'/heats/{hid}/order',
                                data={'order': f'{pids[2]},{pids[0]},{pids[1]}'})
        self.assertTrue(resp.get_json()['ok'])
        self.assertEqual(self._numbers(hid), [3, 5, 1])

    def test_reset_by_number_category_and_name(self):
        hid, pids = self._mass_heat()
        self.client.post(f'/heats/{hid}/order', data={'order': f'{pids[2]},{pids[0]},{pids[1]}'})

        self.client.post(f'/heats/{hid}/reset_order', data={'mode': 'number'})
        self.assertEqual(self._numbers(hid), [1, 3, 5])

        self.client.post(f'/heats/{hid}/reset_order', data={'mode': 'name'})
        self.assertEqual(self._numbers(hid), [1, 3, 5])  # Абрамов, Сидоров, Яковлев

        self.client.post(f'/heats/{hid}/reset_order', data={'mode': 'category'})
        self.assertEqual(self._numbers(hid), [3, 5, 1])  # группа А (сортировка 1), внутри — по номеру

    def test_start_time_is_same_for_all(self):
        hid, pids = self._mass_heat()
        resp = self.client.post(f'/heats/{hid}/start_at',
                                data={'start_at': '2026-11-24T10:00'})
        self.assertTrue(resp.get_json()['ok'])
        rows = timing._heat_start_rows(self.conn, self.heat(hid))
        self.assertEqual([r['planned_display'] for r in rows], ['10:00:00'] * 3)

    def test_startlist_page_shows_call_order_and_time(self):
        hid, pids = self._mass_heat()
        self.client.post(f'/heats/{hid}/start_at', data={'start_at': '2026-11-24T10:00'})
        page = self.client.get(f'/heats/{hid}/startlist').get_data(as_text=True)
        self.assertIn('Вызов', page)
        self.assertIn('порядок вызова', page)
        self.assertIn('10:00:00', page)
        self.assertIn('групповой старт', page)
