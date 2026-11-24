# -*- coding: utf-8 -*-
"""Возраст в категориях: разбор строки, годы рождения, подсветка, пересчёт."""
import os
import sqlite3

from tests.support import ChronoTestCase, chrono


class AgeRangeParseTest(ChronoTestCase):
    def test_parse_variants(self):
        from chrono.utils import _parse_age_range
        self.assertEqual(_parse_age_range('9-10'), (9, 10, None))
        self.assertEqual(_parse_age_range('-6'), (None, 6, None))
        self.assertEqual(_parse_age_range('9-'), (9, None, None))
        self.assertEqual(_parse_age_range('9'), (9, 9, None))
        self.assertEqual(_parse_age_range(''), (None, None, None))
        self.assertEqual(_parse_age_range(' 12 – 14 '), (12, 14, None))

    def test_parse_errors(self):
        from chrono.utils import _parse_age_range
        self.assertTrue(_parse_age_range('10-9')[2])
        self.assertTrue(_parse_age_range('abc')[2])
        self.assertTrue(_parse_age_range('-')[2])

    def test_years_from_age(self):
        from chrono.utils import _age_range_years
        self.assertEqual(_age_range_years(2026, 9, 10), (2016, 2017))
        self.assertEqual(_age_range_years(2026, None, 6), (2020, None))
        self.assertEqual(_age_range_years(2026, 9, None), (None, 2017))


class CategoryAgeTest(ChronoTestCase):
    def set_start_date(self, value):
        self.login_admin()
        return self.client.post('/competition/start-date', data={'start_date': value})

    def add_category_post(self, name, age_range, laps=1):
        return self.client.post('/competitions/%s/categories' % self.comp['id'],
                                data={'name': name, 'laps_total': str(laps),
                                      'age_range': age_range})

    def category(self, name):
        return self.conn.execute('SELECT * FROM categories WHERE name = ?', (name,)).fetchone()

    def test_age_saved_and_years_computed(self):
        self.set_start_date('2026-05-01')
        self.add_category_post('Юноши 9-10', '9-10')
        cat = self.category('Юноши 9-10')
        self.assertEqual(cat['age_range'], '9-10')
        self.assertEqual((cat['year_from'], cat['year_to']), (2016, 2017))

    def test_open_ranges(self):
        self.set_start_date('2026-05-01')
        self.add_category_post('До 6', '-6')
        self.add_category_post('18+', '18-')
        self.assertEqual((self.category('До 6')['year_from'], self.category('До 6')['year_to']),
                         (2020, None))
        self.assertEqual((self.category('18+')['year_from'], self.category('18+')['year_to']),
                         (None, 2008))

    def test_hint_on_categories_page(self):
        self.set_start_date('2026-05-01')
        self.add_category_post('Юноши 9-10', '9-10')
        html = self.client.get('/competitions/%s/categories' % self.comp['id']).get_data(as_text=True)
        self.assertIn('2016–2017 г.р.', html)

    def test_bad_age_rejected(self):
        self.set_start_date('2026-05-01')
        resp = self.add_category_post('Плохая', '10-9')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('не может быть больше', resp.get_data(as_text=True))
        self.assertIsNone(self.category('Плохая'))


class ParticipantMismatchTest(ChronoTestCase):
    def setUp(self):
        super().setUp()
        self.login_admin()
        self.client.post('/competition/start-date', data={'start_date': '2026-05-01'})
        self.client.post('/competitions/%s/categories' % self.comp['id'],
                         data={'name': 'Юноши 9-10', 'laps_total': '1', 'age_range': '9-10'})
        self.cat = self.conn.execute(
            "SELECT * FROM categories WHERE name = 'Юноши 9-10'").fetchone()

    def add_with_year(self, number, name, year):
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO participants (competition_id, number, full_name, gender, category_id, birth_year) "
                "VALUES (?, ?, ?, 'M', ?, ?)",
                (self.comp['id'], number, name, self.cat['id'], year))
        return cur.lastrowid

    def test_out_of_range_highlighted(self):
        self.add_with_year(1, 'Вне возраста', 2010)
        html = self.client.get('/competitions/%s' % self.comp['id']).get_data(as_text=True)
        self.assertIn('class="part-row row-mismatch"', html)
        self.assertIn('не по возрасту', html)

    def test_matching_participant_not_highlighted(self):
        self.add_with_year(2, 'По возрасту', 2016)
        html = self.client.get('/competitions/%s' % self.comp['id']).get_data(as_text=True)
        self.assertNotIn('class="part-row row-mismatch"', html)


class StartDateRecalcTest(ChronoTestCase):
    def test_year_change_asks_and_recalculates(self):
        self.login_admin()
        self.client.post('/competition/start-date', data={'start_date': '2026-05-01'})
        # категории: 15-16 и 17-18 (на 2026 год)
        self.client.post('/competitions/%s/categories' % self.comp['id'],
                         data={'name': '15-16', 'laps_total': '1', 'age_range': '15-16'})
        self.client.post('/competitions/%s/categories' % self.comp['id'],
                         data={'name': '17-18', 'laps_total': '1', 'age_range': '17-18'})
        a = self.conn.execute("SELECT * FROM categories WHERE name = '15-16'").fetchone()
        b = self.conn.execute("SELECT * FROM categories WHERE name = '17-18'").fetchone()
        with self.conn:
            pid = self.conn.execute(
                "INSERT INTO participants (competition_id, number, full_name, gender, category_id, birth_year) "
                "VALUES (?, 1, 'Райдер', 'M', ?, 2010)",
                (self.comp['id'], a['id'])).lastrowid

        # смена года без подтверждения — страница вопроса
        resp = self.client.post('/competition/start-date', data={'start_date': '2027-05-01'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Пересчитать и сохранить', resp.get_data(as_text=True))

        # подтверждаем — категории и участник пересчитаны
        resp = self.client.post('/competition/start-date',
                                data={'start_date': '2027-05-01', 'confirm': '1'})
        self.assertEqual(resp.status_code, 302)
        a2 = self.conn.execute("SELECT * FROM categories WHERE name = '15-16'").fetchone()
        b2 = self.conn.execute("SELECT * FROM categories WHERE name = '17-18'").fetchone()
        self.assertEqual((a2['year_from'], a2['year_to']), (2011, 2012))
        self.assertEqual((b2['year_from'], b2['year_to']), (2009, 2010))
        person = self.conn.execute('SELECT category_id FROM participants WHERE id = ?', (pid,)).fetchone()
        self.assertEqual(person['category_id'], b['id'])


class AgeMigrationTest(ChronoTestCase):
    def test_old_years_get_age_range(self):
        self.login_admin()
        self.client.post('/competition/start-date', data={'start_date': '2026-05-01'})
        with self.conn:
            self.conn.execute(
                'INSERT INTO categories (competition_id, name, laps_total, year_from, year_to) '
                'VALUES (?, ?, 1, ?, ?)', (self.comp['id'], 'Старая', 2016, 2017))
            self.conn.execute("UPDATE categories SET age_range = NULL WHERE name = 'Старая'")
        chrono.init_db(self.comp['path'])
        conn = chrono._connect(self.comp['path'])
        try:
            cat = conn.execute("SELECT * FROM categories WHERE name = 'Старая'").fetchone()
        finally:
            conn.close()
        self.assertEqual(cat['age_range'], '9-10')

    def test_missing_start_date_filled_with_today(self):
        with self.conn:
            self.conn.execute(
                'INSERT INTO categories (competition_id, name, laps_total, year_from, year_to) '
                'VALUES (?, ?, 1, ?, ?)', (self.comp['id'], 'Старая', 2016, 2017))
            self.conn.execute("DELETE FROM settings WHERE key = 'start_date'")
        chrono.init_db(self.comp['path'])
        conn = chrono._connect(self.comp['path'])
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = 'start_date'").fetchone()
            cat = conn.execute("SELECT * FROM categories WHERE name = 'Старая'").fetchone()
        finally:
            conn.close()
        self.assertTrue(row and len(row['value']) == 10)
        self.assertTrue(cat['age_range'])

    def test_open_ended_years_become_open_age(self):
        self.login_admin()
        self.client.post('/competition/start-date', data={'start_date': '2026-05-01'})
        with self.conn:
            self.conn.execute(
                'INSERT INTO categories (competition_id, name, laps_total, year_from, year_to) '
                'VALUES (?, ?, 1, ?, ?)', (self.comp['id'], '19+', 1900, 2007))
        chrono.init_db(self.comp['path'])
        conn = chrono._connect(self.comp['path'])
        try:
            cat = conn.execute("SELECT * FROM categories WHERE name = '19+'").fetchone()
        finally:
            conn.close()
        self.assertEqual(cat['age_range'], '19-')
        self.assertEqual((cat['year_from'], cat['year_to']), (1900, 2007))
