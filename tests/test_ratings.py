# -*- coding: utf-8 -*-
"""Рейтинги bike.cedor.ru (JSON): разбор, сопоставление, настройки, сортировка."""
import json

from tests.support import ChronoTestCase  # задаёт пути данных до импорта chrono

from chrono import ratings as ratings_mod
from chrono.ratings import (RatingsIndex, _parse_ratings_json, _rating_column_id,
                            _split_name)
from chrono.timing import _heat_start_rows, _manual_start_queue

DATA = {
    'schema': 1,
    'ratings': [
        {'id': 'mtb-2026', 'label': 'МТБ · 2026', 'athletes': 2},
        {'id': 'overall-2026', 'label': 'Все · 2026', 'athletes': 6},
        {'id': 'overall-all', 'label': 'Все · всё время', 'athletes': 6},
    ],
    'athletes': [
        {'name': 'Спиридонова Варвара Дмитриевна', 'birth_year': 2020, 'city': 'Белгород',
         'club': 'СШ 4',
         'ratings': {'mtb-2026': {'points': 40}, 'overall-2026': {'points': 125},
                     'overall-all': {'points': 173}}},
        {'name': 'Шрубченко Ольга', 'birth_year': 1977, 'city': 'Белгород', 'club': 'KopenkoPro',
         'ratings': {'overall-2026': {'points': 52}, 'overall-all': {'points': 52}}},
        {'name': 'Капустина Алена', 'birth_year': 2014, 'city': 'Разумное', 'club': 'МАУ ЦРС',
         'ratings': {'overall-2026': {'points': 35}}},
        {'name': 'Капустина Алена', 'birth_year': 2012, 'city': 'Разумное', 'club': 'МАУ ЦРС',
         'ratings': {'overall-2026': {'points': 12}}},
        {'name': 'Без Рейтинга', 'birth_year': 1990, 'city': 'Курск', 'club': '',
         'ratings': {}},
    ],
}
JSON_TEXT = json.dumps(DATA, ensure_ascii=False)


class RatingsParseTest(ChronoTestCase):
    def test_parse_json(self):
        rows, columns = _parse_ratings_json(JSON_TEXT)
        self.assertEqual([c['id'] for c in columns], ['mtb-2026', 'overall-2026', 'overall-all'])
        self.assertEqual(columns[0]['label'], 'МТБ · 2026')
        self.assertEqual(len(rows), 5)
        first = rows[0]
        self.assertEqual(first['surname_key'], 'спиридонова')
        self.assertEqual(first['first_key'], 'варвара')
        self.assertEqual(first['birth_year'], 2020)
        self.assertEqual(first['values']['overall-2026'], 125)
        self.assertEqual(rows[4]['values'], {})

    def test_split_name(self):
        self.assertEqual(_split_name('Спиридонова Варвара Дмитриевна'), ('спиридонова', 'варвара'))
        self.assertEqual(_split_name('Левченко'), ('левченко', ''))

    def test_legacy_column_names(self):
        self.assertEqual(_rating_column_id('Все 2026'), 'overall-2026')
        self.assertEqual(_rating_column_id('МТБ всё время'), 'mtb-all')
        self.assertEqual(_rating_column_id('overall-2026'), 'overall-2026')


class RatingsIndexTest(ChronoTestCase):
    def _index(self):
        rows, _cols = _parse_ratings_json(JSON_TEXT)
        return RatingsIndex(rows)

    def test_middle_name_ignored(self):
        idx = self._index()
        self.assertEqual(idx.value('Спиридонова Варвара', 2020, 'overall-2026'), 125)

    def test_same_name_different_year(self):
        idx = self._index()
        self.assertEqual(idx.value('Капустина Алена', 2014, 'overall-2026'), 35)
        self.assertEqual(idx.value('Капустина Алена', 2012, 'overall-2026'), 12)

    def test_unknown_and_empty(self):
        idx = self._index()
        self.assertEqual(idx.value('Нет Такого', 2000, 'overall-2026'), 0)
        self.assertEqual(idx.value('Спиридонова Варвара', 2020, ''), 0)


class RatingsSettingsTest(ChronoTestCase):
    def _seed_ratings(self):
        rows, columns = _parse_ratings_json(JSON_TEXT)
        conn = ratings_mod._ratings_db()
        try:
            with conn:
                for r in rows:
                    conn.execute(
                        'INSERT INTO ratings (full_name, surname_key, first_key, birth_year, city, team, values_json) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (r['full_name'], r['surname_key'], r['first_key'], r['birth_year'],
                         r['city'], r['team'], json.dumps(r['values'], ensure_ascii=False)))
                for key, value in (('columns', json.dumps(columns, ensure_ascii=False)),
                                   ('count', str(len(rows))),
                                   ('updated_at', '2026-01-01T10:00:00')):
                    conn.execute('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)', (key, value))
        finally:
            conn.close()

    def test_settings_save_and_page(self):
        self.login_admin()
        self._seed_ratings()
        resp = self.client.post('/competition/rating', data={'rating_column': 'overall-2026'})
        self.assertEqual(resp.status_code, 302)
        html = self.client.get('/competitions').get_data(as_text=True)
        self.assertIn('Все · 2026', html)
        self.assertIn('set-label">Рейтинг', html)

    def test_legacy_value_is_migrated(self):
        self.login_admin()
        self._seed_ratings()
        self.client.post('/competition/rating', data={'rating_column': 'Все 2026'})
        html = self.client.get('/competitions/1').get_data(as_text=True)
        # «Все 2026» из старой CSV-версии понимается как overall-2026
        self.assertIn('Рейтинг', html)

    def test_bad_column_rejected(self):
        self.login_admin()
        self._seed_ratings()
        resp = self.client.post('/competition/rating', data={'rating_column': 'Нет такого'},
                                follow_redirects=True)
        self.assertIn('нет в загруженном файле', resp.get_data(as_text=True))


class RatingsHeatTest(ChronoTestCase):
    def _seed(self):
        rows, columns = _parse_ratings_json(JSON_TEXT)
        conn = ratings_mod._ratings_db()
        try:
            with conn:
                for r in rows:
                    conn.execute(
                        'INSERT INTO ratings (full_name, surname_key, first_key, birth_year, city, team, values_json) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (r['full_name'], r['surname_key'], r['first_key'], r['birth_year'],
                         r['city'], r['team'], json.dumps(r['values'], ensure_ascii=False)))
                for key, value in (('columns', json.dumps(columns, ensure_ascii=False)),
                                   ('count', str(len(rows))),
                                   ('updated_at', '2026-01-01T10:00:00')):
                    conn.execute('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)', (key, value))
        finally:
            conn.close()

    def _participants(self):
        cid = self.comp['id']
        cat = self.add_category('Категория', 1, 1)
        ids = []
        with self.conn:
            for number, name, year, rating in (
                    (1, 'Первый Спортсмен', 2000, 100),
                    (2, 'Второй Спортсмен', 2001, 50),
                    (3, 'Третий Спортсмен', 2002, 0)):
                ids.append(self.conn.execute(
                    'INSERT INTO participants (competition_id, number, full_name, gender, birth_year, category_id) '
                    "VALUES (?, ?, ?, 'M', ?, ?)", (cid, number, name, year, cat)).lastrowid)
        return ids

    def _seed_custom(self, values):
        conn = ratings_mod._ratings_db()
        try:
            with conn:
                for name, year, rating in values:
                    surname, first = name.split(' ')
                    conn.execute(
                        'INSERT INTO ratings (full_name, surname_key, first_key, birth_year, city, team, values_json) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (name, surname.lower(), first.lower(), year, '', '',
                         json.dumps({'overall-2026': rating}, ensure_ascii=False)))
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('columns', ?)",
                             (json.dumps([{'id': 'overall-2026', 'label': 'Все · 2026',
                                           'athletes': len(values)}], ensure_ascii=False),))
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('count', ?)",
                             (str(len(values)),))
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('updated_at', ?)",
                             ('2026-01-01T10:00:00',))
        finally:
            conn.close()

    def test_heat_gets_rating_snapshot(self):
        self.login_admin()
        self._seed_custom([('Первый Спортсмен', 2000, 100),
                           ('Второй Спортсмен', 2001, 50),
                           ('Третий Спортсмен', 2002, 0)])
        self.client.post('/competition/rating', data={'rating_column': 'overall-2026'})
        ids = self._participants()
        resp = self.client.post('/races/create_heat', data={
            'competition_id': self.comp['id'],
            'participant_ids': ids,
            'start_order': 'rating',
            'min_lap_time': '5',
        })
        self.assertEqual(resp.status_code, 302)
        heat = self.conn.execute('SELECT * FROM heats ORDER BY id DESC LIMIT 1').fetchone()
        self.assertIn('rating:desc', heat['start_order'])
        rows = self.conn.execute(
            'SELECT participant_id, rating FROM heat_participants WHERE heat_id = ?', (heat['id'],)).fetchall()
        rating_by_pid = {r['participant_id']: r['rating'] for r in rows}
        self.assertEqual(rating_by_pid[ids[0]], 100)
        self.assertEqual(rating_by_pid[ids[1]], 50)
        self.assertEqual(rating_by_pid[ids[2]], 0)

    def test_sort_by_rating_desc_and_asc(self):
        self.login_admin()
        ids = self._participants()
        hid = self.add_heat(number=1, start_order='rating:desc,number:asc,category:asc,name:asc')
        with self.conn:
            self.conn.execute(
                "INSERT INTO heat_participants (heat_id, participant_id, status, rating) VALUES (?, ?, 'active', 100)",
                (hid, ids[0]))
            self.conn.execute(
                "INSERT INTO heat_participants (heat_id, participant_id, status, rating) VALUES (?, ?, 'active', 50)",
                (hid, ids[1]))
            self.conn.execute(
                "INSERT INTO heat_participants (heat_id, participant_id, status, rating) VALUES (?, ?, 'active', 0)",
                (hid, ids[2]))
        heat = self.conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        rows = _heat_start_rows(self.conn, heat)
        self.assertEqual([r['number'] for r in rows], [1, 2, 3])
        queue = _manual_start_queue(self.conn, hid)
        self.assertEqual([r['number'] for r in queue], [1, 2, 3])
        with self.conn:
            self.conn.execute(
                'UPDATE heats SET start_order = ? WHERE id = ?',
                ('rating:asc,number:asc,category:asc,name:asc', hid))
        heat = self.conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        rows = _heat_start_rows(self.conn, heat)
        self.assertEqual([r['number'] for r in rows], [3, 2, 1])

    def test_participants_page_shows_rating(self):
        self.login_admin()
        self._seed()
        self.client.post('/competition/rating', data={'rating_column': 'overall-2026'})
        self._participants()
        html = self.client.get(f'/competitions/{self.comp["id"]}').get_data(as_text=True)
        self.assertIn('Рейтинг', html)
        self.assertIn('100', html)
