# -*- coding: utf-8 -*-
"""auto: маршруты (регистрируются в app.py)."""

import io
import json
import os
import re
import shutil
import sqlite3
import secrets
from datetime import datetime, timedelta

from flask import (flash, jsonify, redirect, render_template, request, send_file,
                   send_from_directory, session, url_for, Response, current_app,
                   has_request_context, abort, make_response)
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash, check_password_hash
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from flask import flash, redirect, render_template, request, send_file, url_for
from chrono.db import (LOG_DIR, _get_competition, get_db)
from chrono.settings import (RFID_DEFAULTS, get_cached_setting)
from chrono.utils import (format_secs_short)
from chrono.results_helpers import (_auto_redirect, _get_auto_rows, _read_log_tail, _save_setting)
from chrono.rfid import (_auto_log, _auto_log_lock, _swrfid_status, _swrfid_status_lock)


def auto_page():
    conn = get_db()
    try:
        competition = _get_competition(conn)
        cid = competition['id']
        heats, rows, selected_heat_id, selected_heat = [], [], None, None
        if competition:
            heats = conn.execute("""
                SELECT h.*,
                  (SELECT COUNT(*) FROM heat_participants hp
                     WHERE hp.heat_id = h.id AND hp.status = 'active') AS active_count,
                  (SELECT COUNT(*) FROM heat_participants hp
                     JOIN participants p ON p.id = hp.participant_id
                     WHERE hp.heat_id = h.id AND hp.status = 'active'
                       AND EXISTS (SELECT 1 FROM bib_tags bt
                                   WHERE bt.competition_id = p.competition_id AND bt.number = p.number)) AS tagged_count
                FROM heats h
                WHERE h.competition_id = ? AND h.status IN ('running','finished','pending')
                ORDER BY h.heat_number DESC
            """, (cid,)).fetchall()
            hid = request.args.get('hid', type=int)
            if not hid and heats: hid = heats[0]['id']
            selected_heat = next((h for h in heats if h['id'] == hid), None)
            if selected_heat:
                selected_heat_id = hid
                rows = _get_auto_rows(conn, hid)
        settings = {k: get_cached_setting(k, RFID_DEFAULTS[k]) for k in RFID_DEFAULTS}
        with _swrfid_status_lock: swrfid_snapshot = dict(_swrfid_status)
        with _auto_log_lock: auto_log_snapshot = list(_auto_log)
        return render_template('auto.html', competition=competition, heats=heats, rows=rows,
                               selected_heat_id=selected_heat_id, selected_heat=selected_heat, settings=settings,
                               swrfid_status=swrfid_snapshot, auto_log=auto_log_snapshot)
    finally: conn.close()


def auto_device_page():
    settings = {k: get_cached_setting(k, RFID_DEFAULTS[k]) for k in RFID_DEFAULTS}
    with _swrfid_status_lock: swrfid_snapshot = dict(_swrfid_status)
    cid = request.args.get('cid', type=int)
    ports = []
    try:
        from serial.tools import list_ports
        ports = sorted({p.device for p in list_ports.comports()})
    except Exception:
        ports = []
    return render_template('auto_device.html', settings=settings, swrfid_status=swrfid_snapshot,
                           ports=ports, cid=cid, swrfid_available=SWRFID_AVAILABLE,
                           log_tail=_read_log_tail(os.path.join(LOG_DIR, 'swrfid.log'), 40))


def auto_log():
    with _auto_log_lock:
        return render_template('_auto_log.html', auto_log=list(_auto_log))


def auto_detail_rows():
    hid = request.args.get('hid', type=int)
    if not hid: return '', 400
    conn = get_db()
    try:
        return render_template('_auto_details.html', rows=_get_auto_rows(conn, hid))
    finally: conn.close()


def auto_lap_compare():
    """Подробное сравнение по кругам — подгружается при раскрытии блока."""
    hid = request.args.get('hid', type=int)
    if not hid: return '', 400
    conn = get_db()
    try:
        return render_template('_auto_compare.html', rows=_get_auto_rows(conn, hid))
    finally: conn.close()


def swrfid_settings_save():
    cid = request.form.get('cid', type=int); port = request.form.get('swrfid_port', '').strip(); baud = request.form.get('swrfid_baud', '').strip()
    if not port or not baud.isdigit(): flash('Некорректный порт или baudrate', 'error')
    else:
        conn = get_db()
        try:
            with conn: _save_setting(conn, 'swrfid_port', port); _save_setting(conn, 'swrfid_baud', baud)
            flash('Настройки SWRFID сохранены', 'success')
        finally: conn.close()
    return _auto_redirect()


def swrfid_toggle():
    cid = request.form.get('cid', type=int); conn = get_db()
    try:
        new_val = '0' if get_cached_setting('swrfid_enabled', '0') == '1' else '1'
        with conn: _save_setting(conn, 'swrfid_enabled', new_val)

        if new_val == '0':
            with _swrfid_status_lock:
                _swrfid_status['connected'] = False
                _swrfid_status['last_error'] = None

        flash('SWRFID ' + ('включён' if new_val == '1' else 'выключен'), 'success')
    finally: conn.close()
    return _auto_redirect()


def auto_heat_toggle(hid):
    cid = request.form.get('cid', type=int); conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if h:
            with conn: conn.execute('UPDATE heats SET auto_enabled = ? WHERE id = ?', (0 if h['auto_enabled'] else 1, hid))
            flash(f'Автохронометраж старта №{h["heat_number"]} переключён', 'success')
    finally: conn.close()
    return redirect(url_for('auto_page', cid=cid, hid=hid) if cid else url_for('auto_page'))


def auto_export():
    conn = get_db()
    try:
        cid = request.args.get('cid', type=int); hid = request.args.get('hid', type=int)
        if not cid or not hid: flash('Не выбраны параметры', 'error'); return redirect(url_for('auto_page'))
        races = conn.execute('SELECT r.*, p.number, p.full_name FROM races r JOIN participants p ON p.id = r.participant_id WHERE r.heat_id = ? AND EXISTS (SELECT 1 FROM bib_tags bt WHERE bt.competition_id = r.competition_id AND bt.number = p.number) ORDER BY p.number', (hid,)).fetchall()
        if not races: flash('Нет данных', 'error'); return redirect(url_for('auto_page', cid=cid, hid=hid))
        wb = Workbook(); ws = wb.active; ws.title = 'Сравнение'
        max_laps = 1
        for r in races:
            max_laps = max(max_laps, conn.execute('SELECT COUNT(*) FROM auto_laps WHERE race_id = ?', (r['id'],)).fetchone()[0], conn.execute('SELECT COUNT(*) FROM laps WHERE race_id = ?', (r['id'],)).fetchone()[0])
        ws.append(['№', 'ФИО'] + sum([[f'Круг {i} (ручной)', f'Круг {i} (авто)', f'Разница {i}', f'Судья {i}'] for i in range(1, max_laps + 1)], []))
        for col in range(1, len(ws[1]) + 1): ws.cell(row=1, column=col).font = Font(bold=True); ws.column_dimensions[get_column_letter(col)].width = 18
        ws.column_dimensions['B'].width = 32
        for r in races:
            start = datetime.fromisoformat(r['start_time']); row_vals = [r['number'], r['full_name']]
            manual = conn.execute('SELECT * FROM laps WHERE race_id = ? ORDER BY lap_number', (r['id'],)).fetchall()
            auto = conn.execute('SELECT * FROM auto_laps WHERE race_id = ? ORDER BY lap_number', (r['id'],)).fetchall()
            for i in range(max_laps):
                m = manual[i] if i < len(manual) else None; a = auto[i] if i < len(auto) else None
                row_vals.append(format_secs_short((datetime.fromisoformat(m['lap_time'])-start).total_seconds()) if m else '')
                row_vals.append(format_secs_short((datetime.fromisoformat(a['scan_time'])-start).total_seconds()) if a else '')
                row_vals.append(f"{(datetime.fromisoformat(a['scan_time'])-datetime.fromisoformat(m['lap_time'])).total_seconds():+.1f}" if m and a else '')
                row_vals.append(m['user_login'] if m and m['user_login'] else '')
            ws.append(row_vals)
        buf = io.BytesIO(); wb.save(buf); buf.seek(0)
        return send_file(buf, as_attachment=True, download_name=f'auto_compare_heat_{hid}.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    finally: conn.close()


RULES = [
    ('/auto', 'auto_page', auto_page, ['GET']),
    ('/auto/device', 'auto_device_page', auto_device_page, ['GET']),
    ('/auto/log', 'auto_log', auto_log, ['GET']),
    ('/auto/detail_rows', 'auto_detail_rows', auto_detail_rows, ['GET']),
    ('/auto/lap_compare', 'auto_lap_compare', auto_lap_compare, ['GET']),
    ('/auto/swrfid/settings', 'swrfid_settings_save', swrfid_settings_save, ['POST']),
    ('/auto/swrfid/toggle', 'swrfid_toggle', swrfid_toggle, ['POST']),
    ('/auto/heat_toggle/<int:hid>', 'auto_heat_toggle', auto_heat_toggle, ['POST']),
    ('/auto/export', 'auto_export', auto_export, ['GET']),
]
