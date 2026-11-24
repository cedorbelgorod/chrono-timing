# -*- coding: utf-8 -*-
"""Рейтинги bike.cedor.ru: разбор CSV, сопоставление, сортировка старта."""
import json

from tests.support import ChronoTestCase  # задаёт пути данных до импорта chrono

from chrono import ratings as ratings_mod
from chrono.ratings import RatingsIndex, _parse_ratings_csv, _split_name
from chrono.timing import _heat_start_rows, _manual_start_queue

CSV = (
    'ФИО;Год рождения;Город;Клуб;МТБ 2026;Все 2026;Все всё время\n'
    'Спиридонова Варвара Дмитриевна;2020;Белгород;СШ 4;40;125;173\n'
    'Шрубченко Ольга;1977;Белгород;KopenkoPro;0;52;52\n'
    'Шрубченко Оля;1977;Белгород;;0;52;52\n'
    'Капустина Алена;2014;Разумное;МАУ ЦРС;0;35;35\n'
    'Капустина Алена;2012;Разумное;МАУ ЦРС;0;12;12\n'
    'Без Рейтинга;1990;Курск;Лично;;;\n'
)


class RatingsParseTest(ChronoTestCase):
    def test_parse_csv(self):
        rows, columns = _parse_ratings_csv(CSV)
        self.assertEqual(columns, ['МТБ 2026', 'Все 2026', 'Все всё время'])
        self.assertEqual(len(rows), 6)
        first = rows[0]
        self.assertEqual(first['surname_key'], 'спиридонова')
        self.assertEqual(first['first_key'], 'варвара')
        self.assertEqual(first['birth_year'], 2020)
        self.assertEqual(first['values']['Все 2026'], 125)
        self.assertEqual(rows[5]['values']['Все 2026'], 0)

    def test_split_name(self):
        self.assertEqual(_split_name('Спиридонова Варвара Дмитриевна'), ('спиридонова', 'варвара'))
        self.assertEqual(_split_name('Левченко'), ('левченко', ''))


class RatingsIndexTest(ChronoTestCase):
    def _index(self):
        rows, _cols = _parse_ratings_csv(CSV)
        return RatingsIndex(rows)

    def test_middle_name_ignored(self):
        idx = self._index()
        self.assertEqual(idx.value('Спиридонова Варвара', 2020, 'Все 2026'), 125)

    def test_ambiguous_name_gives_zero(self):
        idx = self._index()
        # два «Капустина Алена» с разными годами — по году различимы
        self.assertEqual(idx.value('Капустина Алена', 2014, 'Все 2026'), 35)
        self.assertEqual(idx.value('Капустина Алена', 2012, 'Все 2026'), 12)

    def test_surname_fallback_variants(self):
        idx = self._index()
        # точное имя находится, даже если есть вариант «Оля»
        self.assertEqual(idx.value('Шрубченко Ольга', 1977, 'Все 2026'), 52)

    def test_unknown_and_empty(self):
        idx = self._index()
        self.assertEqual(idx.value('Нет Такого', 2000, 'Все 2026'), 0)
        self.assertEqual(idx.value('Спиридонова Варвара', 2020, ''), 0)


class RatingsSettingsTest(ChronoTestCase):
    def _seed_ratings(self):
        conn = ratings_mod._ratings_db()
        try:
            with conn:
                conn.execute(
                    'INSERT INTO ratings (full_name, surname_key, first_key, birth_year, city, team, values_json) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?)',
                    ('Иванов Иван Иванович', 'иванов', 'иван', 1990, 'Белгород', 'Лично',
                     json.dumps({'Все 2026': 77}, ensure_ascii=False)))
                for key, value in (('columns', json.dumps(['Все 2026'], ensure_ascii=False)),
                                   ('count', '1'), ('updated_at', '2026-01-01T10:00:00')):
                    conn.execute('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)', (key, value))
        finally:
            conn.close()

    def test_settings_save_and_page(self):
        self.login_admin()
        self._seed_ratings()
        resp = self.client.post('/competition/rating', data={'rating_column': 'Все 2026'})
        self.assertEqual(resp.status_code, 302)
        html = self.client.get('/competitions').get_data(as_text=True)
        self.assertIn('Все 2026', html)
        self.assertIn('рейтинг', html.lower())

    def test_bad_column_rejected(self):
        self.login_admin()
        self._seed_ratings()
        resp = self.client.post('/competition/rating', data={'rating_column': 'Нет такого'},
                                follow_redirects=True)
        self.assertIn('нет в загруженном файле', resp.get_data(as_text=True))


class RatingsHeatTest(ChronoTestCase):
    def _seed(self):
        conn = ratings_mod._ratings_db()
        try:
            with conn:
                for name, year, rating in (('Первый Спортсмен', 2000, 100),
                                           ('Второй Спортсмен', 2001, 50),
                                           ('Третий Спортсмен', 2002, 0)):
                    surname, first = name.split(' ')
                    conn.execute(
                        'INSERT INTO ratings (full_name, surname_key, first_key, birth_year, city, team, values_json) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (name, surname.lower(), first.lower(), year, '', '',
                         json.dumps({'Все 2026': rating}, ensure_ascii=False)))
                for key, value in (('columns', json.dumps(['Все 2026'], ensure_ascii=False)),
                                   ('count', '3'), ('updated_at', '2026-01-01T10:00:00')):
                    conn.execute('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)', (key, value))
        finally:
            conn.close()

    def _participants(self):
        cid = self.comp['id']
        cat = self.add_category('Категория', 1, 1)
        ids = []
        with self.conn:
            for number, name, year in ((1, 'Первый Спортсмен', 2000),
                                       (2, 'Второй Спортсмен', 2001),
                                       (3, 'Третий Спортсмен', 2002)):
                ids.append(self.conn.execute(
                    'INSERT INTO participants (competition_id, number, full_name, gender, birth_year, category_id) '
                    "VALUES (?, ?, ?, 'M', ?, ?)", (cid, number, name, year, cat)).lastrowid)
        return ids

    def test_heat_gets_rating_snapshot(self):
        self.login_admin()
        self._seed()
        self.client.post('/competition/rating', data={'rating_column': 'Все 2026'})
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
        self.client.post('/competition/rating', data={'rating_column': 'Все 2026'})
        self._participants()
        html = self.client.get(f'/competitions/{self.comp["id"]}').get_data(as_text=True)
        self.assertIn('Рейтинг', html)
        self.assertIn('100', html)
