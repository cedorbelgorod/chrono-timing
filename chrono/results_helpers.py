# -*- coding: utf-8 -*-
"""results_helpers: помощники (перенесены из app.py)."""
from datetime import datetime
import json, os, time
from flask import redirect, render_template, request, url_for
from chrono.catalog import (_scalar, _to_float)
from chrono.db import (_comp_db_path, _get_competition, _registry_find)
from chrono.settings import (PROTOCOL_DEFAULTS, _comp_format, get_cached_setting, set_cached_setting)
from chrono.timing import (_build_heat_view, _build_results_detail, _count_races, _heat_participant_counts, _load_auto_laps_by_race, _load_laps_by_race, prepare_race)
from chrono.utils import (_qr_svg, _speed_kmh, format_secs_short)

def _public_competition(slug):
    """Соревнование для публичной страницы: None, если скрыто/архив/нет базы."""
    entry = _registry_find(slug)
    if not entry or entry.get('hidden') or entry.get('archived'):
        return None
    path = _comp_db_path(entry)
    if not os.path.exists(path):
        return None
    return entry

WMO_RU = {
    0: 'Ясно', 1: 'Малооблачно', 2: 'Переменная облачность', 3: 'Пасмурно',
    45: 'Туман', 48: 'Изморозь',
    51: 'Слабая морось', 53: 'Морось', 55: 'Сильная морось',
    56: 'Ледяная морось', 57: 'Сильная ледяная морось',
    61: 'Слабый дождь', 63: 'Дождь', 65: 'Сильный дождь',
    66: 'Ледяной дождь', 67: 'Сильный ледяной дождь',
    71: 'Слабый снег', 73: 'Снег', 75: 'Сильный снег', 77: 'Снежная крупа',
    80: 'Слабый ливень', 81: 'Ливень', 82: 'Сильный ливень',
    85: 'Снежный ливень', 86: 'Сильный снежный ливень',
    95: 'Гроза', 96: 'Гроза с градом', 99: 'Сильная гроза с градом',
}

def _base_url():
    """Базовый URL сайта (с учётом CHRONO_PUBLIC_URL)."""
    return (os.environ.get('CHRONO_PUBLIC_URL') or request.url_root).rstrip('/')

def _public_url():
    """Абсолютный URL текущей страницы (с учётом CHRONO_PUBLIC_URL)."""
    base = (os.environ.get('CHRONO_PUBLIC_URL') or request.url_root).rstrip('/')
    query = request.query_string.decode('utf-8', 'ignore')
    return base + request.path + (('?' + query) if query else '')

def _save_proto_settings(conn):
    """Сохраняет все поля протокола, пришедшие в форме."""
    for k in PROTOCOL_DEFAULTS:
        if k in request.form:
            set_cached_setting(conn, k, (request.form.get(k) or '').strip())

def _prepare_results_data(conn, cid, target, include_auto=True):
    competition = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
    if not competition: return None, [], [], [], {}, None, None
    heats = conn.execute("SELECT * FROM heats WHERE competition_id = ? AND status IN ('running','finished') ORDER BY heat_number", (cid,)).fetchall()
    categories = conn.execute('''SELECT DISTINCT c.id, c.name FROM categories c JOIN heat_categories hc ON hc.category_id = c.id JOIN heats h ON h.id = hc.heat_id WHERE c.competition_id = ? AND h.status IN ('running','finished') ORDER BY c.name''', (cid,)).fetchall()
    details, info, selected_heat_id, selected_cat_id = [], {}, None, None

    if target.startswith('heat:'):
        try: selected_heat_id = int(target.split(':', 1)[1])
        except: pass
        if selected_heat_id:
            h = conn.execute('SELECT * FROM heats WHERE id = ? AND competition_id = ?', (selected_heat_id, cid)).fetchone()
            if h and h['status'] in ('running', 'finished'):
                details = _build_results_detail(conn, h['id'], include_auto=include_auto)
                info = {'mode': 'heat', 'heat': dict(h)}
    elif target.startswith('cat:'):
        try: selected_cat_id = int(target.split(':', 1)[1])
        except: pass
        if selected_cat_id:
            h = conn.execute('''SELECT h.* FROM heats h JOIN heat_categories hc ON hc.heat_id = h.id WHERE hc.category_id = ? AND h.competition_id = ? AND h.status IN ('running','finished') ORDER BY h.id DESC LIMIT 1''', (selected_cat_id, cid)).fetchone()
            if h:
                cat = conn.execute('SELECT * FROM categories WHERE id = ?', (selected_cat_id,)).fetchone()
                details = _build_results_detail(conn, h['id'], cat_filter_id=selected_cat_id, include_auto=include_auto)
                info = {'mode': 'category', 'heat': dict(h), 'category': dict(cat) if cat else None}
    return competition, heats, categories, details, info, selected_heat_id, selected_cat_id

def _results_response(conn, cid, target, include_auto, public_view=False):
    """Страница результатов для указанной базы (используется и публично)."""
    # без выбора показываем последний старт — публичная страница должна быть сразу осмысленной
    if not target:
        last = conn.execute(
            "SELECT id FROM heats WHERE competition_id = ? "
            "ORDER BY (finished_at IS NULL), finished_at DESC, id DESC LIMIT 1", (cid,)).fetchone()
        if last: target = f'heat:{last["id"]}'
    competition, heats, categories, details, info, selected_heat_id, selected_cat_id = _prepare_results_data(conn, cid, target, include_auto)

    public_url = _public_url()
    return render_template('results.html', competition=competition, heats=heats, categories=categories,
                           details=details, info=info, target=target, include_auto=include_auto,
                           selected_heat_id=selected_heat_id, selected_cat_id=selected_cat_id,
                           qr_svg=_qr_svg(public_url), public_url=public_url,
                           public_view=public_view)

def _read_log_tail(path, lines=40):
    """Последние строки лог-файла (без чтения всего файла)."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 16384))
            data = f.read()
        return data.splitlines()[-lines:]
    except Exception:
        return []

def _save_setting(conn, key, val): set_cached_setting(conn, key, val)

def _auto_redirect():
    """Возврат туда, откуда пришли: на страницу устройства или на общую."""
    cid = request.form.get('cid', type=int)
    if request.form.get('back') == 'device':
        return redirect(url_for('auto_device_page', cid=cid) if cid else url_for('auto_device_page'))
    return redirect(url_for('auto_page', cid=cid) if cid else url_for('auto_page'))

def _get_auto_rows(conn, hid):
    races = conn.execute('''
        SELECT r.*, p.number, p.full_name
        FROM races r
        JOIN participants p ON p.id = r.participant_id
        WHERE r.heat_id = ?
          AND r.status != 'dns'
          AND EXISTS (SELECT 1 FROM bib_tags bt
                      WHERE bt.competition_id = r.competition_id AND bt.number = p.number)
        ORDER BY p.number
    ''', (hid,)).fetchall()

    rows = []
    for r in races:
        rd = dict(r); start = datetime.fromisoformat(rd['start_time']); rd['manual_laps'], rd['auto_laps'], rd['compare'] = [], [], []
        prev_dt = start
        for ml in conn.execute('SELECT * FROM laps WHERE race_id = ? ORDER BY lap_number', (r['id'],)).fetchall():
            dt = datetime.fromisoformat(ml['lap_time'])
            rd['manual_laps'].append({'lap': ml['lap_number'], 'time': format_secs_short((dt-start).total_seconds()), 'split': format_secs_short((dt-prev_dt).total_seconds()), 'user': ml['user_login'], '_dt': dt})
            prev_dt = dt
        prev_dt = start
        for al in conn.execute('SELECT * FROM auto_laps WHERE race_id = ? ORDER BY lap_number', (r['id'],)).fetchall():
            dt = datetime.fromisoformat(al['scan_time'])
            rd['auto_laps'].append({'lap': al['lap_number'], 'time': format_secs_short((dt-start).total_seconds()), 'split': format_secs_short((dt-prev_dt).total_seconds()), '_dt': dt})
            prev_dt = dt

        for i in range(max(len(rd['manual_laps']), len(rd['auto_laps']))):
            m = rd['manual_laps'][i] if i < len(rd['manual_laps']) else None
            a = rd['auto_laps'][i] if i < len(rd['auto_laps']) else None
            diff_sec = (a['_dt'] - m['_dt']).total_seconds() if (m and a) else None
            rd['compare'].append({
                'lap': i+1,
                'manual': m['time'] if m else '—',
                'auto': a['time'] if a else '—',
                'diff': f'{diff_sec:+.1f}' if diff_sec is not None else '',
                'diff_sec': diff_sec,
                'user': m.get('user') if m else None,
            })

        rd['last_scan_at'] = rd['auto_laps'][-1]['_dt'].strftime('%H:%M:%S') if rd['auto_laps'] else None

        for x in rd['manual_laps'] + rd['auto_laps']:
            x.pop('_dt', None)
        rows.append(rd)
    return rows

def _proto_time(sec):
    """Время в формате протокола: ММ:СС,сс или Ч:ММ:СС,сс."""
    if sec is None: return ''
    cs = int(round(max(0.0, float(sec)) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, frac = divmod(rem, 100)
    if h: return f'{h}:{m:02d}:{s:02d},{frac:02d}'
    return f'{m:02d}:{s:02d},{frac:02d}'

def _build_protocol(conn, cid):
    """Данные итогового протокола по всему соревнованию."""
    s = {k: get_cached_setting(k, '') for k in PROTOCOL_DEFAULTS}

    # дата и время — из фактических стартов, а не из ручных полей
    first_start = conn.execute("SELECT MIN(started_at) FROM heats WHERE competition_id = ? AND started_at IS NOT NULL",
                               (cid,)).fetchone()[0]
    last_finish = conn.execute("SELECT MAX(finished_at) FROM heats WHERE competition_id = ? AND finished_at IS NOT NULL",
                               (cid,)).fetchone()[0]

    def fmt_date(iso):
        if not iso: return s.get('proto_date') or ''
        return f'{iso[8:10]}.{iso[5:7]}.{iso[0:4]}'

    def fmt_hm(iso):
        return iso[11:16] if iso else ''

    date_display = fmt_date(first_start)
    start_display = fmt_hm(first_start) or (s.get('proto_start_time') or '')
    finish_display = fmt_hm(last_finish)

    cats = [dict(c) for c in conn.execute('SELECT * FROM categories WHERE competition_id = ? ORDER BY name', (cid,)).fetchall()]
    uncategorized = _scalar(conn, 'SELECT COUNT(*) FROM participants WHERE competition_id = ? AND category_id IS NULL', cid)
    if uncategorized:
        cats.append({'id': 0, 'name': 'Без категории', 'laps_total': 1, 'lap_length': None,
                     'route_length': None, 'hd': None, 'ms': None, 'ts': None})
    blocks = []
    for cat in cats:
        # маршрут = круг × кругов
        lap = _to_float(cat['lap_length'])
        route = round(lap * (cat['laps_total'] or 0), 1) if lap else None
        cat_distance = route or _to_float(s.get('proto_route_length'))
        route_display = f'{route:.1f}'.replace('.', ',') if route else ''
        if cat['id']:
            zayav = _scalar(conn, 'SELECT COUNT(*) FROM participants WHERE competition_id = ? AND category_id = ?',
                            cid, cat['id'])
            races = conn.execute(
                'SELECT r.*, p.number, p.full_name, p.birth_year, p.gender, p.city, p.team '
                'FROM races r JOIN participants p ON p.id = r.participant_id '
                'WHERE r.competition_id = ? AND p.category_id = ? ORDER BY p.number',
                (cid, cat['id'])).fetchall()
        else:
            zayav = uncategorized
            races = conn.execute(
                'SELECT r.*, p.number, p.full_name, p.birth_year, p.gender, p.city, p.team '
                'FROM races r JOIN participants p ON p.id = r.participant_id '
                'WHERE r.competition_id = ? AND p.category_id IS NULL ORDER BY p.number',
                (cid,)).fetchall()
        race_ids = [r['id'] for r in races]
        laps_map = _load_laps_by_race(conn, race_ids)
        auto_map = _load_auto_laps_by_race(conn, race_ids)
        heat_ids = {r['heat_id'] for r in races if r['heat_id']}
        min_map = {}
        if heat_ids:
            ph = ','.join('?' * len(heat_ids))
            for hh in conn.execute(f'SELECT id, min_lap_time FROM heats WHERE id IN ({ph})', tuple(heat_ids)).fetchall():
                min_map[hh['id']] = hh['min_lap_time'] or 0
        prepared, dns_count = [], 0
        for r in races:
            if r['status'] == 'dns':
                dns_count += 1
                continue
            prepared.append(prepare_race(r, conn, laps_map.get(r['id'], []), auto_map.get(r['id'], []),
                                         min_map.get(r['heat_id'], 0), include_auto=True))
        prepared.sort(key=lambda r: (0 if r['status'] == 'ok' else 1, r.get('sort_time', float('inf')), r['number']))

        place, winner = 0, None
        for r in prepared:
            r['start_display'] = (r['start_time'] or '')[11:19]
            r['finish_display'] = (r.get('finish_time') or '')[11:19] if r['status'] == 'ok' else ''
            r['laps_display'] = str(r.get('planned_laps') or r.get('laps_total') or 1)
            if r['status'] == 'ok':
                place += 1
                r['place'] = place
                if winner is None: winner = r['sort_time']
                r['time_display'] = _proto_time(r['sort_time'])
                r['gap_display'] = '' if place == 1 else _proto_time(r['sort_time'] - winner)
                r['speed_display'] = _speed_kmh(r['sort_time'], cat_distance)
            else:
                r['place'] = ''
                r['time_display'] = ''
                r['gap_display'] = ''
                r['speed_display'] = ''

        counts = _count_races(prepared)
        blocks.append({
            'category': cat,
            'races': prepared,
            'route_display': route_display,
            'stats': {
                'zayav': zayav,
                'start': counts['total'],
                'dns': dns_count,
                'finish': counts['ok'],
                'dnf': counts['dnf'] + counts['lapped'],
            },
        })

    totals = {
        'zayav': _scalar(conn, 'SELECT COUNT(*) FROM participants WHERE competition_id = ?', cid),
        'start': _scalar(conn, "SELECT COUNT(*) FROM races WHERE competition_id = ? AND status != 'dns'", cid),
        'dns': _scalar(conn, "SELECT COUNT(*) FROM races WHERE competition_id = ? AND status = 'dns'", cid),
        'finish': _scalar(conn, "SELECT COUNT(*) FROM races WHERE competition_id = ? AND status = 'ok'", cid),
        'dnf': _scalar(conn, "SELECT COUNT(*) FROM races WHERE competition_id = ? AND status IN ('dnf','lapped')", cid),
        'dsk': 0,
    }
    return {'settings': s, 'blocks': blocks, 'totals': totals,
            'format': _comp_format(conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()),
            'date_display': date_display, 'start_display': start_display, 'finish_display': finish_display}

def _http_json(url, timeout=10):
    """GET JSON из открытого API (stdlib, без внешних зависимостей)."""
    import json
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': 'chrono-protocol/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))

def _geocode_city(city):
    """Координаты по названию города (Open-Meteo geocoding)."""
    import urllib.parse
    url = ('https://geocoding-api.open-meteo.com/v1/search?name='
           + urllib.parse.quote(city) + '&count=1&language=ru&format=json')
    results = _http_json(url).get('results') or []
    if not results: return None
    return float(results[0]['latitude']), float(results[0]['longitude'])

def _fetch_weather(lat, lon, date_iso, hour_start, hour_end):
    """Погода на дату гонки по координатам (Open-Meteo).

    Пробует подходящий по дате источник, при неудаче — второй.
    """
    from datetime import date as _date
    try: target = _date.fromisoformat(date_iso)
    except Exception: target = _date.today()
    delta = (target - _date.today()).days
    hosts = (['forecast', 'archive'] if -1 <= delta <= 16 else ['archive', 'forecast'])
    base_map = {
        'forecast': 'https://api.open-meteo.com/v1/forecast',
        'archive': 'https://archive-api.open-meteo.com/v1/archive',
    }
    last_error = None
    for host in hosts:
        url = (f'{base_map[host]}?latitude={lat}&longitude={lon}'
               f'&hourly=temperature_2m,relative_humidity_2m,weather_code'
               f'&start_date={target.isoformat()}&end_date={target.isoformat()}&timezone=auto')
        try:
            hourly = (_http_json(url, timeout=15) or {}).get('hourly') or {}
        except Exception as ex:
            last_error = ex
            continue
        times = hourly.get('time') or []
        if not times:
            last_error = RuntimeError('нет данных за выбранную дату')
            continue

        def pick(hour, hourly=hourly, times=times):
            hour = max(0, min(23, int(hour)))
            idx = next((i for i, t in enumerate(times) if t[11:13] == f'{hour:02d}'), None)
            if idx is None:
                idx = min(range(len(times)), key=lambda i: abs(int(times[i][11:13]) - hour))
            code = hourly['weather_code'][idx]
            temp = hourly['temperature_2m'][idx]
            hum = hourly['relative_humidity_2m'][idx]
            return WMO_RU.get(code, f'код {code}'), (f'{temp:+.0f}' if temp is not None else ''), hum

        ws, ts, hum = pick(hour_start)
        we, te, _ = pick(hour_end)
        return {'start': ws, 'end': we, 'temp_start': ts, 'temp_end': te,
                'humidity': '' if hum is None else str(int(hum))}
    raise RuntimeError(last_error or 'сервис погоды недоступен')

def _build_dashboard_heats(conn, cid):
    """Идущие старты с подготовленными заездами и счётчиками для дашборда."""
    return [_build_heat_view(conn, h)
            for h in conn.execute("SELECT * FROM heats WHERE competition_id = ? AND status = 'running' "
                                  "ORDER BY heat_number", (cid,)).fetchall()]

def _finish_screen_response(conn, data_url=None):
    """Экран финиша для базы подключения (публично и внутри)."""
    competition = _get_competition(conn)
    public_url = _public_url()
    return render_template('finish_screen.html', competition=competition, cid=competition['id'],
                           qr_svg=_qr_svg(public_url), public_url=public_url,
                           data_url=data_url or url_for('finish_screen_data'))

def _finish_screen_data_response(conn, cid):
    """Табло: текущий старт (или последний завершённый) с разбивкой по категориям."""
    h = conn.execute("SELECT * FROM heats WHERE competition_id = ? AND status = 'running' "
                     "ORDER BY heat_number DESC LIMIT 1", (cid,)).fetchone()
    if not h:
        h = conn.execute("SELECT * FROM heats WHERE competition_id = ? AND status = 'finished' "
                         "ORDER BY finished_at DESC LIMIT 1", (cid,)).fetchone()
    if not h:
        return render_template('_finish_screen_data.html', heat=None, categories=[])

    view = _build_heat_view(conn, h)
    order = {'ok': 0, 'running': 1, 'lapped': 2, 'dnf': 3}
    for g in view['categories']:
        g['races'].sort(key=lambda r: (order.get(r['status'], 5),
                                       r.get('sort_time', float('inf')), r['number']))
    return render_template('_finish_screen_data.html', heat=view, categories=view['categories'])

def _hub_data(conn, entry):
    """Данные публичного хаба соревнования."""
    comp = conn.execute('SELECT * FROM competitions LIMIT 1').fetchone()
    if not comp:
        return None
    cid = comp['id']
    counts = _heat_participant_counts(conn, cid)
    heats = []
    for h in conn.execute('SELECT * FROM heats WHERE competition_id = ? ORDER BY heat_number', (cid,)).fetchall():
        c = counts.get(h['id'], {'total': 0, 'active': 0})
        heats.append({
            'id': h['id'],
            'number': h['heat_number'],
            'name': h['name'] or '',
            'status': h['status'],
            'total': c['active'],
            'has_protocol': True,
        })
    first = conn.execute(
        'SELECT MIN(started_at) FROM heats WHERE competition_id = ? AND started_at IS NOT NULL',
        (cid,)).fetchone()[0]
    date_display = ''
    if first:
        try:
            date_display = datetime.fromisoformat(first).strftime('%d.%m.%Y')
        except (TypeError, ValueError):
            date_display = ''
    return {
        'slug': entry.get('slug'),
        'name': comp['name'],
        'date_display': date_display,
        'heats': heats,
        'registration': get_cached_setting('reg_enabled', '0') == '1',
    }
