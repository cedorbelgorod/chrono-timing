# -*- coding: utf-8 -*-
"""Порядок категорий: везде по categories.sort_order."""
from tests.support import ChronoTestCase

from chrono.results_helpers import _build_protocol
from chrono.timing import _build_heat_view, _build_races_heats, _build_results_detail


class CategoryOrderTest(ChronoTestCase):
    def _setup(self):
        self.login_admin()
        cid = self.comp['id']
        # специально «не по алфавиту»: Яблоко раньше Апельсина по sort_order
        with self.conn:
            yellow = self.conn.execute(
                'INSERT INTO categories (competition_id, name, laps_total, sort_order) '
                "VALUES (?, 'Яблоко', 1, 1)", (cid,)).lastrowid
            orange = self.conn.execute(
                'INSERT INTO categories (competition_id, name, laps_total, sort_order) '
                "VALUES (?, 'Апельсин', 1, 2)", (cid,)).lastrowid
        p1 = self.add_participant(1, 'Первый', yellow)
        p2 = self.add_participant(2, 'Второй', orange)
        hid = self.add_heat(number=1)
        self.add_to_heat(hid, [p1, p2])
        with self.conn:
            self.conn.execute('INSERT INTO heat_categories (heat_id, category_id) VALUES (?, ?)',
                              (hid, yellow))
            self.conn.execute('INSERT INTO heat_categories (heat_id, category_id) VALUES (?, ?)',
                              (hid, orange))
        with self.conn:
            self.conn.execute("UPDATE heats SET status = 'running' WHERE id = ?", (hid,))
            self.conn.execute(
                "INSERT INTO races (participant_id, competition_id, heat_id, laps_total, planned_laps, "
                "start_time, status, finish_time) VALUES (?, ?, ?, 1, 1, '2026-11-28T10:00:00', 'ok', "
                "'2026-11-28T10:05:00')", (p1, cid, hid))
            self.conn.execute(
                "INSERT INTO races (participant_id, competition_id, heat_id, laps_total, planned_laps, "
                "start_time, status, finish_time) VALUES (?, ?, ?, 1, 1, '2026-11-28T10:00:10', 'ok', "
                "'2026-11-28T10:06:00')", (p2, cid, hid))
        with self.conn:
            self.conn.execute("UPDATE heats SET status = 'finished' WHERE id = ?", (hid,))
        return cid, hid

    def test_results_detail_categories_order(self):
        cid, hid = self._setup()
        blocks = _build_results_detail(self.conn, hid, include_auto=False)
        self.assertEqual([b['category']['name'] for b in blocks], ['Яблоко', 'Апельсин'])

    def test_protocol_categories_order(self):
        cid, hid = self._setup()
        data = _build_protocol(self.conn, cid)
        names = [b['category']['name'] for b in data['blocks'] if b['category'].get('name')]
        self.assertEqual(names[:2], ['Яблоко', 'Апельсин'])

    def test_heat_view_categories_order(self):
        cid, hid = self._setup()
        heat = self.conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        view = _build_heat_view(self.conn, heat)
        self.assertEqual([c['name'] for c in view['categories']], ['Яблоко', 'Апельсин'])

    def test_races_heats_categories_order(self):
        cid, hid = self._setup()
        heats = _build_races_heats(self.conn, cid)
        heat = next(h for h in heats if h['id'] == hid)
        self.assertEqual([c['name'] for c in heat['categories']], ['Яблоко', 'Апельсин'])
        self.assertEqual([r['category_name'] for r in heat['races']], ['Яблоко', 'Апельсин'])

    def test_participants_category_filter_order(self):
        self._setup()
        html = self.client.get(f'/competitions/{self.comp["id"]}').get_data(as_text=True)
        first = html.find('Яблоко')
        second = html.find('Апельсин')
        self.assertNotEqual(first, -1)
        self.assertNotEqual(second, -1)
        self.assertLess(first, second)
