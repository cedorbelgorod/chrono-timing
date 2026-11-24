# -*- coding: utf-8 -*-
"""Рейтинги bike.cedor.ru — кэш CSV и связывание с участниками.

Файл ratings-all.csv: ФИО;Год рождения;Город;Клуб;<рейтинги...>
Кэшируется в ratings.db (как список участников в people.db) и обновляется раз в сутки.
"""
from datetime import datetime, timedelta
import csv as _csv
import io
import json
import os
import re
import threading

from chrono import db as _db
from chrono.db import (_connect, RATINGS_DB)
from chrono.logging_setup import (_app_logger)
from chrono.utils import (_parse_year)

RATINGS_URL = os.environ.get('CHRONO_RATINGS_URL', 'https://bike.cedor.ru/ratings-all.csv')

RATINGS_TTL_HOURS = 24

DEFAULT_RATING_COLUMN = 'Все 2026'

BASE_COLUMNS = ('фио', 'год рождения', 'город', 'клуб')


def _ratings_db():
    conn = _connect(_db.RATINGS_DB if hasattr(_db, 'RATINGS_DB') else RATINGS_DB)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            surname_key TEXT NOT NULL,
            first_key TEXT NOT NULL,
            birth_year INTEGER,
            city TEXT,
            team TEXT,
            values_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ratings_full ON ratings(surname_key, first_key, birth_year);
        CREATE INDEX IF NOT EXISTS idx_ratings_surname ON ratings(surname_key, birth_year);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    return conn


def _norm_name(s):
    s = (s or '').strip().lower().replace('ё', 'е')
    s = re.sub(r'[^0-9a-zа-я\- ]+', ' ', s)
    return ' '.join(s.split())


def _split_name(full_name):
    """«Спиридонова Варвара Дмитриевна» → ('спиридонова', 'варвара')."""
    words = _norm_name(full_name).split(' ')
    surname = words[0] if words else ''
    first = words[1] if len(words) > 1 else ''
    return surname, first


def _to_number(raw):
    s = (str(raw) if raw is not None else '').strip().replace(',', '.').replace(' ', '')
    if not s:
        return 0.0
    try:
        value = float(s)
    except ValueError:
        return 0.0
    return int(value) if value.is_integer() else value


def _parse_ratings_csv(text):
    """CSV → (rows, columns). rows: dict(name, year, city, team, values)."""
    first_line = text.split('\n', 1)[0]
    delimiter = ';' if first_line.count(';') >= first_line.count(',') else ','
    rows = list(_csv.reader(io.StringIO(text), delimiter=delimiter))
    if len(rows) < 2:
        return [], []

    header = [(h or '').strip() for h in rows[0]]
    norm = [h.lower().replace('ё', 'е') for h in header]

    def find(*names):
        for i, h in enumerate(norm):
            for n in names:
                if n in h:
                    return i
        return None

    i_fio = find('фио', 'фамилия', 'name')
    i_year = find('год рождения', 'год')
    i_city = find('город', 'city')
    i_team = find('клуб', 'команд', 'team', 'club')
    used = {i for i in (i_fio, i_year, i_city, i_team) if i is not None}
    if i_fio is None:
        i_fio = 0
        used.add(0)
    col_indexes = [(h, i) for i, h in enumerate(header) if i not in used and h]
    columns = [name for name, _idx in col_indexes]

    parsed = []
    for r in rows[1:]:
        if not r or len(r) <= i_fio:
            continue
        full_name = (r[i_fio] or '').strip()
        if len(full_name) < 3:
            continue
        year = _parse_year(r[i_year]) if i_year is not None and len(r) > i_year else None
        city = (r[i_city] or '').strip() if i_city is not None and len(r) > i_city else None
        team = (r[i_team] or '').strip() if i_team is not None and len(r) > i_team else None
        values = {}
        for name, idx in col_indexes:
            values[name] = _to_number(r[idx]) if idx < len(r) else 0.0
        surname, first = _split_name(full_name)
        parsed.append({'full_name': full_name, 'surname_key': surname, 'first_key': first,
                       'birth_year': year, 'city': city, 'team': team, 'values': values})
    return parsed, columns


def _ratings_refresh():
    """Скачивает CSV и полностью перезаливает кэш. Возвращает (count, columns, error)."""
    import urllib.request
    try:
        req = urllib.request.Request(RATINGS_URL, headers={'User-Agent': 'chrono-timer/1.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except Exception as ex:
        return None, [], f'Не удалось скачать рейтинги: {ex}'

    text = None
    for enc in ('utf-8-sig', 'utf-8', 'cp1251'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return None, [], 'Не удалось определить кодировку файла'

    rows, columns = _parse_ratings_csv(text)
    if not rows:
        return None, [], 'В файле рейтингов не найдено ни одного спортсмена'

    conn = _ratings_db()
    try:
        with conn:
            conn.execute('DELETE FROM ratings')
            conn.executemany(
                'INSERT INTO ratings (full_name, surname_key, first_key, birth_year, city, team, values_json) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                [(r['full_name'], r['surname_key'], r['first_key'], r['birth_year'],
                  r['city'], r['team'], json.dumps(r['values'], ensure_ascii=False)) for r in rows])
            now = datetime.now().isoformat(timespec='seconds')
            meta = (('columns', json.dumps(columns, ensure_ascii=False)),
                    ('updated_at', now), ('count', str(len(rows))), ('source', RATINGS_URL))
            for key, value in meta:
                conn.execute('INSERT INTO meta (key, value) VALUES (?, ?) '
                             'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, value))
    finally:
        conn.close()
    _app_logger.info('Ratings refreshed: %s records, %s columns', len(rows), len(columns))
    return len(rows), columns, None


def _ratings_meta():
    try:
        conn = _ratings_db()
    except Exception:
        return {}
    try:
        return {r['key']: r['value'] for r in conn.execute('SELECT key, value FROM meta')}
    finally:
        conn.close()


def _ratings_stats():
    """(count, updated_at, columns)."""
    meta = _ratings_meta()
    try:
        columns = json.loads(meta.get('columns') or '[]')
    except ValueError:
        columns = []
    try:
        count = int(meta.get('count') or 0)
    except ValueError:
        count = 0
    return count, meta.get('updated_at'), columns


def _ratings_columns():
    return _ratings_stats()[2]


class RatingsIndex(object):
    """Поиск рейтинга: ФИО без отчества + год рождения."""

    def __init__(self, items):
        self.by_full = {}
        self.by_surname = {}
        for item in items:
            surname = item.get('surname_key') or item.get('surname') or ''
            first = item.get('first_key') or item.get('first') or ''
            year = item.get('birth_year', item.get('year'))
            row = dict(item)
            row['surname_key'], row['first_key'], row['birth_year'] = surname, first, year
            key_full = (surname, first, year)
            self.by_full.setdefault(key_full, []).append(row)
            self.by_surname.setdefault((surname, year), []).append(row)

    def find(self, full_name, birth_year):
        surname, first = _split_name(full_name)
        if not surname or birth_year is None:
            return None
        matches = self.by_full.get((surname, first, birth_year))
        if matches:
            return matches[0] if len(matches) == 1 else None
        matches = self.by_surname.get((surname, birth_year))
        if matches:
            return matches[0] if len(matches) == 1 else None
        return None

    def value(self, full_name, birth_year, column):
        if not column:
            return 0
        item = self.find(full_name, birth_year)
        if not item:
            return 0
        try:
            return item['values'].get(column) or 0
        except (AttributeError, TypeError):
            return 0


def _ratings_index():
    try:
        conn = _ratings_db()
    except Exception:
        return RatingsIndex([])
    try:
        rows = conn.execute(
            'SELECT surname_key, first_key, birth_year, values_json FROM ratings').fetchall()
    finally:
        conn.close()
    items = []
    for r in rows:
        try:
            values = json.loads(r['values_json'])
        except ValueError:
            values = {}
        items.append({'surname': r['surname_key'], 'first': r['first_key'],
                      'year': r['birth_year'], 'values': values})
    return RatingsIndex(items)


def _rating_for(index, full_name, birth_year, column):
    return index.value(full_name, birth_year, column) if index else 0


def _ratings_auto_refresh():
    """В фоне обновляет рейтинги, если кэш старше суток."""
    try:
        count, updated, _cols = _ratings_stats()
        if count and updated:
            age = datetime.now() - datetime.fromisoformat(updated)
            if age < timedelta(hours=RATINGS_TTL_HOURS):
                return
    except Exception:
        pass

    def _run():
        try:
            _ratings_refresh()
        except Exception:
            _app_logger.exception('Ratings auto refresh failed')

    threading.Thread(target=_run, daemon=True).start()
