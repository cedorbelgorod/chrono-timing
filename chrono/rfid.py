# -*- coding: utf-8 -*-
"""RFID: чтение меток, автоотметки кругов, статус считывателя."""
from datetime import datetime
import re, threading, time
try:
    from swrfid import RFIDReader
    SWRFID_AVAILABLE = True
except ImportError:
    RFIDReader = None
    SWRFID_AVAILABLE = False
from chrono.catalog import (_participant_by_tag)
from chrono.db import (_db_key, get_db)
from chrono.logging_setup import (_swrfid_logger)
from chrono.manual_helpers import (AUTO_LOG_SIZE, _log_timing)
from chrono.settings import (_reload_settings_cache_from_db, _settings_cache, _settings_lock)
from chrono.timing import (_process_race_finish)

_swrfid_thread = None

_swrfid_stop_event = threading.Event()

_swrfid_status_lock = threading.Lock()

_swrfid_status = {'connected': False, 'last_error': None, 'port': None}

_auto_log_lock = threading.Lock()

_auto_log = []

def _push_auto_log(entry, key=''):
    """Журнал автохронометража: записи помечаются базой соревнования."""
    entry = dict(entry)
    entry['key'] = key
    with _auto_log_lock:
        _auto_log.insert(0, entry)
        del _auto_log[AUTO_LOG_SIZE:]


def _auto_log_for(key):
    """Записи авто-журнала только для указанной базы."""
    with _auto_log_lock:
        return [e for e in _auto_log if e.get('key') == key]

def _process_tag_scan(conn, tag, scan_time=None):
    if scan_time is None: scan_time = datetime.now()
    now_ts = scan_time.strftime('%H:%M:%S')
    tag = re.sub(r'\s+', '', tag or '').upper()
    if not tag:
        _log_timing(conn, 'auto', 'scan', False, reason='Пустая метка')
        return {'ok': False, 'error': 'Пустая метка'}

    log_key = _db_key(conn)

    def alog(entry):
        _push_auto_log(entry, log_key)

    p = _participant_by_tag(conn, tag)
    if not p:
        alog({'ok': False, 'tag': tag, 'message': 'Метка не привязана к номеру', 'time': now_ts})
        _log_timing(conn, 'auto', 'scan', False, reason='Метка не привязана к номеру', detail=f'EPC {tag}')
        return {'ok': False, 'error': 'Метка не привязана к номеру'}

    def log(ok, action, reason=None, detail=None, race=None):
        _log_timing(conn, 'auto', action, ok, reason=reason,
                    competition_id=p['competition_id'],
                    heat_id=race['heat_id'] if race is not None else None,
                    race_id=race['id'] if race is not None else None,
                    participant_id=p['id'], number=p['number'], full_name=p['full_name'],
                    detail=detail)

    race = conn.execute('''
        SELECT r.*, h.auto_enabled, h.min_lap_time, h.heat_number, h.status AS heat_status
        FROM races r JOIN heats h ON h.id = r.heat_id
        WHERE r.participant_id = ? AND r.status != 'dns'
        ORDER BY r.start_time DESC LIMIT 1
    ''', (p['id'],)).fetchone()

    if not race:
        alog({'ok': False, 'tag': tag, 'participant': p['full_name'], 'number': p['number'], 'message': 'Не в стартах', 'time': now_ts})
        log(False, 'scan', reason='Участник не в стартах', detail=f'EPC {tag}')
        return {'ok': False, 'error': f'Участник №{p["number"]} не на дистанции'}

    if race['status'] != 'running':
        alog({'ok': False, 'tag': tag, 'participant': p['full_name'], 'number': p['number'], 'message': 'Заезд завершён', 'time': now_ts})
        log(False, 'scan', reason='Заезд уже завершён', race=race, detail=f'Старт №{race["heat_number"]}')
        return {'ok': False, 'error': 'Заезд завершён'}

    if race['heat_status'] != 'running':
        alog({'ok': False, 'tag': tag, 'participant': p['full_name'], 'number': p['number'], 'message': 'Старт завершён', 'time': now_ts})
        log(False, 'scan', reason='Старт завершён', race=race)
        return {'ok': False, 'error': 'Старт завершён'}

    if not race['auto_enabled']:
        alog({'ok': False, 'tag': tag, 'participant': p['full_name'], 'number': p['number'], 'message': 'Авто выключен', 'time': now_ts})
        log(False, 'scan', reason='Автохронометраж выключен', race=race, detail=f'Старт №{race["heat_number"]}')
        return {'ok': False, 'error': f'Автохронометраж выключен (старт №{race["heat_number"]})'}

    laps_done = conn.execute('SELECT COUNT(*) FROM auto_laps WHERE race_id = ?', (race['id'],)).fetchone()[0]
    if laps_done >= race['laps_total']:
        alog({'ok': False, 'tag': tag, 'participant': p['full_name'], 'number': p['number'], 'message': 'Уже финишировал (авто)', 'time': now_ts})
        log(False, 'scan', reason='Уже финишировал', race=race)
        return {'ok': False, 'error': f'Участник №{p["number"]} уже финишировал (авто)'}

    min_lap = race['min_lap_time'] if race['min_lap_time'] is not None else 0
    last_scan = conn.execute('SELECT scan_time FROM auto_laps WHERE race_id = ? ORDER BY lap_number DESC LIMIT 1', (race['id'],)).fetchone()
    last_dt = datetime.fromisoformat(last_scan['scan_time']) if last_scan else datetime.fromisoformat(race['start_time'])
    elapsed = (scan_time - last_dt).total_seconds()

    if elapsed < min_lap:
        msg = f'Рано ({elapsed:.1f} < {min_lap} с)'
        alog({'ok': False, 'tag': tag, 'participant': p['full_name'], 'number': p['number'], 'message': msg, 'time': now_ts})
        log(False, 'scan', reason='Слишком рано', race=race, detail=f'{elapsed:.1f} с < {min_lap} с')
        return {'ok': False, 'error': msg}

    lap_num = laps_done + 1
    is_finish = lap_num == race['laps_total']

    conn.execute('INSERT INTO auto_laps (race_id, lap_number, scan_time, rfid_tag) VALUES (?, ?, ?, ?)', (race['id'], lap_num, scan_time.isoformat(), tag))

    if is_finish:
        _process_race_finish(conn, race, scan_time.isoformat(), force=False)

    msg = 'ФИНИШ (авто) 🏁' if is_finish else f'Круг {lap_num}/{race["laps_total"]} (авто)'
    alog({'ok': True, 'tag': tag, 'participant': p['full_name'], 'number': p['number'], 'message': msg, 'time': now_ts})
    log(True, 'finish' if is_finish else 'lap', race=race,
        detail=f'Круг {lap_num}/{race["laps_total"]} · EPC {tag}')

    return {
        'ok': True, 'number': p['number'], 'full_name': p['full_name'],
        'lap': lap_num, 'laps_total': race['laps_total'], 'is_finish': is_finish,
        'heat_number': race['heat_number'],
        'message': msg
    }

def _swrfid_listener():
    while not _swrfid_stop_event.is_set():
        try:
            with _settings_lock:
                enabled = _settings_cache.get('swrfid_enabled', '0') == '1'
                port = _settings_cache.get('swrfid_port', 'COM3')

            if not enabled:
                with _swrfid_status_lock:
                    _swrfid_status['connected'] = False
                    _swrfid_status['last_error'] = None
                _swrfid_stop_event.wait(2)
                continue

            if not SWRFID_AVAILABLE:
                with _swrfid_status_lock:
                    _swrfid_status['connected'] = False
                    _swrfid_status['last_error'] = 'Библиотека swrfid не установлена'
                _swrfid_stop_event.wait(5)
                continue

            reader = None
            try:
                reader = RFIDReader(port)
                reader.open()
                with _swrfid_status_lock:
                    _swrfid_status['connected'] = True
                    _swrfid_status['last_error'] = None
                    _swrfid_status['port'] = port
                _swrfid_logger.info('SWRFID connected on %s', port)
            except Exception as e:
                with _swrfid_status_lock:
                    _swrfid_status['connected'] = False
                    _swrfid_status['last_error'] = str(e)
                _swrfid_logger.error('SWRFID connect error: %s', e)
                if reader:
                    try: reader.close()
                    except: pass
                _swrfid_stop_event.wait(5)
                continue

            def on_tags(dev_sn, tags):
                if _swrfid_stop_event.is_set(): return
                conn = get_db()
                try:
                    for tag in tags:
                        epc = tag.epc_hex.upper()
                        with conn: _process_tag_scan(conn, epc, datetime.now())
                except Exception as e: _swrfid_logger.error('SWRFID DB error: %s', e)
                finally: conn.close()

            try:
                reader.start_active_listener(on_tags, dedup_window=2.0, rssi_min=0x30)
                reader.start_read()
            except Exception as e:
                with _swrfid_status_lock: _swrfid_status['connected'] = False; _swrfid_status['last_error'] = str(e)
                _swrfid_logger.error('SWRFID start_read error: %s', e)
                try: reader.close()
                except: pass
                _swrfid_stop_event.wait(5)
                continue

            while not _swrfid_stop_event.is_set():
                with _settings_lock:
                    if _settings_cache.get('swrfid_enabled', '0') != '1':
                        break
                time.sleep(0.5)

            try:
                reader.stop_read()
                reader.close()
            except: pass

            with _swrfid_status_lock:
                _swrfid_status['connected'] = False
                _swrfid_status['last_error'] = None

        except Exception as e:
            with _swrfid_status_lock: _swrfid_status['connected'] = False; _swrfid_status['last_error'] = str(e)
            _swrfid_stop_event.wait(5)

    with _swrfid_status_lock: _swrfid_status['connected'] = False

def start_swrfid_listener():
    global _swrfid_thread
    if _swrfid_thread and _swrfid_thread.is_alive(): return
    if not _settings_cache:
        conn = get_db()
        try: _reload_settings_cache_from_db(conn)
        finally: conn.close()
    _swrfid_stop_event.clear()
    _swrfid_thread = threading.Thread(target=_swrfid_listener, daemon=True); _swrfid_thread.start()
