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

    def test_sort_spec_priority_and_direction(self):
        hid, pids = self._mass_heat()

        def sort_spec(spec):
            return self.client.post(f'/heats/{hid}/sort', data={'spec': spec})

        # главный ключ — номер, по убыванию
        self.assertTrue(sort_spec('number:desc,category:asc,name:asc').get_json()['ok'])
        self.assertEqual(self._numbers(hid), [5, 3, 1])

        # главный ключ — группа, затем ФИО
        sort_spec('category:asc,name:asc,number:asc')
        self.assertEqual(self._numbers(hid), [3, 5, 1])  # А: Сидоров, Яковлев; Б: Абрамов

        # группа по убыванию, внутри — ФИО по убыванию
        sort_spec('category:desc,name:desc,number:asc')
        self.assertEqual(self._numbers(hid), [1, 5, 3])  # Б: Абрамов; А: Яковлев, Сидоров

        # главный ключ — ФИО
        sort_spec('name:asc,number:asc,category:asc')
        self.assertEqual(self._numbers(hid), [1, 3, 5])

    def test_sort_spec_toggling_direction_by_name(self):
        hid, pids = self._mass_heat()
        self.client.post(f'/heats/{hid}/sort', data={'spec': 'name:asc,number:asc,category:asc'})
        self.assertEqual(self._numbers(hid), [1, 3, 5])
        self.client.post(f'/heats/{hid}/sort', data={'spec': 'name:desc,number:asc,category:asc'})
        self.assertEqual(self._numbers(hid), [5, 3, 1])

    def test_create_heat_with_name(self):
        self.login_admin()
        self.set_format('mass')
        cat = self.add_category('Массовая', 1, 1)
        p1 = self.add_participant(1, 'Один', cat)
        resp = self.client.post('/races/create_heat', data={
            'competition_id': str(self.comp['id']),
            'participant_ids': [str(p1)],
            'name': 'Заезд 1 · юноши',
            'start_order': 'number',
            'min_lap_time': '5',
        })
        self.assertEqual(resp.status_code, 302)
        h = self.conn.execute('SELECT * FROM heats ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(h['name'], 'Заезд 1 · юноши')
        html = self.client.get('/races').get_data(as_text=True)
        self.assertIn('value="Заезд 1 · юноши"', html)

    def test_create_form_prefills_name(self):
        self.login_admin()
        self.set_format('mass')
        cat = self.add_category('Массовая', 1, 1)
        self.add_participant(1, 'Один', cat)
        html = self.client.get('/races').get_data(as_text=True)
        self.assertIn('value="Старт №1"', html)
