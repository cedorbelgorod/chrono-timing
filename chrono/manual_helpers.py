# -*- coding: utf-8 -*-
"""Ручной хронометраж: журнал действий и данные страницы «Хронометраж»."""
from datetime import datetime
import sqlite3
from flask import session
from chrono.db import _create_timing_log
from chrono.timing import (_load_laps_by_race, _predict_race, prepare_race)
from chrono.utils import (_fmt_hms)

LOG_SIZE = 8

AUTO_LOG_SIZE = 20

def _log_timing(conn, source, action, ok, reason=None, competition_id=None, heat_id=None, race_id=None,
                participant_id=None, number=None, full_name=None, user_login=None, detail=None):
    """Пишет событие хронометража в БД. Никогда не роняет основной сценарий."""
    params = (datetime.now().isoformat(timespec='seconds'), competition_id, heat_id, race_id, participant_id,
              source, action, 1 if ok else 0, reason or None,
              None if number is None else str(number), full_name, user_login, detail)
    sql = ('INSERT INTO timing_log (created_at, competition_id, heat_id, race_id, participant_id, source, action, ok,'
           ' reason, number, full_name, user_login, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
    try:
        conn.execute(sql, params)
    except sqlite3.OperationalError:
        # таблицы ещё нет (старая база до первого запуска init_db) — создаём и пробуем снова
        try:
            _create_timing_log(conn)
            conn.execute(sql, params)
        except Exception:
            pass
    except Exception:
        pass

def _push_manual_log(entry):
    log = session.get('manual_log', [])
    log.insert(0, {'ok': entry.get('ok'), 'number': entry.get('number'), 'time': entry.get('time'), 'action': entry.get('action') or entry.get('message') or ''})
    session['manual_log'] = log[:LOG_SIZE]

def _get_manual_data(conn, cid):
    competition = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
    active_races = []
    if competition:
        races_rows = conn.execute('''
            SELECT r.*, p.number, p.full_name, h.heat_number, h.finish_on_leader, c.name AS category_name
            FROM races r JOIN participants p ON p.id = r.participant_id JOIN heats h ON h.id = r.heat_id
            LEFT JOIN categories c ON c.id = p.category_id WHERE r.competition_id = ? AND r.status = 'running' ORDER BY h.heat_number, p.number
        ''', (cid,)).fetchall()
        laps_map = _load_laps_by_race(conn, [r['id'] for r in races_rows])
        active_races = [prepare_race(r, conn, laps_rows=laps_map.get(r['id'], []), include_auto=True) for r in races_rows]

        active_races = [r for r in active_races if r['status'] == 'running']

        heat_splits = {}
        for r in active_races:
            for l in r['laps']: heat_splits.setdefault(r['heat_id'], []).append(l['split_seconds'])
        heat_avg = {hid: sum(v)/len(v) for hid, v in heat_splits.items() if v}

        now = datetime.now()
        active_races = [_predict_race(r, now, heat_avg.get(r['heat_id'])) for r in active_races]
        active_races.sort(key=lambda r: (1, float('inf'), r['number']) if r.get('eta_next_event_secs') is None else (0, r['eta_next_event_secs'], r['number']))
        for r in active_races:
            try:
                r['elapsed_display'] = _fmt_hms((now - datetime.fromisoformat(r['start_time'])).total_seconds())
            except (TypeError, ValueError):
                r['elapsed_display'] = '—'
    return competition, active_races
