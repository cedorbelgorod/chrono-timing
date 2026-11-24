# -*- coding: utf-8 -*-
"""Настройки соревнований: кэш по базам, форматы, интервалы."""
import os
import threading
from datetime import datetime

from chrono.db import _active_db_path, _connect


AUTH_USERNAME = os.environ.get('CHRONO_USER', 'admin')

AUTH_PASSWORD = os.environ.get('CHRONO_PASS', 'admin')

_settings_lock = threading.Lock()

_settings_cache = {}

RFID_DEFAULTS = {
    'swrfid_enabled': '0',
    'swrfid_port': 'COM3',
    'swrfid_baud': '115200',
}

COMPETITION_DEFAULTS = {
    'start_date': '',
}

PROTOCOL_DEFAULTS = {
    'proto_title': '',
    'proto_venue': '',
    'proto_date': '',
    'proto_start_time': '',
    'proto_chief_judge': '',
    'proto_start_judge': '',
    'proto_finish_judge': '',
    'proto_chief_secretary': '',
    'proto_track_name': '',
    'proto_track_reg': '',
    'proto_route_length': '',
    'proto_weather_start': '',
    'proto_weather_end': '',
    'proto_temp_start': '',
    'proto_temp_end': '',
    'proto_humidity': '',
    'proto_weather_city': '',
    'proto_weather_lat': '',
    'proto_weather_lon': '',
}

RACE_FORMATS = {
    'mass': 'Групповой старт',
    'itt': 'Раздельный старт',
}

ITT_DEFAULTS = {
    'itt_start_interval': '30',
    'itt_countdown_sound': '1',
}

def _comp_format(comp):
    """Формат соревнования: 'mass' (по умолчанию) или 'itt'."""
    if comp is None:
        return 'mass'
    try:
        value = comp['format']
    except (KeyError, IndexError):
        return 'mass'
    return value if value in RACE_FORMATS else 'mass'

def _competition_start_year():
    """Год плановой даты старта; если дата не задана — текущий год."""
    value = (get_cached_setting('start_date', '') or '').strip()
    if len(value) >= 4 and value[:4].isdigit():
        return int(value[:4])
    return datetime.now().year

def _itt_interval():
    """Интервал старта в разделке, секунды (минимум 5)."""
    try:
        value = int(float(get_cached_setting('itt_start_interval', '30') or 30))
    except (TypeError, ValueError):
        value = 30
    return max(5, value)

def _reload_settings_cache_from_db(conn, path=None):
    """Загружает настройки в кэш для конкретной базы (у каждого соревнования свой)."""
    key = path or _active_db_path()
    rows = conn.execute('SELECT key, value FROM settings').fetchall()
    with _settings_lock:
        _settings_cache[key] = {r['key']: r['value'] for r in rows}

def get_cached_setting(key, default=None):
    path = _active_db_path()
    with _settings_lock:
        data = _settings_cache.get(path)
    if data is None:
        conn = _connect(path)
        try:
            _reload_settings_cache_from_db(conn, path)
        finally:
            conn.close()
        with _settings_lock:
            data = _settings_cache.get(path, {})
    return data.get(key, default)

def set_cached_setting(conn, key, value):
    conn.execute('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, str(value)))
    path = _active_db_path()
    with _settings_lock:
        _settings_cache.setdefault(path, {})[key] = str(value)

def _heat_format(heat):
    """Формат старта: 'mass' или 'itt' (у старых стартов поля нет — считаем массовым)."""
    if heat is None:
        return 'mass'
    try:
        value = heat['format']
    except (KeyError, IndexError):
        return 'mass'
    return value if value in RACE_FORMATS else 'mass'
