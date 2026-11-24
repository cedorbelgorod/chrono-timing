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

RATINGS_URL = os.environ.get('CHRONO_RATINGS_URL', 'https://bike.cedor.ru/ratings-all.json')

RATINGS_TTL_HOURS = 24

DEFAULT_RATING_COLUMN = 'overall-2026'

LEGACY_RATING_COLUMNS = {
    'мтб 2026': 'mtb-2026', 'шоссе 2026': 'road-2026', 'другое 2026': 'other-2026',
    'все 2026': 'overall-2026', 'мтб все время': 'mtb-all', 'шоссе все время': 'road-all',
    'другое все время': 'other-all', 'все все время': 'overall-all',
}


def _rating_column_id(value):
    """Имя столбца → стабильный id рейтинга (старые значения из CSV-версии тоже)."""
    raw = (value or '').strip()
    return LEGACY_RATING_COLUMNS.get(raw.lower().replace('ё', 'е'), raw)


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


def _parse_ratings_json(text):
    """JSON bike.cedor.ru → (rows, columns).

    columns: [{'id', 'label', 'athletes'}]; rows: name/year/city/club/values по id.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return [], []
    if not isinstance(data, dict):
        return [], []
    columns = []
    for item in data.get('ratings') or []:
        rid = (item.get('id') or '').strip() if isinstance(item, dict) else ''
        if not rid:
            continue
        columns.append({
            'id': rid,
            'label': (item.get('label') or rid),
            'athletes': item.get('athletes') or 0,
        })
    parsed = []
    for a in data.get('athletes') or []:
        if not isinstance(a, dict):
            continue
        full_name = (a.get('name') or '').strip()
        if len(full_name) < 3:
            continue
        values = {}
        for rid, item in (a.get('ratings') or {}).items():
            if isinstance(item, dict):
                values[rid] = _to_number(item.get('points'))
            else:
                values[rid] = _to_number(item)
        surname, first = _split_name(full_name)
        parsed.append({'full_name': full_name, 'surname_key': surname, 'first_key': first,
                       'birth_year': _parse_year(a.get('birth_year')),
                       'city': (a.get('city') or '').strip() or None,
                       'team': (a.get('club') or '').strip() or None,
                       'values': values})
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

    rows, columns = _parse_ratings_json(text)
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
    """[{'id', 'label'}] — столбцы рейтингов из файла (старые значения-строки тоже)."""
    out = []
    for item in _ratings_stats()[2]:
        if isinstance(item, dict):
            rid = (item.get('id') or '').strip()
            if rid:
                out.append({'id': rid, 'label': item.get('label') or rid,
                            'athletes': item.get('athletes') or 0})
        elif isinstance(item, str) and item.strip():
            rid = _rating_column_id(item)
            out.append({'id': rid, 'label': item.strip(), 'athletes': 0})
    return out


def _column_label(column_id):
    for item in _ratings_columns():
        if item['id'] == column_id:
            return item['label']
    return column_id


def _active_rating_column():
    """Текущий выбранный рейтинг (id) или '' — если не выбран/не найден в файле."""
    try:
        from chrono.settings import get_cached_setting
        value = _rating_column_id(get_cached_setting('rating_column', '') or '')
    except Exception:
        return ''
    ids = {item['id'] for item in _ratings_columns()}
    return value if value in ids else ''


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
        # старый кэш (CSV) хранил значения по подписи столбца — приводим к id
        values = {_rating_column_id(k): v for k, v in values.items()}
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
