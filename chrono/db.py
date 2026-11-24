# -*- coding: utf-8 -*-
"""Базы соревнований: соединения, схема, реестр competitions.json."""
import json
import logging
import os
from pathlib import Path
import sqlite3
from datetime import datetime

from flask import has_request_context, session
from werkzeug.security import generate_password_hash

from chrono.utils import _format_age_range, _parse_age_range, _slugify, _unique_slug

log = logging.getLogger('chrono')


# корень проекта: chrono/db.py → chrono/ → корень
BASE_DIR = str(Path(__file__).resolve().parents[1])

DB_PATH = os.environ.get('CHRONO_DB') or os.path.join(BASE_DIR, 'chrono.db')

LOG_DIR = os.environ.get('CHRONO_LOG_DIR') or os.path.join(BASE_DIR, 'log')

CERT_DIR = os.environ.get('CHRONO_CERT_DIR') or os.path.join(BASE_DIR, 'cert')

DATA_DIR = os.path.dirname(os.path.abspath(DB_PATH))

REGISTRY_PATH = os.environ.get('CHRONO_REGISTRY') or os.path.join(DATA_DIR, 'competitions.json')

COMPS_DIR = os.path.join(DATA_DIR, 'comps')

BACKUP_DIR = os.path.join(DATA_DIR, 'backups')

def _registry_load():
    try:
        with open(REGISTRY_PATH, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {'competitions': []}
    if not isinstance(data, dict):
        return {'competitions': []}
    data.setdefault('competitions', [])
    return _registry_norm(data)

def _registry_save(reg):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = REGISTRY_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REGISTRY_PATH)

def _registry_find(slug):
    if not slug:
        return None
    for c in _registry_load().get('competitions', []):
        if c.get('slug') == slug:
            return c
    return None

def _comp_db_path(entry):
    path = (entry or {}).get('db') or ''
    # в реестре пути хранятся с «/» — чтобы база переезжала между Windows и Linux
    path = path.replace('\\', '/')
    return path if os.path.isabs(path) else os.path.join(DATA_DIR, path)

def _rel_db_path(path):
    return os.path.relpath(path, DATA_DIR).replace('\\', '/')

def _registry_norm(reg):
    """Проставляет флаги, появившиеся позже (архив, скрытое, только чтение)."""
    for c in reg.get('competitions', []):
        c.setdefault('archived', False)
        c.setdefault('hidden', False)
        c.setdefault('read_only', False)
        c.setdefault('created', '')
    return reg

def _ensure_registry():
    """Первый запуск: существующая база регистрируется как первое соревнование."""
    if os.path.exists(REGISTRY_PATH):
        reg = _registry_load()
        _migrate_competitions()
        return _registry_norm(reg)
    name = 'Соревнование'
    if os.path.exists(DB_PATH):
        try:
            probe = sqlite3.connect(DB_PATH)
            probe.row_factory = sqlite3.Row
            row = probe.execute('SELECT name FROM competitions ORDER BY id LIMIT 1').fetchone()
            if row and row['name']:
                name = row['name']
            probe.close()
        except Exception:
            pass
    reg = {'competitions': []}
    reg['competitions'].append({
        'slug': _unique_slug(_slugify(name), reg),
        'name': name,
        'db': _rel_db_path(DB_PATH),
        'created': datetime.now().isoformat(timespec='seconds'),
        'read_only': False,
    })
    _registry_save(reg)
    return reg

def _active_db_path():
    """Файл базы текущего соревнования (из сессии), иначе — база по умолчанию."""
    slug = None
    if has_request_context():
        slug = session.get('comp')
    if slug:
        entry = _registry_find(slug)
        if entry:
            return _comp_db_path(entry)
    return DB_PATH

def _create_competition_db(path, name, admin_login, admin_password, fmt='mass'):
    """Создаёт пустую базу соревнования с заданным администратором и форматом."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    init_db(path)
    conn = _connect(path)
    try:
        with conn:
            row = conn.execute('SELECT id FROM competitions ORDER BY id LIMIT 1').fetchone()
            if row:
                conn.execute('UPDATE competitions SET name = ?, format = ? WHERE id = ?',
                             (name, fmt, row['id']))
            else:
                conn.execute('INSERT INTO competitions (name, format) VALUES (?, ?)', (name, fmt))
            conn.execute('DELETE FROM users')
            conn.execute(
                'INSERT INTO users (username, password_hash, is_admin, role, created_at) VALUES (?, ?, 1, ?, ?)',
                (admin_login, generate_password_hash(admin_password), 'admin', datetime.now().isoformat()))
    finally:
        conn.close()

def _create_timing_log(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS timing_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        competition_id INTEGER,
        heat_id INTEGER,
        race_id INTEGER,
        participant_id INTEGER,
        source TEXT NOT NULL,
        action TEXT NOT NULL,
        ok INTEGER NOT NULL DEFAULT 0,
        reason TEXT,
        number TEXT,
        full_name TEXT,
        user_login TEXT,
        detail TEXT
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_timing_log_created ON timing_log (created_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_timing_log_comp ON timing_log (competition_id)')

def _migrate_schema(conn):
    """Добавляет колонки, появившиеся в новых версиях схемы."""
    comp_cols = {r['name'] for r in conn.execute('PRAGMA table_info(competitions)').fetchall()}
    if 'format' not in comp_cols:
        conn.execute("ALTER TABLE competitions ADD COLUMN format TEXT NOT NULL DEFAULT 'mass'")
    cat_cols = {r['name'] for r in conn.execute('PRAGMA table_info(categories)').fetchall()}
    if 'age_range' not in cat_cols:
        conn.execute('ALTER TABLE categories ADD COLUMN age_range TEXT')
    heat_cols = {r['name'] for r in conn.execute('PRAGMA table_info(heats)').fetchall()}
    if 'start_dir' not in heat_cols:
        conn.execute("ALTER TABLE heats ADD COLUMN start_dir TEXT NOT NULL DEFAULT 'asc'")


def _migrate_age_ranges(conn):
    """Заполняет/исправляет age_range у категорий и подставляет дату старта."""
    rows = conn.execute(
        'SELECT id, year_from, year_to, age_range FROM categories').fetchall()
    todo = []
    for r in rows:
        _age_min, _age_max, err = _parse_age_range(r['age_range'])
        if (r['age_range'] or '').strip() and not err:
            continue
        if r['year_from'] is None and r['year_to'] is None:
            continue
        todo.append(r)
    if not todo:
        return
    row = conn.execute("SELECT value FROM settings WHERE key = 'start_date'").fetchone()
    start = ((row['value'] if row else '') or '').strip()
    if not start:
        start = datetime.now().strftime('%Y-%m-%d')
        conn.execute("INSERT INTO settings (key, value) VALUES ('start_date', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (start,))
    start_year = int(start[:4]) if start[:4].isdigit() else datetime.now().year
    for r in todo:
        if r['year_from'] is None and r['year_to'] is None:
            # некорректная строка без годов — сбрасываем (условий по возрасту не остаётся)
            conn.execute('UPDATE categories SET age_range = ? WHERE id = ?', ('', r['id']))
            continue
        age_min = (start_year - r['year_to']) if r['year_to'] is not None else None
        age_max = (start_year - r['year_from']) if r['year_from'] is not None else None
        # 1900 у старых категорий означал «без верхней границы»
        if r['year_from'] is not None and r['year_from'] <= 1900:
            age_max = None
        if r['year_to'] is not None and r['year_to'] >= start_year + 1:
            age_min = None
        if age_min is not None:
            age_min = max(0, age_min)
        if age_max is not None and age_max > 120:
            age_max = None
        if age_min is not None and age_min > 120:
            age_min = 0
        if age_min is not None and age_max is not None and age_min > age_max:
            age_min, age_max = age_max, age_min
        conn.execute('UPDATE categories SET age_range = ? WHERE id = ?',
                     (_format_age_range(age_min, age_max), r['id']))


def _connect(path):
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn

def get_db():
    """Соединение с базой текущего соревнования."""
    return _connect(_active_db_path())

def init_db(path=None):
    from chrono.settings import (AUTH_PASSWORD, AUTH_USERNAME, COMPETITION_DEFAULTS,
                                 ITT_DEFAULTS, PROTOCOL_DEFAULTS, RFID_DEFAULTS,
                                 _reload_settings_cache_from_db)
    conn = _connect(path or DB_PATH)
    try:
        conn.execute('PRAGMA journal_mode = WAL')
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL DEFAULT 'judge',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS competitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                format TEXT NOT NULL DEFAULT 'mass'
            );
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, competition_id INTEGER NOT NULL, name TEXT NOT NULL,
                laps_total INTEGER NOT NULL DEFAULT 1, gender TEXT, year_from INTEGER, year_to INTEGER,
                sort_order INTEGER, lap_length TEXT, route_length TEXT, hd TEXT, ms TEXT, ts TEXT,
                FOREIGN KEY (competition_id) REFERENCES competitions(id) ON DELETE CASCADE,
                UNIQUE (competition_id, name)
            );
            CREATE TABLE IF NOT EXISTS participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT, competition_id INTEGER NOT NULL, number INTEGER,
                full_name TEXT NOT NULL, birth_year INTEGER, category_id INTEGER, gender TEXT,
                city TEXT, team TEXT, phone TEXT, consent_at TEXT, reg_source TEXT,
                FOREIGN KEY (competition_id) REFERENCES competitions(id) ON DELETE CASCADE,
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE RESTRICT, UNIQUE (competition_id, number)
            );
            CREATE TABLE IF NOT EXISTS registrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                competition_id INTEGER NOT NULL,
                participant_id INTEGER,
                full_name TEXT,
                birth_year INTEGER,
                gender TEXT,
                city TEXT,
                team TEXT,
                phone TEXT,
                category_id INTEGER,
                category_name TEXT,
                consent_at TEXT,
                created_at TEXT NOT NULL,
                ip TEXT,
                user_agent TEXT,
                browser TEXT,
                os TEXT,
                device TEXT,
                accept_language TEXT,
                referer TEXT,
                raw_json TEXT,
                FOREIGN KEY (competition_id) REFERENCES competitions(id) ON DELETE CASCADE,
                FOREIGN KEY (participant_id) REFERENCES participants(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reg_comp ON registrations(competition_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_reg_pid ON registrations(participant_id);
            CREATE TABLE IF NOT EXISTS bib_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                competition_id INTEGER NOT NULL,
                number INTEGER NOT NULL,
                tag TEXT NOT NULL,
                slot INTEGER NOT NULL DEFAULT 1,
                created_at TEXT,
                FOREIGN KEY (competition_id) REFERENCES competitions(id) ON DELETE CASCADE,
                UNIQUE (competition_id, number, slot),
                UNIQUE (competition_id, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_bib_tags_number ON bib_tags(competition_id, number);
            CREATE TABLE IF NOT EXISTS heats (
                id INTEGER PRIMARY KEY AUTOINCREMENT, competition_id INTEGER NOT NULL, heat_number INTEGER NOT NULL,
                name TEXT, finish_on_leader INTEGER NOT NULL DEFAULT 0, min_lap_time REAL NOT NULL DEFAULT 5,
                auto_enabled INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
                started_at TEXT, finished_at TEXT, format TEXT NOT NULL DEFAULT 'mass',
                start_order TEXT NOT NULL DEFAULT 'number',
                start_at TEXT, startlist_token TEXT,
                FOREIGN KEY (competition_id) REFERENCES competitions(id) ON DELETE CASCADE,
                UNIQUE (competition_id, heat_number)
            );
            CREATE TABLE IF NOT EXISTS heat_categories (
                heat_id INTEGER NOT NULL, category_id INTEGER NOT NULL, PRIMARY KEY (heat_id, category_id),
                FOREIGN KEY (heat_id) REFERENCES heats(id) ON DELETE CASCADE, FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS heat_participants (
                heat_id INTEGER NOT NULL, participant_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                sort_order INTEGER,
                PRIMARY KEY (heat_id, participant_id), FOREIGN KEY (heat_id) REFERENCES heats(id) ON DELETE CASCADE,
                FOREIGN KEY (participant_id) REFERENCES participants(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS races (
                id INTEGER PRIMARY KEY AUTOINCREMENT, participant_id INTEGER NOT NULL, competition_id INTEGER NOT NULL,
                heat_id INTEGER, laps_total INTEGER NOT NULL DEFAULT 1, planned_laps INTEGER,
                start_time TEXT NOT NULL, finish_time TEXT,
                status TEXT NOT NULL DEFAULT 'running', laps_behind INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (participant_id) REFERENCES participants(id) ON DELETE CASCADE,
                FOREIGN KEY (competition_id) REFERENCES competitions(id) ON DELETE CASCADE, FOREIGN KEY (heat_id) REFERENCES heats(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS laps (
                id INTEGER PRIMARY KEY AUTOINCREMENT, race_id INTEGER NOT NULL, lap_number INTEGER NOT NULL,
                lap_time TEXT NOT NULL, user_id INTEGER, user_login TEXT,
                FOREIGN KEY (race_id) REFERENCES races(id) ON DELETE CASCADE, UNIQUE (race_id, lap_number)
            );
            CREATE TABLE IF NOT EXISTS auto_laps (
                id INTEGER PRIMARY KEY AUTOINCREMENT, race_id INTEGER NOT NULL, lap_number INTEGER NOT NULL,
                scan_time TEXT NOT NULL, rfid_tag TEXT NOT NULL, FOREIGN KEY (race_id) REFERENCES races(id) ON DELETE CASCADE, UNIQUE (race_id, lap_number)
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        _create_timing_log(conn)

        _migrate_schema(conn)

        for k, v in ITT_DEFAULTS.items():
            conn.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (k, v))
        for k, v in RFID_DEFAULTS.items():
            conn.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (k, v))
        for k, v in PROTOCOL_DEFAULTS.items():
            conn.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (k, v))
        for k, v in COMPETITION_DEFAULTS.items():
            conn.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (k, v))

        _migrate_age_ranges(conn)

        if not conn.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            conn.execute(
                'INSERT INTO users (username, password_hash, is_admin, role, created_at) VALUES (?, ?, 1, ?, ?)',
                (AUTH_USERNAME, generate_password_hash(AUTH_PASSWORD), 'admin', datetime.now().isoformat())
            )
            print(f'👤 Создан администратор по умолчанию: {AUTH_USERNAME}', flush=True)

        conn.executescript('''
            CREATE INDEX IF NOT EXISTS idx_participants_competition ON participants(competition_id);
            CREATE INDEX IF NOT EXISTS idx_categories_competition   ON categories(competition_id);
            CREATE INDEX IF NOT EXISTS idx_heats_competition        ON heats(competition_id);
            CREATE INDEX IF NOT EXISTS idx_races_competition        ON races(competition_id);
            CREATE INDEX IF NOT EXISTS idx_races_participant        ON races(participant_id);
            CREATE INDEX IF NOT EXISTS idx_races_heat               ON races(heat_id);
            CREATE INDEX IF NOT EXISTS idx_laps_race                ON laps(race_id);
            CREATE INDEX IF NOT EXISTS idx_auto_laps_race           ON auto_laps(race_id);
            CREATE INDEX IF NOT EXISTS idx_laps_race_lap            ON laps(race_id, lap_number);
            CREATE INDEX IF NOT EXISTS idx_participants_comp_number ON participants(competition_id, number);
            CREATE INDEX IF NOT EXISTS idx_races_participant_status ON races(participant_id, status);
            CREATE INDEX IF NOT EXISTS idx_races_heat_status        ON races(heat_id, status);
        ''')

        conn.commit()
        _reload_settings_cache_from_db(conn, path or DB_PATH)
        _get_competition(conn)   # в базе всегда есть одно соревнование
    finally:
        conn.close()

_competitions_migrated = False

def _sync_registry_format(entry, path):
    """Пишет формат соревнования в реестр (для бейджа на странице входа)."""
    try:
        conn = _connect(path)
        try:
            row = conn.execute('SELECT format FROM competitions ORDER BY id LIMIT 1').fetchone()
        finally:
            conn.close()
    except Exception:
        return False
    if not row:
        return False
    fmt = row['format'] if row['format'] in ('mass', 'itt') else 'mass'
    if entry.get('format') != fmt:
        entry['format'] = fmt
        return True
    return False

def _migrate_competitions():
    """Обновляет схему во всех базах соревнований (один раз за процесс)."""
    global _competitions_migrated
    if _competitions_migrated:
        return
    _competitions_migrated = True
    try:
        reg = _registry_load()
    except Exception:
        return
    changed = False
    for c in reg.get('competitions', []):
        path = _comp_db_path(c)
        if not os.path.exists(path):
            continue
        try:
            init_db(path)
            if _sync_registry_format(c, path):
                changed = True
        except Exception:
            try:
                log.exception('Миграция базы не удалась: %s', path)
            except Exception:
                pass
    if changed:
        try:
            _registry_save(reg)
        except Exception:
            try:
                log.exception('Не удалось сохранить реестр после миграции')
            except Exception:
                pass

DEFAULT_COMPETITION_NAME = os.environ.get('CHRONO_COMPETITION', 'Соревнование')

def _get_competition(conn):
    """Единственное соревнование в базе. Создаётся автоматически при первом запуске."""
    comp = conn.execute('SELECT * FROM competitions ORDER BY id LIMIT 1').fetchone()
    if comp: return comp
    with conn:
        cid = conn.execute('INSERT INTO competitions (name) VALUES (?)', (DEFAULT_COMPETITION_NAME,)).lastrowid
    return conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()

PEOPLE_DB = os.path.join(DATA_DIR, 'people.db')
