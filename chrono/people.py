# -*- coding: utf-8 -*-
"""Кэш участников bike.cedor.ru — подсказки при вводе ФИО."""
from datetime import datetime, timedelta
import io, os, re, threading
from chrono.db import (PEOPLE_DB, _connect)
from chrono.logging_setup import (_app_logger)
from chrono.utils import (_normalize_gender, _parse_year)

PEOPLE_URL = os.environ.get('CHRONO_PEOPLE_URL', 'https://bike.cedor.ru/participants.csv')

PEOPLE_TTL_HOURS = 24

def _people_db():
    conn = _connect(PEOPLE_DB)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            name_key TEXT NOT NULL,
            birth_year INTEGER,
            city TEXT,
            team TEXT,
            gender TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_people_key ON people(name_key);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    return conn

def _norm_person(s):
    return re.sub(r'\s+', ' ', (s or '').strip().lower().replace('ё', 'е'))

def _parse_people_csv(text):
    """Разбирает CSV вида «ФИО;Год рождения;Город;Клуб»."""
    import csv as _csv
    first_line = text.split('\n', 1)[0]
    delimiter = ';' if first_line.count(';') >= first_line.count(',') else ','
    rows = list(_csv.reader(io.StringIO(text), delimiter=delimiter))
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]

    def find(*names):
        for i, h in enumerate(header):
            for n in names:
                if n in h:
                    return i
        return None

    i_fio = find('фио', 'фамилия', 'name')
    i_year = find('год рождения', 'год')
    i_city = find('город', 'city')
    i_team = find('клуб', 'команд', 'team', 'club')
    i_gender = find('пол', 'gender', 'sex')
    if i_fio is None:
        i_fio, i_year, i_city, i_team = 0, 1, 2, 3

    out = []
    for r in rows[1:]:
        if not r or len(r) <= i_fio:
            continue
        fio = (r[i_fio] or '').strip()
        if len(fio) < 3:
            continue
        year = _parse_year(r[i_year]) if i_year is not None and len(r) > i_year else None
        city = (r[i_city] or '').strip() if i_city is not None and len(r) > i_city else None
        team = (r[i_team] or '').strip() if i_team is not None and len(r) > i_team else None
        gender = _normalize_gender(r[i_gender]) if i_gender is not None and len(r) > i_gender else None
        # «Лично» в колонке города — это не город, а отсутствие клуба
        if city and city.strip().lower() == 'лично':
            city = None
        # в общем списке клуб всегда заполнен: пусто → «Лично»
        out.append((fio, _norm_person(fio), year, city or None, team or 'Лично', gender))
    return out

def _people_refresh():
    """Скачивает CSV и полностью перезаливает список. Возвращает (count, error)."""
    import urllib.request
    try:
        req = urllib.request.Request(PEOPLE_URL, headers={'User-Agent': 'chrono-timer/1.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except Exception as ex:
        return None, f'Не удалось скачать список: {ex}'

    text = None
    for enc in ('utf-8-sig', 'utf-8', 'cp1251'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return None, 'Не удалось определить кодировку файла'

    rows = _parse_people_csv(text)
    if not rows:
        return None, 'В файле не найдено ни одного участника'

    conn = _people_db()
    try:
        with conn:
            conn.execute('DELETE FROM people')
            conn.executemany(
                'INSERT INTO people (full_name, name_key, birth_year, city, team, gender) '
                'VALUES (?, ?, ?, ?, ?, ?)', rows)
            now = datetime.now().isoformat(timespec='seconds')
            conn.execute("INSERT INTO meta (key, value) VALUES ('updated_at', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (now,))
            conn.execute("INSERT INTO meta (key, value) VALUES ('count', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(len(rows)),))
    finally:
        conn.close()
    _app_logger.info('People list refreshed: %s records', len(rows))
    return len(rows), None

def _people_stats():
    try:
        conn = _people_db()
    except Exception:
        return 0, None
    try:
        meta = {r['key']: r['value'] for r in conn.execute('SELECT key, value FROM meta')}
        count = conn.execute('SELECT COUNT(*) FROM people').fetchone()[0]
        return count, meta.get('updated_at')
    finally:
        conn.close()

def _people_search(query, limit=20):
    q = _norm_person(query)
    if len(q) < 2:
        return []
    words = q.split(' ')[:4]
    sql = 'SELECT id, full_name, birth_year, city, team, gender FROM people WHERE 1=1'
    params = []
    for w in words:
        sql += ' AND (name_key LIKE ? OR name_key LIKE ?)'
        params += [w + '%', '% ' + w + '%']
    sql += ' ORDER BY full_name COLLATE NOCASE LIMIT ?'
    params.append(limit)
    conn = _people_db()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()

def _people_auto_refresh():
    """В фоне обновляет список, если кэш старше суток (ошибки не влияют на работу)."""
    try:
        count, updated = _people_stats()
        if count and updated:
            age = datetime.now() - datetime.fromisoformat(updated)
            if age < timedelta(hours=PEOPLE_TTL_HOURS):
                return
    except Exception:
        pass

    def _run():
        try:
            _people_refresh()
        except Exception:
            _app_logger.exception('People auto refresh failed')

    threading.Thread(target=_run, daemon=True).start()
