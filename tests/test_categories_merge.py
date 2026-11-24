# -*- coding: utf-8 -*-
"""Объединение категорий: переезд участников, неизменные условия, запреты."""
from datetime import datetime

from tests.support import ChronoTestCase


class CategoriesMergeTest(ChronoTestCase):
    def _years(self, age_range):
        """Годы рождения из строки возраста для текущего года старта."""
        if not age_range:
            return None, None
        start = datetime.now().year
        left, _, right = age_range.partition('-')
        age_min = int(left) if left.strip() else None
        age_max = int(right) if right.strip() else None
        return (start - age_max) if age_max is not None else None, \
               (start - age_min) if age_min is not None else None

    def _add(self, name, gender=None, age_range='', laps=1, sort=0):
        year_from, year_to = self._years(age_range)
        with self.conn:
            cur = self.conn.execute(
                'INSERT INTO categories (competition_id, name, laps_total, gender, age_range, '
                'sort_order, year_from, year_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (self.comp['id'], name, laps, gender, age_range, sort, year_from, year_to))
        return cur.lastrowid

    def _participant(self, number, name, cat, gender='M', birth_year=None):
        with self.conn:
            cur = self.conn.execute(
                'INSERT INTO participants (competition_id, number, full_name, gender, birth_year, '
                'category_id) VALUES (?, ?, ?, ?, ?, ?)',
                (self.comp['id'], number, name, gender, birth_year, cat))
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
        p1 = self._participant(1, 'Первый', a, 'M', 2012)   # 14 лет — подходит
        p2 = self._participant(2, 'Второй', b, 'M', 2008)   # 18 лет — не подходит
        resp = self._merge(a, [a, b])
        data = resp.get_json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['moved'], 1)
        self.assertEqual(data['mismatch'], 1)
        self.assertIsNone(self._cat(b))
        target = self._cat(a)
        self.assertEqual(target['name'], 'Юноши 12-14')
        self.assertEqual(target['laps_total'], 3)
        self.assertEqual(target['sort_order'], 5)
        # условия целевой не меняются
        self.assertEqual(target['gender'], 'M')
        self.assertEqual(target['age_range'], '12-14')
        for pid in (p1, p2):
            row = self.conn.execute(
                'SELECT category_id FROM participants WHERE id = ?', (pid,)).fetchone()
            self.assertEqual(row['category_id'], a)

    def test_target_conditions_unchanged_when_merging_other_gender(self):
        self.login_admin()
        a = self._add('Мужчины 18+', 'M', '18-')
        b = self._add('Женщины 18+', 'F', '18-')
        p = self._participant(1, 'Женщина', b, 'F', 2000)
        data = self._merge(a, [a, b]).get_json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['mismatch'], 1)
        target = self._cat(a)
        self.assertEqual(target['gender'], 'M')
        self.assertEqual(target['age_range'], '18-')
        row = self.conn.execute(
            'SELECT category_id FROM participants WHERE id = ?', (p,)).fetchone()
        self.assertEqual(row['category_id'], a)

    def test_mismatch_is_highlighted_on_participants_page(self):
        self.login_admin()
        a = self._add('Юноши 12-14', 'M', '12-14')
        b = self._add('Девушки 12-14', 'F', '12-14')
        self._participant(1, 'Девочка Не Туда', b, 'F', 2013)
        self._merge(a, [a, b])
        html = self.client.get(f'/competitions/{self.comp["id"]}').get_data(as_text=True)
        self.assertIn('row-mismatch', html)
        self.assertIn('не по возрасту', html)

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
        self.assertFalse(self._merge(a, [a]).get_json()['ok'])
        self.assertFalse(self._merge(a, [b, b]).get_json()['ok'])
        self.assertFalse(self._merge(a, [b, 999999]).get_json()['ok'])
        self.assertEqual(len(self._ids()), 2)

    def test_counts_after_merge_on_page(self):
        self.login_admin()
        a = self._add('Цель', 'M', '')
        b = self._add('Источник', 'M', '')
        self._participant(1, 'Первый', b)
        self._merge(a, [a, b])
        html = self.client.get(f'/competitions/{self.comp["id"]}/categories').get_data(as_text=True)
        self.assertIn('class="cat-cnt', html)
        self.assertNotIn('Источник', html)
