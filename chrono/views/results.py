# -*- coding: utf-8 -*-
"""Маршруты результатов, протокола, итогов и журнала."""

import io
import re
from datetime import datetime

from flask import flash, make_response, redirect, render_template, request, send_file, url_for
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from chrono.db import (_create_timing_log, _get_competition, get_db)
from chrono.pdf import (_render_protocol_pdf)
from chrono.settings import (set_cached_setting)
from chrono.timing import (_build_results_detail)
from chrono.catalog import (_to_float)
from chrono.results_helpers import (
    _build_protocol, _fetch_weather, _geocode_city, _prepare_results_data, _results_response,
    _save_proto_settings,
)


def results_page():
    conn = get_db()
    try:
        comp = _get_competition(conn)
        return _results_response(conn, comp['id'],
                                 request.args.get('target', '').strip(),
                                 request.args.get('include_auto', '1') == '1')
    finally:
        conn.close()


def results_details():
    cid = request.args.get('cid', type=int); target = request.args.get('target', '').strip()
    include_auto = request.args.get('include_auto', '1') == '1'
    if not cid or not target: return '', 400
    conn = get_db()
    try:
        _, _, _, details, info, _, _ = _prepare_results_data(conn, cid, target, include_auto)
        resp = make_response(render_template('_results_details.html', details=details))
        heat = (info or {}).get('heat') or {}
        if heat.get('status'):
            resp.headers['X-Heat-Status'] = heat['status']
        return resp
    finally: conn.close()


def results_export():
    conn = get_db()
    try:
        cid = request.args.get('cid', type=int); target = request.args.get('target', '').strip()
        include_auto = request.args.get('include_auto', '1') == '1'
        if not cid or not target: flash('Не выбраны параметры', 'error'); return redirect(url_for('results_page'))
        hid, cat_id = None, None
        if target.startswith('heat:'):
            try: hid = int(target.split(':', 1)[1])
            except: pass
        elif target.startswith('cat:'):
            try:
                cat_id = int(target.split(':', 1)[1])
                h = conn.execute('SELECT h.id FROM heats h JOIN heat_categories hc ON hc.heat_id = h.id WHERE hc.category_id = ? AND h.competition_id = ? AND h.status IN ("running","finished") ORDER BY h.id DESC LIMIT 1', (cat_id, cid)).fetchone()
                if h: hid = h['id']
            except: pass
        if not hid: flash('Данные не найдены', 'error'); return redirect(url_for('results_page', cid=cid, target=target))
        details = _build_results_detail(conn, hid, cat_filter_id=cat_id, include_auto=include_auto)
        comp = conn.execute('SELECT name FROM competitions WHERE id = ?', (cid,)).fetchone()
        heat = conn.execute('SELECT heat_number, name FROM heats WHERE id = ?', (hid,)).fetchone()
        heat_name = f"Старт_{heat['heat_number']}" + (f"_{heat['name']}" if heat['name'] else '')
        if not details: flash('Нет данных', 'error'); return redirect(url_for('results_page', cid=cid, target=target))
        wb = Workbook(); wb.remove(wb.active)
        for block in details:
            safe_title = re.sub(r'[\\/*?:\[\]]', '', block['category']['name'])[:31] or "Без категории"
            ws = wb.create_sheet(title=safe_title)
            is_itt = block.get('format') == 'itt'
            if is_itt:
                headers = ["Место", "№", "ФИО", "Год рожд.", "Пол", "Круги", "Старт", "Финиш", "Время", "Отставание"]
            else:
                headers = ["№", "ФИО", "Год рожд.", "Пол"] + [f"Круг {i}" for i in range(1, block['laps_total'] + 1)] + ["Итог", "Отставание"]
            ws.append(headers)
            for col in range(1, len(headers) + 1): ws.cell(row=1, column=col).font = Font(bold=True); ws.column_dimensions[get_column_letter(col)].width = 16
            ws.column_dimensions['B' if not is_itt else 'C'].width = 32
            for row_data in block['rows']:
                r = row_data['race']
                if is_itt:
                    row_vals = [row_data.get('place') if r['status'] == 'ok' else '',
                                r['number'], r['full_name'], r['birth_year'] or '',
                                'М' if r['gender'] == 'M' else ('Ж' if r['gender'] == 'F' else ''),
                                f"{row_data.get('laps_done', 0)} / {row_data.get('laps_total', 1)}",
                                row_data.get('start_display') or '', row_data.get('finish_display') or '',
                                row_data['total_display'] or '', row_data['gap_display'] or '']
                    ws.append(row_vals)
                    continue
                row_vals = [r['number'], r['full_name'], r['birth_year'] or '', 'М' if r['gender'] == 'M' else ('Ж' if r['gender'] == 'F' else '')]
                for i in range(block['laps_total']):
                    cell_data = row_data['laps'][i]
                    if cell_data:
                        src = 'AUTO' if cell_data['source'] == 'A' else (cell_data.get('user') or 'M')
                        row_vals.append(f"{cell_data['cumulative']} ({cell_data['split']}) [{src}]")
                    else:
                        row_vals.append("")
                row_vals.extend([row_data['total_display'] or '', row_data['gap_display'] or '']); ws.append(row_vals)
        buf = io.BytesIO(); wb.save(buf); buf.seek(0)
        filename = re.sub(r'[\\/*?:<>|"]', '_', f"{comp['name']}_{heat_name}.xlsx")
        return send_file(buf, as_attachment=True, download_name=filename, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    finally: conn.close()


def logs_page():
    conn = get_db()
    try:
        _create_timing_log(conn)
        comp = _get_competition(conn)
        cid = comp['id']
        source = request.args.get('source', '').strip()
        hid = request.args.get('hid', type=int)
        only_errors = request.args.get('errors') == '1'
        page = max(1, request.args.get('page', type=int) or 1)
        per_page = 100

        where, params = [], []
        if cid:
            where.append('competition_id = ?'); params.append(cid)
        if hid:
            where.append('heat_id = ?'); params.append(hid)
        if source in ('manual', 'auto'):
            where.append('source = ?'); params.append(source)
        if only_errors:
            where.append('ok = 0')
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        heats = conn.execute('SELECT id, heat_number, name FROM heats WHERE competition_id = ? ORDER BY heat_number DESC',
                             (cid,)).fetchall()

        total = conn.execute(f'SELECT COUNT(*) FROM timing_log {clause}', params).fetchone()[0]
        errors_total = conn.execute(
            f'SELECT COUNT(*) FROM timing_log {clause}'
            + (' AND ok = 0' if clause else ' WHERE ok = 0'), params).fetchone()[0]
        rows = conn.execute(f'SELECT * FROM timing_log {clause} ORDER BY id DESC LIMIT ? OFFSET ?',
                            params + [per_page, (page - 1) * per_page]).fetchall()
        pages = max(1, (total + per_page - 1) // per_page)
        return render_template('logs.html', rows=rows, total=total, heats=heats, hid=hid,
                               errors_total=errors_total, source=source,
                               only_errors=only_errors, page=page, pages=pages)
    finally: conn.close()


def weather_fetch():
    """Подтягивает погоду на дату гонки по городу или координатам."""
    conn = get_db()
    try:
        with conn:
            _save_proto_settings(conn)

        lat = _to_float(request.form.get('proto_weather_lat'))
        lon = _to_float(request.form.get('proto_weather_lon'))
        city = (request.form.get('proto_weather_city') or '').strip()
        if (lat is None or lon is None) and city:
            try:
                geo = _geocode_city(city)
                if geo: lat, lon = geo
            except Exception:
                pass
        if lat is None or lon is None:
            flash('Укажите город или координаты — точку определить не удалось', 'error')
            return redirect(url_for('competitions_list'))

        comp = _get_competition(conn)
        first = conn.execute("SELECT MIN(started_at) FROM heats WHERE competition_id = ? AND started_at IS NOT NULL",
                             (comp['id'],)).fetchone()[0]
        last = conn.execute("SELECT MAX(finished_at) FROM heats WHERE competition_id = ? AND finished_at IS NOT NULL",
                            (comp['id'],)).fetchone()[0]
        date_iso = first[:10] if first else datetime.now().strftime('%Y-%m-%d')
        hour_start = int(first[11:13]) if first else 12
        hour_end = int(last[11:13]) if last else hour_start

        try:
            wx = _fetch_weather(lat, lon, date_iso, hour_start, hour_end)
        except Exception as ex:
            flash(f'Не удалось получить погоду: {ex}', 'error')
            return redirect(url_for('competitions_list'))

        with conn:
            set_cached_setting(conn, 'proto_weather_start', wx['start'])
            set_cached_setting(conn, 'proto_weather_end', wx['end'])
            set_cached_setting(conn, 'proto_temp_start', wx['temp_start'])
            set_cached_setting(conn, 'proto_temp_end', wx['temp_end'])
            set_cached_setting(conn, 'proto_humidity', wx['humidity'])
        flash(f'Погода на {date_iso}: {wx["start"]}, {wx["temp_start"]}, влажность {wx["humidity"]}%', 'success')
        return redirect(url_for('competitions_list'))
    finally: conn.close()


def protocol_page():
    conn = get_db()
    try:
        comp = _get_competition(conn)
        data = _build_protocol(conn, comp['id'])
        protocol_filled = bool((data['settings'].get('proto_title') or '').strip())
        return render_template('protocol.html', competition=comp,
                               protocol_filled=protocol_filled, **data)
    finally: conn.close()


def protocol_settings_save():
    conn = get_db()
    try:
        with conn:
            _save_proto_settings(conn)
        flash('Данные для протокола сохранены', 'success')
        return redirect(url_for('protocol_page'))
    finally: conn.close()


def protocol_pdf():
    """Официальный протокол одним PDF-файлом."""
    conn = get_db()
    try:
        comp = _get_competition(conn)
        data = _build_protocol(conn, comp['id'])
        pdf = _render_protocol_pdf(comp, data)
        return send_file(io.BytesIO(pdf), as_attachment=True,
                         download_name=f'протокол_{comp["name"]}.pdf'.replace(' ', '_'),
                         mimetype='application/pdf')
    finally: conn.close()


def summary_page():
    """Сводка по соревнованию: участники, старты, заезды, отметки."""
    conn = get_db()
    try:
        comp = _get_competition(conn)
        cid = comp['id']

        def one(sql, *args):
            row = conn.execute(sql, (cid,) + args).fetchone()
            return row[0] if row and row[0] is not None else 0

        s = {
            'participants': one('SELECT COUNT(*) FROM participants WHERE competition_id = ?'),
            'tagged': one("SELECT COUNT(*) FROM participants p WHERE p.competition_id = ? AND p.number IS NOT NULL "
                          "AND EXISTS (SELECT 1 FROM bib_tags bt WHERE bt.competition_id = p.competition_id "
                          "            AND bt.number = p.number)"),
            'bib_tags': one("SELECT COUNT(*) FROM bib_tags WHERE competition_id = ?"),
            'male': one("SELECT COUNT(*) FROM participants WHERE competition_id = ? AND gender = 'M'"),
            'female': one("SELECT COUNT(*) FROM participants WHERE competition_id = ? AND gender = 'F'"),
            'categories': one('SELECT COUNT(*) FROM categories WHERE competition_id = ?'),
            'empty_categories': one("SELECT COUNT(*) FROM categories c WHERE c.competition_id = ? AND NOT EXISTS (SELECT 1 FROM participants p WHERE p.category_id = c.id)"),
            'heats_pending': one("SELECT COUNT(*) FROM heats WHERE competition_id = ? AND status = 'pending'"),
            'heats_running': one("SELECT COUNT(*) FROM heats WHERE competition_id = ? AND status = 'running'"),
            'heats_finished': one("SELECT COUNT(*) FROM heats WHERE competition_id = ? AND status = 'finished'"),
            'awaiting': one("SELECT COUNT(*) FROM heat_participants hp JOIN heats h ON h.id = hp.heat_id WHERE h.competition_id = ? AND h.status = 'pending' AND hp.status = 'active'"),
            'started': one("SELECT COUNT(DISTINCT participant_id) FROM races WHERE competition_id = ? AND status != 'dns'"),
            'running': one("SELECT COUNT(*) FROM races WHERE competition_id = ? AND status = 'running'"),
            'finished': one("SELECT COUNT(*) FROM races WHERE competition_id = ? AND status = 'ok'"),
            'dnf': one("SELECT COUNT(*) FROM races WHERE competition_id = ? AND status = 'dnf'"),
            'lapped': one("SELECT COUNT(*) FROM races WHERE competition_id = ? AND status = 'lapped'"),
            'dns': one("SELECT COUNT(*) FROM races WHERE competition_id = ? AND status = 'dns'"),
            'manual_laps': one('SELECT COUNT(*) FROM laps l JOIN races r ON r.id = l.race_id WHERE r.competition_id = ?'),
            'auto_laps': one('SELECT COUNT(*) FROM auto_laps a JOIN races r ON r.id = a.race_id WHERE r.competition_id = ?'),
            'first_start': conn.execute("SELECT MIN(started_at) FROM heats WHERE competition_id = ? AND started_at IS NOT NULL", (cid,)).fetchone()[0],
            'last_finish': conn.execute("SELECT MAX(finished_at) FROM heats WHERE competition_id = ? AND finished_at IS NOT NULL", (cid,)).fetchone()[0],
            'last_mark': max(
                conn.execute('SELECT MAX(l.lap_time) FROM laps l JOIN races r ON r.id = l.race_id WHERE r.competition_id = ?', (cid,)).fetchone()[0] or '',
                conn.execute('SELECT MAX(a.scan_time) FROM auto_laps a JOIN races r ON r.id = a.race_id WHERE r.competition_id = ?', (cid,)).fetchone()[0] or '',
            ) or None,
        }
        s['heats'] = s['heats_pending'] + s['heats_running'] + s['heats_finished']
        s['marks'] = s['manual_laps'] + s['auto_laps']
        s['auto_share'] = round(s['auto_laps'] * 100 / s['marks']) if s['marks'] else 0
        s['tagged_share'] = round(s['tagged'] * 100 / s['participants']) if s['participants'] else 0
        s['started_share'] = round(s['started'] * 100 / s['participants']) if s['participants'] else 0
        s['finished_share'] = round(s['finished'] * 100 / s['started']) if s['started'] else 0
        s['unfinished'] = s['running'] + s['lapped']

        issues = [
            {'label': 'Участники без RFID-метки', 'count': s['participants'] - s['tagged'],
             'url': url_for('participants_list', cid=cid)},
            {'label': 'Пустые категории', 'count': s['empty_categories'],
             'url': url_for('categories_list', cid=cid)},
            {'label': 'Старты без участников',
             'count': one("SELECT COUNT(*) FROM heats h WHERE h.competition_id = ? AND NOT EXISTS (SELECT 1 FROM heat_participants hp WHERE hp.heat_id = h.id)"),
             'url': url_for('races_page')},
            {'label': 'Завершённые заезды без отсечек',
             'count': one("SELECT COUNT(*) FROM races r WHERE r.competition_id = ? AND r.status IN ('ok','lapped') AND NOT EXISTS (SELECT 1 FROM laps l WHERE l.race_id = r.id) AND NOT EXISTS (SELECT 1 FROM auto_laps a WHERE a.race_id = r.id)"),
             'url': url_for('races_page')},
        ]
        issues = [i for i in issues if i['count']]
        return render_template('summary.html', competition=comp, s=s, issues=issues)
    finally: conn.close()


RULES = [
    ('/results', 'results_page', results_page, ['GET']),
    ('/results/details', 'results_details', results_details, ['GET']),
    ('/results/export', 'results_export', results_export, ['GET']),
    ('/logs', 'logs_page', logs_page, ['GET']),
    ('/competition/weather/fetch', 'weather_fetch', weather_fetch, ['POST']),
    ('/protocol', 'protocol_page', protocol_page, ['GET']),
    ('/competition/protocol-settings', 'protocol_settings_save', protocol_settings_save, ['POST']),
    ('/protocol.pdf', 'protocol_pdf', protocol_pdf, ['GET']),
    ('/summary', 'summary_page', summary_page, ['GET']),
]
