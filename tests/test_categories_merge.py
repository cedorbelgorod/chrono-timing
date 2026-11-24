# -*- coding: utf-8 -*-
"""Объединение категорий: переезд участников, условия, запреты."""
from tests.support import ChronoTestCase


class CategoriesMergeTest(ChronoTestCase):
    def _add(self, name, gender=None, age_range='', laps=1, sort=0,
             year_from=None, year_to=None):
        with self.conn:
            cur = self.conn.execute(
                'INSERT INTO categories (competition_id, name, laps_total, gender, age_range, '
                'sort_order, year_from, year_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (self.comp['id'], name, laps, gender, age_range, sort, year_from, year_to))
        return cur.lastrowid

    def _merge(self, target, ids, ajax=True):
        data = {'target_id': target, 'ids': ids}
        headers = {'X-Requested-With': 'fetch'} if ajax else {}
        return self.client.post(f'/competitions/{self.comp["id"]}/categories/merge',
                                data=data, headers=headers)

    def _cat(self, cat_id):
        return self.conn.execute('SELECT * FROM categories WHERE id = ?', (cat_id,)).fetchone()

    def _ids(self):
        return [r['id'] for r in self.conn.execute(
            'SELECT id FROM categories WHERE competition_id = ? ORDER BY id', (self.comp['id'],))]

    def test_merge_moves_participants_and_keeps_target(self):
        self.login_admin()
        a = self._add('Юноши 12-14', 'M', '12-14', laps=3, sort=5)
        b = self._add('Юноши 15-17', 'M', '15-17', laps=1, sort=9)
        p1 = self.add_participant(1, 'Первый', a)
        p2 = self.add_participant(2, 'Второй', b)
        resp = self._merge(a, [a, b])
        self.assertTrue(resp.get_json()['ok'])
        self.assertEqual(resp.get_json()['moved'], 1)
        self.assertIsNone(self._cat(b))
        target = self._cat(a)
        self.assertEqual(target['name'], 'Юноши 12-14')
        self.assertEqual(target['laps_total'], 3)
        self.assertEqual(target['sort_order'], 5)
        self.assertEqual(target['age_range'], '12-17')
        for pid in (p1, p2):
            row = self.conn.execute('SELECT category_id FROM participants WHERE id = ?', (pid,)).fetchone()
            self.assertEqual(row['category_id'], a)

    def test_merge_genders_union(self):
        self.login_admin()
        a = self._add('Мужчины', 'M', '18-')
        b = self._add('Женщины', 'F', '18-')
        self._merge(a, [a, b])
        self.assertEqual(self._cat(a)['gender'], 'MF')

    def test_merge_age_ranges(self):
        self.login_admin()
        cases = [('12-14', '15-17', '12-17'),
                 ('-8', '9-10', '-10'),
                 ('18-', '40-', '18-'),
                 ('12-14', '', '')]
        for i, (left, right, expected) in enumerate(cases):
            a = self._add(f'A{i}', 'M', left)
            b = self._add(f'B{i}', 'M', right)
            resp = self._merge(a, [a, b])
            self.assertTrue(resp.get_json()['ok'], (left, right))
            self.assertEqual(self._cat(a)['age_range'], expected, (left, right))

    def test_forbidden_when_used_in_finished_heat(self):
        self.login_admin()
        a = self._add('Пустая', 'M', '')
        b = self._add('Использованная', 'M', '')
        hid = self.add_heat(number=1)
        with self.conn:
            self.conn.execute('INSERT INTO heat_categories (heat_id, category_id) VALUES (?, ?)',
                              (hid, b))
            self.conn.execute("UPDATE heats SET status = 'finished' WHERE id = ?", (hid,))
        resp = self._merge(a, [a, b])
        self.assertFalse(resp.get_json()['ok'])
        self.assertIn('участвовавшие', resp.get_json()['error'])
        self.assertIsNotNone(self._cat(b))
        self.assertEqual(len(self._ids()), 2)

    def test_pending_heat_link_moves_to_target(self):
        self.login_admin()
        a = self._add('Цель', 'M', '')
        b = self._add('Источник', 'M', '')
        hid = self.add_heat(number=1)
        with self.conn:
            self.conn.execute('INSERT INTO heat_categories (heat_id, category_id) VALUES (?, ?)',
                              (hid, b))
        resp = self._merge(a, [a, b])
        self.assertTrue(resp.get_json()['ok'])
        rows = self.conn.execute(
            'SELECT category_id FROM heat_categories WHERE heat_id = ?', (hid,)).fetchall()
        self.assertEqual([r['category_id'] for r in rows], [a])

    def test_validation_errors(self):
        self.login_admin()
        a = self._add('Одна', 'M', '')
        b = self._add('Две', 'M', '')
        r = self._merge(a, [a])
        self.assertFalse(r.get_json()['ok'])
        r = self._merge(a, [b, b])
        self.assertFalse(r.get_json()['ok'])
        r = self._merge(a, [b, 999999])
        self.assertFalse(r.get_json()['ok'])
        self.assertEqual(len(self._ids()), 2)

    def test_counts_after_merge_on_page(self):
        self.login_admin()
        a = self._add('Цель', 'M', '')
        b = self._add('Источник', 'M', '')
        self.add_participant(1, 'Первый', b)
        self._merge(a, [a, b])
        html = self.client.get(f'/competitions/{self.comp["id"]}/categories').get_data(as_text=True)
        self.assertIn('class="cat-cnt', html)
        self.assertNotIn('Источник', html)
