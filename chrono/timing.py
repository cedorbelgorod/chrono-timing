# -*- coding: utf-8 -*-
"""Домен хронометража: заезды, круги, финиши, результаты, стартовый протокол."""
import os
import sqlite3
import time
from datetime import datetime, timedelta

from chrono.db import _comp_db_path, _connect, _registry_load
from chrono.settings import _comp_format, _heat_format, _itt_interval, get_cached_setting
from chrono.utils import _fmt_eta_short, _fmt_start_at, format_secs, format_secs_short


def _heat_participant_counts(conn, cid):
    """{heat_id: {'total': n, 'active': m}} — для списков стартов."""
    out = {}
    for r in conn.execute(
            'SELECT hp.heat_id, COUNT(*) AS total, '
            "SUM(CASE WHEN hp.status != 'dns' THEN 1 ELSE 0 END) AS active "
            'FROM heat_participants hp JOIN heats h ON h.id = hp.heat_id '
            'WHERE h.competition_id = ? GROUP BY hp.heat_id', (cid,)).fetchall():
        out[r['heat_id']] = {'total': r['total'], 'active': r['active'] or 0}
    return out

def _load_auto_laps_by_race(conn, race_ids):
    if not race_ids: return {}
    placeholders = ','.join('?' * len(race_ids))
    rows = conn.execute(f'SELECT * FROM auto_laps WHERE race_id IN ({placeholders}) ORDER BY race_id, lap_number', race_ids).fetchall()
    out = {}
    for r in rows: out.setdefault(r['race_id'], []).append(r)
    return out

def _merge_laps(manual_rows, auto_rows, min_lap_sec, laps_total=None):
    """Объединяет ручные и авто-отметки в одну хронологию без дублей (чистая функция)."""
    marks = [{'time': a['scan_time'], 'source': 'A', 'id': a['id'], 'user': None} for a in auto_rows]

    for m in manual_rows:
        m_dt = datetime.fromisoformat(m['lap_time'])
        conflict = any(
            abs((m_dt - datetime.fromisoformat(a['scan_time'])).total_seconds()) <= min_lap_sec
            for a in auto_rows)
        if not conflict:
            marks.append({'time': m['lap_time'], 'source': 'M', 'id': m['id'], 'user': m['user_login']})

    marks.sort(key=lambda x: x['time'])
    return marks[:laps_total] if laps_total is not None else marks

def _build_merged_laps(conn, race_id, min_lap_sec, laps_total=None):
    """Обёртка _merge_laps для одиночного заезда (загружает отметки из БД)."""
    auto = conn.execute('SELECT id, scan_time FROM auto_laps WHERE race_id = ? ORDER BY lap_number', (race_id,)).fetchall()
    manual = conn.execute('SELECT id, lap_time, user_login FROM laps WHERE race_id = ? ORDER BY lap_number', (race_id,)).fetchall()
    return _merge_laps(manual, auto, min_lap_sec, laps_total)

def _load_laps_by_race(conn, race_ids):
    if not race_ids: return {}
    placeholders = ','.join('?' * len(race_ids))
    rows = conn.execute(f'SELECT * FROM laps WHERE race_id IN ({placeholders}) ORDER BY race_id, lap_number', race_ids).fetchall()
    out = {}
    for r in rows: out.setdefault(r['race_id'], []).append(r)
    return out

def prepare_race(row, conn, laps_rows=None, auto_laps_rows=None, min_lap=None, include_auto=False):
    d = dict(row)
    # planned_laps — дистанция по регламенту (из категории). laps_total может быть
    # уменьшен механикой «финиш после лидера», поэтому для показа берём план.
    if not d.get('planned_laps'):
        d['planned_laps'] = d.get('laps_total') or 1
    # номер может отсутствовать (выдаётся на старте) — для сортировок приводим к числу
    if d.get('number') is None:
        d['number'] = 0
    d['number_display'] = d['number'] or '—'
    start = datetime.fromisoformat(d['start_time'])
    d['start_display'] = start.strftime('%H:%M:%S')
    d['start_dt_local'] = start.strftime('%Y-%m-%dT%H:%M:%S')
    d['start_epoch_ms'] = int(start.timestamp() * 1000)
    d['sort_time'] = float('inf')

    if include_auto:
        if min_lap is None:
            h = conn.execute('SELECT min_lap_time FROM heats WHERE id = ?', (d.get('heat_id'),)).fetchone()
            min_lap = h['min_lap_time'] if h and h['min_lap_time'] is not None else 0
        if auto_laps_rows is None:
            auto_laps_rows = conn.execute('SELECT id, scan_time FROM auto_laps WHERE race_id = ? ORDER BY lap_number', (d['id'],)).fetchall()
        if laps_rows is None:
            laps_rows = conn.execute('SELECT id, lap_time, user_login FROM laps WHERE race_id = ? ORDER BY lap_number', (d['id'],)).fetchall()
        marks = _merge_laps(laps_rows, auto_laps_rows, min_lap, d['laps_total'])

        laps = []
        prev = start
        last_elapsed = None
        for i, m in enumerate(marks):
            lap_dt = datetime.fromisoformat(m['time'])
            elapsed = (lap_dt - start).total_seconds()
            split_secs = (lap_dt - prev).total_seconds()
            lap = {
                'lap_number': i + 1,
                'lap_time': m['time'],
                'source': m['source'],
                'id': m.get('id'),
                'user': m.get('user'),
                'display': lap_dt.strftime('%H:%M:%S'),
                'elapsed_seconds': elapsed,
                'elapsed_display': format_secs(elapsed),
                'split_seconds': split_secs,
                'split_display': format_secs(split_secs),
            }
            prev = lap_dt
            last_elapsed = elapsed
            laps.append(lap)
        d['laps'] = laps
        d['last_lap_display'] = laps[-1]['elapsed_display'] if laps else None

        orig_status = d.get('status') or 'running'
        if orig_status not in ('dns', 'dnf', 'lapped'):
            if len(laps) >= d['laps_total']:
                finish_dt = datetime.fromisoformat(laps[d['laps_total'] - 1]['lap_time'])
                d['finish_time'] = finish_dt.isoformat()
                d['finish_display'] = finish_dt.strftime('%H:%M:%S')
                d['total_display'] = format_secs((finish_dt - start).total_seconds())
                d['sort_time'] = (finish_dt - start).total_seconds()
                d['status'] = 'ok'
                d['status_display'] = 'OK'
                d['result_display'] = d['total_display']
            else:
                d['finish_time'] = None
                d['finish_display'] = None
                d['total_display'] = None
                d['status'] = 'running'
                d['status_display'] = '—'
                d['result_display'] = ''
                if last_elapsed is not None:
                    d['sort_time'] = last_elapsed

        status = d.get('status') or 'running'
        if status == 'ok':
            d['status_display'] = 'OK'; d['result_display'] = d['total_display'] or '—'
        elif status == 'dns':
            d['status_display'] = 'DNS'; d['result_display'] = 'DNS'
        elif status == 'dnf':
            d['status_display'] = 'DNF'; d['result_display'] = 'DNF'
        elif status == 'lapped':
            d['status_display'] = f'+{d.get("laps_behind", 0)} кр.'; d['result_display'] = d['status_display']
        else:
            d['status_display'] = '—'; d['result_display'] = ''
        return d

    if d['finish_time']:
        finish = datetime.fromisoformat(d['finish_time'])
        d['finish_display'] = finish.strftime('%H:%M:%S')
        d['total_display'] = format_secs((finish - start).total_seconds())
        d['sort_time'] = (finish - start).total_seconds()
    else:
        d['finish_display'] = None
        d['total_display'] = None

    if laps_rows is None:
        laps_rows = conn.execute('SELECT * FROM laps WHERE race_id = ? ORDER BY lap_number', (d['id'],)).fetchall()

    laps = []
    prev = start
    last_elapsed = None
    for lap_row in laps_rows:
        lap = dict(lap_row)
        lap_dt = datetime.fromisoformat(lap['lap_time'])
        elapsed = (lap_dt - start).total_seconds()
        split_secs = (lap_dt - prev).total_seconds()
        lap.update({
            'display': lap_dt.strftime('%H:%M:%S'), 'elapsed_seconds': elapsed,
            'elapsed_display': format_secs(elapsed), 'split_seconds': split_secs,
            'split_display': format_secs(split_secs),
            'source': 'M',
            'user': lap.get('user_login'),
        })
        prev = lap_dt
        last_elapsed = elapsed
        laps.append(lap)
    d['laps'] = laps
    d['last_lap_display'] = laps[-1]['elapsed_display'] if laps else None

    if not d['finish_time'] and last_elapsed is not None:
        d['sort_time'] = last_elapsed

    status = d.get('status') or 'running'
    if status == 'ok':
        d['status_display'] = 'OK'; d['result_display'] = d['total_display'] or '—'
    elif status == 'dns':
        d['status_display'] = 'DNS'; d['result_display'] = 'DNS'
    elif status == 'dnf':
        d['status_display'] = 'DNF'; d['result_display'] = 'DNF'
    elif status == 'lapped':
        d['status_display'] = f'+{d.get("laps_behind", 0)} кр.'; d['result_display'] = d['status_display']
    else:
        d['status_display'] = '—'; d['result_display'] = ''
    return d

def _predict_race(race, now=None, fallback_lap_secs=None):
    if now is None: now = datetime.now()
    start_dt = datetime.fromisoformat(race['start_time'])
    laps = race.get('laps') or []
    laps_done = len(laps)
    remaining = max(0, race['laps_total'] - laps_done)
    last_lap_dt = datetime.fromisoformat(laps[-1]['lap_time']) if laps else start_dt

    if laps:
        splits = [l['split_seconds'] for l in laps]
        predicted = splits[-1] * 0.6 + (sum(splits) / len(splits)) * 0.4
        predicted_source = 'own'
    elif fallback_lap_secs:
        predicted = fallback_lap_secs
        predicted_source = 'heat'
    else:
        predicted = None
        predicted_source = None

    race['predicted_lap_secs'] = predicted
    race['predicted_source'] = predicted_source

    if predicted is None or remaining == 0:
        race.update({'eta_next_event_secs': None, 'eta_next_display': None, 'overdue_next': False,
                     'eta_finish_secs': None, 'eta_finish_display': None, 'next_event_label': None, 'is_finishing': False})
        return race

    elapsed = (now - last_lap_dt).total_seconds()
    eta_next = predicted - elapsed
    race['eta_next_event_secs'] = eta_next
    race['overdue_next'] = (eta_next <= 0)
    race['is_finishing'] = (remaining == 1)

    if remaining == 1:
        race['next_event_label'] = 'финиш'
        race['eta_finish_secs'] = eta_next
    else:
        race['next_event_label'] = f'круг {laps_done + 1}'
        race['eta_finish_secs'] = eta_next + predicted * (remaining - 1)

    race['eta_next_display'] = _fmt_eta_short(eta_next)
    race['eta_finish_display'] = _fmt_eta_short(race['eta_finish_secs'])
    return race

def maybe_finish_heat(conn, hid):
    if not hid: return
    h = conn.execute('SELECT status, format FROM heats WHERE id = ?', (hid,)).fetchone()
    if not h or h['status'] != 'running': return
    if conn.execute("SELECT COUNT(*) FROM races WHERE heat_id = ? AND status = 'running'", (hid,)).fetchone()[0] == 0:
        # разделка: пока есть не стартовавшие, старт не закрываем
        if _heat_format(h) == 'itt':
            waiting = conn.execute(
                "SELECT COUNT(*) FROM heat_participants hp WHERE hp.heat_id = ? AND hp.status = 'active' "
                "AND NOT EXISTS (SELECT 1 FROM races r WHERE r.heat_id = hp.heat_id "
                "                AND r.participant_id = hp.participant_id)", (hid,)).fetchone()[0]
            if waiting:
                return
        conn.execute("UPDATE heats SET status = 'finished', finished_at = ? WHERE id = ?", (datetime.now().isoformat(), hid))

def _process_race_finish(conn, race, now_iso, force=False):
    h = conn.execute('SELECT min_lap_time FROM heats WHERE id = ?', (race['heat_id'],)).fetchone()
    min_lap = h['min_lap_time'] if h and h['min_lap_time'] is not None else 0
    merged = _build_merged_laps(conn, race['id'], min_lap)
    laps_done = len(merged)

    if force or not merged:
        finish_time = now_iso
    else:
        finish_time = merged[-1]['time']

    cat_id = conn.execute("SELECT category_id FROM participants WHERE id = ?", (race['participant_id'],)).fetchone()['category_id']
    leader_row = conn.execute(
        "SELECT MAX(laps_count) FROM (SELECT COUNT(*) as laps_count FROM laps JOIN races ON races.id = laps.race_id "
        "WHERE races.heat_id = ? AND races.status = 'ok' AND races.participant_id IN (SELECT id FROM participants WHERE category_id = ?) GROUP BY races.id)",
        (race['heat_id'], cat_id)
    ).fetchone()
    laps_behind = max(0, (leader_row[0] if leader_row and leader_row[0] else laps_done) - laps_done)
    conn.execute("UPDATE races SET finish_time = ?, status = 'ok', laps_behind = ? WHERE id = ?", (finish_time, laps_behind, race['id']))

    finished_count = conn.execute(
        "SELECT COUNT(*) FROM races WHERE heat_id = ? AND status = 'ok' AND participant_id IN (SELECT id FROM participants WHERE category_id = ?)",
        (race['heat_id'], cat_id)
    ).fetchone()[0]

    h2 = conn.execute('SELECT finish_on_leader FROM heats WHERE id = ?', (race['heat_id'],)).fetchone()
    if h2 and h2['finish_on_leader'] and finished_count == 1:
        others = conn.execute("SELECT id FROM races WHERE heat_id = ? AND status = 'running' AND participant_id IN (SELECT id FROM participants WHERE category_id = ?)", (race['heat_id'], cat_id)).fetchall()
        for o in others:
            o_laps = len(_build_merged_laps(conn, o['id'], min_lap))
            conn.execute("UPDATE races SET laps_total = ? WHERE id = ?", (o_laps + 1, o['id']))
    maybe_finish_heat(conn, race['heat_id'])

def _force_finish_heat(conn, hid, dnf_ids=None):
    h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
    if not h or h['status'] != 'running': return False
    dnf_ids = set(dnf_ids or [])
    races = conn.execute('SELECT r.*, p.category_id FROM races r JOIN participants p ON p.id = r.participant_id WHERE r.heat_id = ?', (hid,)).fetchall()
    lap_counts = _race_lap_counts(conn, [r['id'] for r in races])
    cat_max_laps = {}
    for r in races:
        if r['category_id'] not in cat_max_laps or lap_counts[r['id']] > cat_max_laps[r['category_id']]:
            cat_max_laps[r['category_id']] = lap_counts[r['id']]
    for r in races:
        if r['status'] != 'running': continue
        if r['id'] in dnf_ids:
            conn.execute("UPDATE races SET status = 'dnf', laps_behind = 0 WHERE id = ?", (r['id'],))
            continue
        laps = lap_counts[r['id']]
        cat_max = cat_max_laps.get(r['category_id'], 0)
        if cat_max == 0:
            conn.execute("UPDATE races SET status = 'dnf', laps_behind = 0 WHERE id = ?", (r['id'],))
            continue
        conn.execute("UPDATE races SET laps_total = ? WHERE id = ?", (cat_max, r['id']))
        if laps >= cat_max:
            last_lap = conn.execute('SELECT lap_time FROM laps WHERE race_id = ? ORDER BY lap_number DESC LIMIT 1', (r['id'],)).fetchone()
            conn.execute("UPDATE races SET status = 'ok', finish_time = ?, laps_behind = 0 WHERE id = ?", (last_lap['lap_time'] if last_lap else r['start_time'], r['id']))
        else:
            conn.execute("UPDATE races SET status = 'lapped', laps_behind = ? WHERE id = ?", (cat_max - laps, r['id']))
    conn.execute("UPDATE heats SET status = 'finished', finished_at = ? WHERE id = ?", (datetime.now().isoformat(), hid))
    return True

def _count_races(races):
    """Счётчики статусов заездов: total, running, ok, dnf, lapped."""
    counts = {'total': len(races), 'running': 0, 'ok': 0, 'dnf': 0, 'lapped': 0}
    for r in races:
        status = r.get('status')
        if status in counts:
            counts[status] += 1
    return counts

def _assign_category_gaps(races):
    """Проставляет gap_display/gap_kind внутри одной категории: лидер + отставание от него."""
    leader = None
    for r in races:
        if not r['laps']:
            continue
        if (leader is None
                or len(r['laps']) > len(leader['laps'])
                or (len(r['laps']) == len(leader['laps']) and r['sort_time'] < leader['sort_time'])):
            leader = r

    for r in races:
        gap, kind = '', ''
        if leader and r['laps']:
            if r['id'] == leader['id']:
                gap, kind = 'лидер', 'leader'
            else:
                lap_diff = len(leader['laps']) - len(r['laps'])
                if lap_diff > 0:
                    gap, kind = f'+{lap_diff} кр.', 'laps'
                else:
                    diff = r['sort_time'] - leader['sort_time']
                    if diff >= 0.05:
                        gap = f'+{diff:.1f}' if diff < 60 else '+' + format_secs_short(diff)
                        kind = 'time'
        r['gap_display'] = gap
        r['gap_kind'] = kind
    return races

def _race_lap_counts(conn, race_ids):
    result = {rid: 0 for rid in race_ids}
    if not race_ids: return result
    placeholders = ','.join('?' * len(race_ids))
    for r in conn.execute(f'SELECT race_id, COUNT(*) as c FROM laps WHERE race_id IN ({placeholders}) GROUP BY race_id', race_ids).fetchall():
        result[r['race_id']] = r['c']
    return result

def _running_itt_heat(conn, cid):
    """Активный старт разделки (последний запущенный)."""
    return conn.execute(
        "SELECT * FROM heats WHERE competition_id = ? AND status = 'running' "
        "ORDER BY heat_number DESC LIMIT 1", (cid,)).fetchone()

def _manual_start_queue(conn, hid):
    """Участники старта, которые ещё не стартовали.

    Порядок: ручная расстановка (sort_order), затем — по настройке старта:
    «number» — по номеру, «category» — по ID группы, затем по номеру.
    """
    heat = conn.execute('SELECT start_order FROM heats WHERE id = ?', (hid,)).fetchone()
    mode = 'number'
    if heat is not None:
        try:
            mode = heat['start_order'] or 'number'
        except (KeyError, IndexError, TypeError):
            mode = heat[0] or 'number'
    if mode == 'category':
        # порядок групп — по колонке «сортировка» категории (по умолчанию равна её ID)
        order_by = ('COALESCE(hp.sort_order, 1000000), (c.id IS NULL), '
                    'COALESCE(c.sort_order, c.id), p.number')
    else:
        order_by = 'COALESCE(hp.sort_order, 1000000), p.number'
    return conn.execute(
        'SELECT hp.participant_id, hp.sort_order, p.number, p.full_name, '
        '       p.category_id, c.name AS category_name, COALESCE(c.laps_total, 1) AS laps_total '
        'FROM heat_participants hp '
        'JOIN participants p ON p.id = hp.participant_id '
        'LEFT JOIN categories c ON c.id = p.category_id '
        "WHERE hp.heat_id = ? AND hp.status = 'active' AND p.number IS NOT NULL "
        '  AND NOT EXISTS (SELECT 1 FROM races r WHERE r.heat_id = hp.heat_id '
        '                  AND r.participant_id = hp.participant_id) '
        f'ORDER BY {order_by} LIMIT ?', (hid, 50)).fetchall()

def _manual_start_state(conn, cid):
    """Состояние табло раздельного старта."""
    comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
    interval = _itt_interval()
    state = {
        'ok': True,
        'format': _comp_format(comp),
        'interval': interval,
        'sound': get_cached_setting('itt_countdown_sound', '1') == '1',
        'now_ts': time.time(),
        'heat': None,
        'queue': [],
        'next': None,
        'last': [],
        'planned_ts': None,
        'total': 0, 'started': 0, 'finished': 0, 'dns': 0, 'waiting': 0,
        'mode': 'none', 'undo_target': None,
    }
    if comp is None:
        return state
    heat = _running_itt_heat(conn, cid)
    state['mode'] = 'running' if heat else 'none'
    if not heat:
        # до запуска старта табло показывает очередь следующего старта — можно переставить порядок
        heat = conn.execute(
            "SELECT * FROM heats WHERE competition_id = ? AND status = 'pending' AND format = 'itt' "
            'ORDER BY heat_number ASC LIMIT 1', (cid,)).fetchone()
        if heat:
            state['mode'] = 'preview'
    if not heat:
        return state
    hid = heat['id']
    state['heat'] = {'id': hid, 'number': heat['heat_number'], 'status': heat['status'],
                     'name': heat['name'] or '', 'started_at': heat['started_at'],
                     'start_order': (heat['start_order'] if 'start_order' in heat.keys() else 'number') or 'number'}

    rows = conn.execute(
        'SELECT hp.participant_id, hp.status AS hp_status, p.number, p.full_name, c.name AS category_name, '
        '       (SELECT r.id FROM races r WHERE r.heat_id = hp.heat_id AND r.participant_id = hp.participant_id) AS race_id, '
        '       (SELECT r.start_time FROM races r WHERE r.heat_id = hp.heat_id AND r.participant_id = hp.participant_id) AS start_time, '
        '       (SELECT r.status FROM races r WHERE r.heat_id = hp.heat_id AND r.participant_id = hp.participant_id) AS race_status '
        'FROM heat_participants hp '
        'JOIN participants p ON p.id = hp.participant_id '
        'LEFT JOIN categories c ON c.id = p.category_id '
        'WHERE hp.heat_id = ? AND p.number IS NOT NULL ORDER BY p.number', (hid,)).fetchall()

    waiting_rows = _manual_start_queue(conn, hid)
    waiting = waiting_rows
    started = [r for r in rows if r['race_id'] is not None and (r['race_status'] or '') != 'dns']
    dns = [r for r in rows if (r['hp_status'] == 'dns') or (r['race_status'] or '') == 'dns']
    finished = [r for r in started if (r['race_status'] or '') == 'ok']

    state['total'] = len(rows)
    state['waiting'] = len(waiting)
    state['started'] = len(started)
    state['finished'] = len(finished)
    state['dns'] = len(dns)
    state['queue'] = [{'pid': r['participant_id'], 'number': r['number'], 'name': r['full_name'],
                       'category': r['category_name'] or '',
                       'laps': r['laps_total'] or 1} for r in waiting[:8]]
    state['next'] = state['queue'][0] if state['queue'] else None

    last_rows = conn.execute(
        'SELECT r.start_time, r.status, p.number, p.full_name FROM races r '
        'JOIN participants p ON p.id = r.participant_id '
        "WHERE r.heat_id = ? AND r.status != 'dns' "
        'ORDER BY r.start_time DESC LIMIT 8', (hid,)).fetchall()
    state['last'] = [{'number': r['number'], 'name': r['full_name'],
                      'time': (r['start_time'] or '')[11:19],
                      'finished': r['status'] == 'ok'} for r in last_rows]

    # кто будет отменён кнопкой «↶» — последний стартовавший и ещё едущий
    undo_row = conn.execute(
        'SELECT p.number, p.full_name FROM races r JOIN participants p ON p.id = r.participant_id '
        "WHERE r.heat_id = ? AND r.status = 'running' ORDER BY r.start_time DESC LIMIT 1", (hid,)).fetchone()
    state['undo_target'] = ({'number': undo_row['number'], 'name': undo_row['full_name']}
                            if undo_row else None)

    if started and waiting:
        times = []
        for r in started:
            try:
                times.append(datetime.fromisoformat(r['start_time']).timestamp())
            except (TypeError, ValueError):
                pass
        if times:
            state['planned_ts'] = max(times) + interval
    return state

def _heat_start_rows(conn, heat):
    """Участники старта в порядке старта: позиция, план/факт время, статус."""
    hid = heat['id']
    try:
        mode = heat['start_order'] or 'number'
    except (KeyError, IndexError):
        mode = 'number'
    rows = conn.execute(
        'SELECT hp.participant_id, hp.status AS hp_status, hp.sort_order, '
        '       p.number, p.full_name, p.birth_year, p.city, p.team, '
        "       COALESCE(c.name, '') AS category_name, COALESCE(c.laps_total, 1) AS category_laps, "
        '       c.sort_order AS cat_sort, '
        '       r.id AS race_id, r.status AS race_status, r.start_time, r.finish_time '
        'FROM heat_participants hp '
        'JOIN participants p ON p.id = hp.participant_id '
        'LEFT JOIN categories c ON c.id = p.category_id '
        'LEFT JOIN races r ON r.heat_id = hp.heat_id AND r.participant_id = hp.participant_id '
        'WHERE hp.heat_id = ?', (hid,)).fetchall()

    def rank(r):
        is_dns = (r['hp_status'] == 'dns') or ((r['race_status'] or '') == 'dns')
        if is_dns:
            return (2, 0, 0, r['number'] or 0)
        if r['race_id'] is not None:
            return (0, 0, 0, r['start_time'] or '')
        manual = r['sort_order']
        if manual is not None:
            return (1, 0, manual, r['number'] or 0)
        cat = r['cat_sort'] if r['cat_sort'] is not None else 1000000
        return (1, 1, cat if mode == 'category' else 0, r['number'] or 0)

    rows = sorted(rows, key=rank)
    interval = _itt_interval()
    start_at = None
    try:
        if heat['start_at']:
            start_at = datetime.fromisoformat(heat['start_at'])
    except (KeyError, IndexError, TypeError, ValueError):
        start_at = None

    out, position = [], 0
    for r in rows:
        is_dns = (r['hp_status'] == 'dns') or ((r['race_status'] or '') == 'dns')
        race_status = (r['race_status'] or '') if r['race_id'] is not None else ''
        if is_dns:
            status = 'dns'
        elif race_status in ('ok', 'dnf', 'lapped'):
            status = 'finished'
        elif race_status == 'running':
            status = 'running'
        else:
            status = 'waiting'
        planned = ''
        if not is_dns:
            position += 1
            if start_at is not None:
                planned = (start_at + timedelta(seconds=interval * (position - 1))).strftime('%H:%M:%S')
        actual = (r['start_time'] or '')[11:19] if r['race_id'] is not None and not is_dns else ''
        out.append({
            'participant_id': r['participant_id'],
            'position': position if not is_dns else None,
            'number': r['number'],
            'full_name': r['full_name'],
            'birth_year': r['birth_year'],
            'category': r['category_name'] or '',
            'laps': r['category_laps'] or 1,
            'city': r['city'] or '',
            'team': r['team'] or '',
            'status': status,
            'manual': (r['sort_order'] is not None) and status == 'waiting',
            'planned_display': planned,
            'actual_display': actual,
            'finish_display': (r['finish_time'] or '')[11:19] if status == 'finished' else '',
        })
    return out

def _heat_startlist_data(conn, heat, comp):
    """Данные стартового протокола."""
    rows = _heat_start_rows(conn, heat)
    total = sum(1 for r in rows if r['status'] != 'dns')
    started = sum(1 for r in rows if r['status'] in ('running', 'finished'))
    finished = sum(1 for r in rows if r['status'] == 'finished')
    return {
        'heat': {
            'id': heat['id'],
            'number': heat['heat_number'],
            'name': heat['name'] or '',
            'status': heat['status'],
            'format': _heat_format(heat),
            'interval': _itt_interval(),
            'start_at': _fmt_start_at(heat['start_at'] if 'start_at' in heat.keys() else None),
            'started_display': (heat['started_at'] or '')[11:19] if heat['started_at'] else '',
        },
        'competition': comp['name'] if comp else '',
        'rows': rows,
        'total': total,
        'started': started,
        'finished': finished,
        'date_display': datetime.now().strftime('%d.%m.%Y'),
    }

def _find_heat_by_token(token):
    """Ищет старт по токену во всех базах соревнований."""
    if not token:
        return None
    try:
        reg = _registry_load()
    except Exception:
        return None
    for entry in reg.get('competitions', []):
        path = _comp_db_path(entry)
        if not os.path.exists(path):
            continue
        conn = None
        try:
            conn = _connect(path)
            h = conn.execute('SELECT * FROM heats WHERE startlist_token = ?', (token,)).fetchone()
            if h:
                comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (h['competition_id'],)).fetchone()
                if comp:
                    return conn, h, comp
        except sqlite3.Error:
            pass
        if conn is not None:
            conn.close()
    return None

def _build_results_detail(conn, hid, cat_filter_id=None, include_auto=True):
    heat_row = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
    heat_fmt = _heat_format(heat_row)
    q = '''SELECT r.*, p.number, p.full_name, p.birth_year, p.gender, c.id AS category_id, c.name AS category_name, c.laps_total AS category_laps
           FROM races r JOIN participants p ON p.id = r.participant_id LEFT JOIN categories c ON c.id = p.category_id WHERE r.heat_id = ?'''
    params = [hid]
    if cat_filter_id is not None:
        q += ' AND p.category_id = ?'; params.append(cat_filter_id)
    q += ' ORDER BY p.number'
    rows = conn.execute(q, params).fetchall()
    laps_map = _load_laps_by_race(conn, [row['id'] for row in rows])

    grouped = {}
    for row in rows:
        prepared = prepare_race(row, conn, laps_map.get(row['id'], []), include_auto=include_auto)
        cat_id = row['category_id']; cat_name = row['category_name'] or 'Без категории'; cat_laps = row['category_laps'] or prepared['laps_total'] or 1
        key = cat_id if cat_id is not None else 0
        if key not in grouped: grouped[key] = {'id': cat_id, 'name': cat_name, 'laps_total': cat_laps, 'races': []}
        grouped[key]['races'].append(prepared)

    blocks = []
    for cat in grouped.values():
        races = cat['races']
        races.sort(key=lambda r: (1 if r['status'] == 'dnf' else (2 if r['status'] == 'dns' else 0), -len(r['laps']), r.get('sort_time', float('inf')), r['number']))
        leader = next((r for r in races if r['status'] == 'ok'), None)
        max_laps = max(cat['laps_total'], max(len(r['laps']) for r in races))
        cat['laps_total'] = max_laps

        rows_data = []
        for r in races:
            lap_cells = [{
                'cumulative': format_secs_short(l['elapsed_seconds']),
                'split': format_secs_short(l['split_seconds']),
                'source': l.get('source', 'M'),
                'user': l.get('user'),
            } for l in r['laps']]
            while len(lap_cells) < max_laps: lap_cells.append(None)

            if r['status'] == 'ok':
                total_short = format_secs_short(r['sort_time']) if r['sort_time'] != float('inf') else '—'
                total_display = f"{total_short} (+{r['laps_behind']} кр.)" if r['laps_behind'] > 0 else total_short
                if leader and r['id'] == leader['id']: gap = '—'
                elif leader and leader['sort_time'] is not None:
                    gap = f"+{r['laps_behind']} кр." if r['laps_behind'] > 0 else ('+' + format_secs_short(r['sort_time'] - leader['sort_time']) if r['sort_time'] - leader['sort_time'] > 0.0005 else '—')
                else: gap = ''
            elif r['status'] == 'lapped':
                total_display = f"+{r['laps_behind']} кр."; gap = total_display
            elif r['status'] in ('dnf', 'dns'):
                total_display = r['status'].upper(); gap = ''
            else:
                total_display = '—'; gap = ''
            rows_data.append({'race': r, 'laps': lap_cells, 'total_display': total_display, 'gap_display': gap,
                              'start_display': (r['start_time'] or '')[11:19],
                              'finish_display': ((r.get('finish_time') or '')[11:19] if r['status'] == 'ok' else ''),
                              'laps_done': len(r['laps']),
                              'laps_total': r.get('planned_laps') or r['laps_total'] or 1,
                              'place': sum(1 for x in rows_data if x['race']['status'] == 'ok') + (1 if r['status'] == 'ok' else 0)})

        # «Место (сейчас)»: внутри категории, по возрастанию времени —
        # финишировавшие по итоговому, едущие по «в пути»
        now_dt = datetime.now()
        for x in rows_data:
            race = x['race']
            secs = None
            if race['status'] == 'ok':
                st = race.get('sort_time')
                if st is not None and st != float('inf'):
                    secs = st
                elif race.get('finish_time') and race.get('start_time'):
                    try:
                        secs = (datetime.fromisoformat(race['finish_time'])
                                - datetime.fromisoformat(race['start_time'])).total_seconds()
                    except (TypeError, ValueError):
                        secs = None
            elif race['status'] == 'running' and race.get('start_time'):
                # оценка по факту последнего пройденного круга: темп × план
                laps = race.get('laps') or []
                if laps:
                    done = len(laps)
                    total = race.get('planned_laps') or race['laps_total'] or done
                    secs = laps[-1]['elapsed_seconds'] * total / done
            x['live_seconds'] = secs
        ranked = sorted([x for x in rows_data if x['live_seconds'] is not None],
                        key=lambda x: (x['live_seconds'], x['race']['number'] or 0))
        for i, x in enumerate(ranked, start=1):
            x['place_now'] = i
        for x in rows_data:
            x.setdefault('place_now', None)

        blocks.append({'category': cat, 'laps_total': max_laps, 'rows': rows_data, 'format': heat_fmt})
    return sorted(blocks, key=lambda x: (x['category']['name'] or '').lower())

def _build_races_heats(conn, cid):
    """Старты со всеми заездами — для страницы «Старт» и её автообновления."""
    heats_data = []
    for h in conn.execute('SELECT * FROM heats WHERE competition_id = ? ORDER BY heat_number', (cid,)).fetchall():
        hd = dict(h)
        heat_cats = [dict(c) for c in conn.execute('SELECT c.* FROM categories c JOIN heat_categories hc ON hc.category_id = c.id WHERE hc.heat_id = ? ORDER BY c.name', (h['id'],)).fetchall()]
        has_uncategorized = conn.execute(
            'SELECT 1 FROM heat_participants hp JOIN participants p ON p.id = hp.participant_id '
            'WHERE hp.heat_id = ? AND p.category_id IS NULL LIMIT 1', (h['id'],)).fetchone()
        if has_uncategorized:
            heat_cats.append({'id': 0, 'name': 'Без категории', 'laps_total': 1})
        hd['categories'] = heat_cats
        hd['scheduled_participants'] = conn.execute(
            "SELECT p.id, p.number, p.full_name, COALESCE(c.name, 'Без категории') AS category_name, hp.status "
            'FROM participants p JOIN heat_participants hp ON hp.participant_id = p.id '
            'LEFT JOIN categories c ON c.id = p.category_id '
            'WHERE hp.heat_id = ? ORDER BY hp.status, p.number', (h['id'],)).fetchall()
        race_rows = conn.execute(
            "SELECT r.*, p.number, p.full_name, p.birth_year, p.gender, "
            "COALESCE(c.name, 'Без категории') AS category_name "
            'FROM races r JOIN participants p ON p.id = r.participant_id '
            'LEFT JOIN categories c ON c.id = p.category_id WHERE r.heat_id = ? ORDER BY p.number', (h['id'],)).fetchall()

        laps_map = _load_laps_by_race(conn, [r['id'] for r in race_rows])
        hd['races'] = [prepare_race(r, conn, laps_map.get(r['id'], []), include_auto=True) for r in race_rows]

        if h['status'] == 'finished':
            multi_cat = len(hd['categories']) > 1
            hd['races'].sort(key=lambda r: ((r['category_name'] or '',) if multi_cat else ()) + (1 if r['status'] == 'dnf' else (2 if r['status'] == 'dns' else 0), -len(r['laps']), r.get('sort_time', float('inf')), r['number']))
        hd['races_total'] = sum(1 for r in hd['races'] if r['status'] != 'dns')
        hd['races_finished'] = sum(1 for r in hd['races'] if r['status'] in ('ok', 'dnf', 'lapped'))
        if h['started_at']:
            st = datetime.fromisoformat(h['started_at']); hd['started_display'] = st.strftime('%H:%M:%S'); hd['started_dt_local'] = st.strftime('%Y-%m-%dT%H:%M:%S')
        else: hd['started_display'] = None; hd['started_dt_local'] = None
        hd['running_list'] = [{'id': r['id'], 'number': r['number'], 'name': r['full_name'],
                               'laps': len(r['laps']), 'total': r['laps_total']}
                              for r in hd['races'] if r['status'] == 'running']

        hd['is_itt'] = _heat_format(h) == 'itt'
        hd['start_order'] = (hd.get('start_order') or 'number')
        hd['start_at_local'] = (hd.get('start_at') or '')[:16]
        hd['start_at_display'] = _fmt_start_at(hd.get('start_at'))
        hd['interval'] = _itt_interval()
        if hd['is_itt']:
            hd['start_rows'] = _heat_start_rows(conn, h)
            hd['order_manual'] = any(r['manual'] for r in hd['start_rows'])
        heats_data.append(hd)
    return heats_data

def _start_heat(conn, h):
    """Создаёт заезды старта и переводит его в running.

    Возвращает число активных участников или None, если запускать некого.
    """
    hid, cid = h['id'], h['competition_id']
    participants = conn.execute(
        'SELECT hp.participant_id, hp.status, COALESCE(c.laps_total, 1) AS laps_total FROM heat_participants hp '
        'JOIN participants p ON p.id = hp.participant_id '
        'LEFT JOIN categories c ON c.id = p.category_id '
        'WHERE hp.heat_id = ? AND p.number IS NOT NULL '
        'AND p.id NOT IN (SELECT participant_id FROM races WHERE competition_id = ?)',
        (hid, cid)).fetchall()
    if not participants: return None
    now_iso = datetime.now().isoformat(); active_count = 0
    is_itt = _heat_format(h) == 'itt'
    with conn:
        for p in participants:
            race_status = 'running' if p['status'] == 'active' else 'dns'
            if race_status == 'running': active_count += 1
            # разделка: заезд создаётся в момент старта участника, здесь — только DNS
            if is_itt and race_status == 'running':
                continue
            laps = p['laps_total'] or 1
            conn.execute('INSERT INTO races (participant_id, competition_id, heat_id, laps_total, planned_laps, start_time, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
                         (p['participant_id'], cid, hid, laps, laps, now_iso, race_status))
        if active_count == 0:
            conn.execute("UPDATE heats SET status = 'finished', started_at = ?, finished_at = ? WHERE id = ?", (now_iso, now_iso, hid))
        else:
            conn.execute("UPDATE heats SET status = 'running', started_at = ? WHERE id = ?", (now_iso, hid))
    return active_count

def _build_heat_view(conn, h):
    """Готовит заезды старта: разбивка по категориям, отставание внутри группы, счётчики.

    Используется и страницей «Сейчас идёт», и публичным табло.
    """
    hd = dict(h)
    races = conn.execute('SELECT r.*, p.number, p.full_name, p.category_id, c.name AS category_name '
                         'FROM races r JOIN participants p ON p.id = r.participant_id '
                         'LEFT JOIN categories c ON c.id = p.category_id '
                         'WHERE r.heat_id = ? ORDER BY p.number', (h['id'],)).fetchall()

    race_ids = [r['id'] for r in races]
    laps_map = _load_laps_by_race(conn, race_ids)
    auto_map = _load_auto_laps_by_race(conn, race_ids)
    min_lap = h['min_lap_time'] or 0
    prepared = []
    for r in races:
        if r['status'] == 'dns': continue
        pr = prepare_race(r, conn, laps_map.get(r['id'], []), auto_map.get(r['id'], []), min_lap, include_auto=True)
        pr['laps_count'] = len(pr['laps'])
        prepared.append(pr)

    # группируем по категориям: отставание считаем внутри своей группы
    groups = {}
    for r in prepared:
        key = r.get('category_id') or 0
        if key not in groups:
            groups[key] = {'id': key, 'name': r.get('category_name') or 'Без категории', 'races': []}
        groups[key]['races'].append(r)

    for g in groups.values():
        _assign_category_gaps(g['races'])
        counts = _count_races(g['races'])
        g.update(total=counts['total'], running_count=counts['running'],
                 finished_count=counts['ok'], dnf_count=counts['dnf'], lapped_count=counts['lapped'])

    hd['races'] = prepared
    counts = _count_races(prepared)
    hd.update(total=counts['total'], running_count=counts['running'],
              finished_count=counts['ok'], dnf_count=counts['dnf'], lapped_count=counts['lapped'])
    hd['categories'] = sorted(groups.values(), key=lambda x: (x['name'] or '').lower())
    hd['running_list'] = [{'id': r['id'], 'number': r['number'], 'name': r['full_name'],
                           'laps': len(r['laps']), 'total': r['laps_total']}
                          for r in prepared if r['status'] == 'running']
    return hd
