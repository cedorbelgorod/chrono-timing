# -*- coding: utf-8 -*-
"""Маршруты стартов, заездов, кругов и стартового протокола."""

import io
import re
from datetime import datetime, timedelta

from flask import flash, jsonify, redirect, render_template, request, send_file, session, url_for
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from chrono.db import (_db_key, _get_competition, get_db)
from chrono.pdf import (_render_startlist_pdf)
from chrono.settings import (
    _comp_format, _heat_format, _itt_interval, get_cached_setting, set_cached_setting,
)
from chrono.timing import (
    _build_races_heats, _find_heat_by_token, _force_finish_heat, _format_sort_spec,
    _heat_sort_spec, _heat_startlist_data, _parse_sort_spec, _race_lap_counts, _start_heat,
    SORT_MODES,
)
from chrono.utils import (_fmt_start_at, _new_startlist_token, format_secs)
from chrono.catalog import (_category_participants, _parse_duration, _tags_by_number)
from chrono.manual_helpers import (_log_timing)
from chrono.results_helpers import (_base_url)


def races_page():
    conn = get_db()
    try:
        competition = _get_competition(conn)
        selected_cid = competition['id']
        categories_data, heat_data, heats_data, next_heat_number = [], None, [], 1
        has_available_categories = False
        try: _min_lap_default = float(get_cached_setting('heat_min_lap_time', '5') or 5)
        except (TypeError, ValueError): _min_lap_default = 5.0
        if _min_lap_default == int(_min_lap_default): _min_lap_default = int(_min_lap_default)
        heat_defaults = {
            'finish_on_leader': get_cached_setting('heat_finish_on_leader', '1') == '1',
            'auto_enabled': get_cached_setting('heat_auto_enabled', '1') == '1',
            'min_lap_time': _min_lap_default,
            'start_order': get_cached_setting('heat_start_order', 'number') or 'number',
        }
        if competition:
            comp_key = session.get('comp') or _db_key(conn)
            if session.get('last_comp_key') != comp_key:
                session.pop('last_action', None)
                session['last_comp_key'] = comp_key
            session.pop('last_cid', None)
            used_map = {r['category_id']: r['heat_number'] for r in conn.execute(
                'SELECT hc.category_id, h.heat_number FROM heat_categories hc JOIN heats h ON h.id = hc.heat_id WHERE h.competition_id = ?',
                (selected_cid,)).fetchall()}
            tags_by_number = _tags_by_number(conn, selected_cid)
            for c in conn.execute('SELECT * FROM categories WHERE competition_id = ? ORDER BY name', (selected_cid,)).fetchall():
                cd = dict(c)
                cd['heat_number'] = used_map.get(c['id'])
                # в старт попадают только участники с выданным номером
                cd['participants'] = [] if cd['heat_number'] else [
                    p for p in _category_participants(conn, c['id']) if p['number']]
                categories_data.append(cd)
            without_number = conn.execute(
                'SELECT COUNT(*) FROM participants WHERE competition_id = ? AND number IS NULL',
                (selected_cid,)).fetchone()[0]
            # участники без группы в стартах не показываются — сначала назначьте им группу
            next_heat_number = conn.execute('SELECT COALESCE(MAX(heat_number), 0) FROM heats WHERE competition_id = ?', (selected_cid,)).fetchone()[0] + 1
            has_available_categories = any(not c['heat_number'] and c['participants'] for c in categories_data)
            heat_data = {
                'next_number': next_heat_number,
                'categories': [{
                    'id': cd['id'], 'name': cd['name'], 'laps': cd['laps_total'],
                    'heat_number': cd['heat_number'],
                    'people': [{'id': p['id'], 'number': p['number'], 'name': p['full_name'],
                                'year': p['birth_year'],
                                'gender': {'M': 'М', 'F': 'Ж'}.get((p['gender'] or '').upper(), ''),
                                'rfid': bool(tags_by_number.get(p['number']))} for p in cd['participants']],
                } for cd in categories_data],
            }
            heats_data = _build_races_heats(conn, selected_cid)
        return render_template('races.html', competition=competition,
                               categories_data=categories_data, heat_data=heat_data,
                               next_heat_number=next_heat_number, heat_defaults=heat_defaults,
                               has_available_categories=has_available_categories,
                               without_number=without_number if competition else 0,
                               itt_interval=_itt_interval(),
                               race_format=_comp_format(competition),
                               heats=heats_data)
    finally: conn.close()


def races_data():
    """Автообновление списка стартов на странице «Старт»."""
    conn = get_db()
    try:
        comp = _get_competition(conn)
        cid = request.args.get('cid', type=int) or comp['id']
        return render_template('_heats_list.html', heats=_build_races_heats(conn, cid), competition=comp)
    finally:
        conn.close()


def heat_rename(hid):
    """Название старта. Для fetch-запроса (автосохранение) отвечает JSON."""
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if not h:
            return jsonify({'ok': False, 'message': 'Старт не найден'}), 404
        name = (request.form.get('name', '').strip()[:120]) or None
        with conn:
            conn.execute('UPDATE heats SET name = ? WHERE id = ?', (name, hid))
        if request.headers.get('X-Requested-With') == 'fetch':
            return jsonify({'ok': True, 'name': name or ''})
        flash('Название обновлено', 'success')
        return redirect(url_for('races_page', cid=h['competition_id']))
    finally: conn.close()


def _heat_label(h):
    """Название старта, а если не задано — «Старт №N»."""
    try:
        name = (h['name'] or '').strip()
    except (KeyError, IndexError, TypeError):
        name = ''
    try:
        number = h['heat_number']
    except (KeyError, IndexError, TypeError):
        number = ''
    return name or f'Старт №{number}'


def heat_create():
    cid = request.form.get('competition_id', type=int); participant_ids = request.form.getlist('participant_ids', type=int)
    finish_on_leader = 1 if request.form.get('finish_on_leader') else 0; auto_enabled = 1 if request.form.get('auto_enabled') else 0
    min_lap_time = request.form.get('min_lap_time', type=float)
    if min_lap_time is None or min_lap_time < 0: min_lap_time = 5.0
    start_order = (request.form.get('start_order') or 'number').strip()
    if start_order not in ('number', 'category', 'name'):
        start_order = 'number'
    heat_name = (request.form.get('name') or '').strip()[:120] or None
    start_now = request.form.get('start_now') == '1'
    if not cid or not participant_ids: flash('Выберите хотя бы одного участника', 'error'); return redirect(url_for('races_page', cid=cid))
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        heat_fmt = _comp_format(comp)
        if heat_fmt == 'itt':
            finish_on_leader = 0
        placeholders = ','.join('?' * len(participant_ids))
        rows = conn.execute(f'SELECT id, category_id, number, full_name FROM participants WHERE id IN ({placeholders}) AND competition_id = ?',
                            participant_ids + [cid]).fetchall()
        if not rows: flash('Участники не найдены', 'error'); return redirect(url_for('races_page', cid=cid))
        # без стартового номера участник не может ехать — номер выдаётся на старте
        no_number = [r for r in rows if not r['number']]
        if no_number:
            rows = [r for r in rows if r['number']]
            names = ', '.join(r['full_name'] for r in no_number[:3])
            tail = ' и ещё' if len(no_number) > 3 else ''
            flash(f'Без стартового номера: {len(no_number)}{tail} — {names}. '
                  f'Выдайте номер на странице «Выдать номера».', 'error')
            participant_ids = [r['id'] for r in rows]
            if not rows:
                return redirect(url_for('races_page', cid=cid))
        category_ids = sorted({r['category_id'] for r in rows if r['category_id']})
        has_no_category = any(r['category_id'] is None for r in rows)
        if category_ids:
            cat_ph = ','.join('?' * len(category_ids))
            valid_cats = conn.execute(f'SELECT id FROM categories WHERE id IN ({cat_ph}) AND competition_id = ? AND id NOT IN (SELECT hc.category_id FROM heat_categories hc JOIN heats h ON h.id = hc.heat_id WHERE h.competition_id = ?)', category_ids + [cid, cid]).fetchall()
            if len(valid_cats) != len(category_ids): flash('Некоторые категории уже в стартах', 'error'); return redirect(url_for('races_page', cid=cid))
        new_num = conn.execute('SELECT COALESCE(MAX(heat_number), 0) FROM heats WHERE competition_id = ?', (cid,)).fetchone()[0] + 1
        active_count, dns_count, selected_set = 0, 0, set(participant_ids)
        with conn:
            hid = conn.execute('INSERT INTO heats (competition_id, heat_number, name, status, finish_on_leader, min_lap_time, auto_enabled, format, start_order, start_dir) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (cid, new_num, heat_name, 'pending', finish_on_leader, min_lap_time, auto_enabled, heat_fmt, _format_sort_spec([(start_order, 'asc')] + [(m, 'asc') for m in SORT_MODES if m != start_order]), 'asc')).lastrowid
            for cat_id in category_ids: conn.execute('INSERT INTO heat_categories (heat_id, category_id) VALUES (?, ?)', (hid, cat_id))
            group_ids = list(category_ids) + ([0] if has_no_category else [])
            for cat_id in group_ids:
                for p in _category_participants(conn, cat_id):
                    st = 'active' if p['id'] in selected_set else 'dns'
                    conn.execute('INSERT INTO heat_participants (heat_id, participant_id, status) VALUES (?, ?, ?)', (hid, p['id'], st))
                    if st == 'active': active_count += 1
                    else: dns_count += 1
            # запоминаем параметры для следующего старта
            set_cached_setting(conn, 'heat_finish_on_leader', finish_on_leader)
            set_cached_setting(conn, 'heat_auto_enabled', auto_enabled)
            set_cached_setting(conn, 'heat_min_lap_time', min_lap_time)
            set_cached_setting(conn, 'heat_start_order', start_order)
        new_label = heat_name or f'Старт №{new_num}'
        if start_now:
            heat_row = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
            if _start_heat(conn, heat_row) is None:
                flash(f'«{new_label}» создан, но запускать некого', 'error')
            else:
                flash(f'«{new_label}» создан и запущен: активных {active_count}, DNS {dns_count}', 'success')
        else:
            flash(f'«{new_label}»: активных {active_count}, DNS {dns_count}', 'success')
        return redirect(url_for('races_page', cid=cid))
    finally: conn.close()


def heat_start(hid):
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if not h or h['status'] != 'pending': flash('Старт уже запущен', 'error'); return redirect(url_for('races_page', cid=h['competition_id'] if h else None))
        if _start_heat(conn, h) is None: flash('Нет участников', 'error'); return redirect(url_for('races_page', cid=h['competition_id']))
        flash(f'«{_heat_label(h)}» запущен', 'success'); return redirect(url_for('races_page', cid=h['competition_id']))
    finally: conn.close()


def heat_force_finish(hid):
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if h and h['status'] == 'running':
            dnf_ids = request.form.getlist('dnf_race_ids', type=int)
            with conn: _force_finish_heat(conn, hid, dnf_ids)
            msg = f'«{_heat_label(h)}» завершён'
            if dnf_ids: msg += f', DNF: {len(dnf_ids)}'
            flash(msg, 'success')
        return redirect(url_for('races_page', cid=h['competition_id']))
    finally: conn.close()


def heat_force_finish_and_next(hid):
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if h and h['status'] == 'running':
            dnf_ids = request.form.getlist('dnf_race_ids', type=int)
            with conn: _force_finish_heat(conn, hid, dnf_ids)
            msg = 'Старт завершён. Выберите следующих.'
            if dnf_ids: msg = f'Старт завершён (DNF: {len(dnf_ids)}). Выберите следующих.'
            flash(msg, 'success')
        return redirect(url_for('races_page', cid=h['competition_id']) + '#create-heat')
    finally: conn.close()


def heat_undns(hid):
    """Вернуть участника в ещё не запущенный старт (был DNS, но успел)."""
    pid = request.form.get('pid', type=int)
    if not pid:
        return jsonify({'ok': False, 'message': 'Не выбран участник'}), 400
    conn = get_db()
    try:
        heat = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if not heat:
            return jsonify({'ok': False, 'message': 'Старт не найден'}), 404
        if heat['status'] != 'pending':
            return jsonify({'ok': False, 'message': 'Старт уже запущен — вернуть нельзя'}), 400
        row = conn.execute(
            'SELECT status FROM heat_participants WHERE heat_id = ? AND participant_id = ?',
            (hid, pid)).fetchone()
        if not row:
            return jsonify({'ok': False, 'message': 'Участник не в этом старте'}), 404
        if row['status'] == 'active':
            return jsonify({'ok': True, 'message': 'Участник уже в старте'})
        with conn:
            conn.execute(
                "UPDATE heat_participants SET status = 'active' "
                'WHERE heat_id = ? AND participant_id = ?', (hid, pid))
            conn.execute(
                "DELETE FROM races WHERE heat_id = ? AND participant_id = ? AND status = 'dns'",
                (hid, pid))
        _log_timing(conn, 'races', 'undns', True, competition_id=heat['competition_id'], heat_id=hid,
                    participant_id=pid, user_login=session.get('user'),
                    detail='DNS снят: участник вернулся до старта')
        person = conn.execute('SELECT full_name, number FROM participants WHERE id = ?',
                              (pid,)).fetchone()
        flash(f'В старт возвращён: №{person["number"]} {person["full_name"]}', 'success')
        return jsonify({'ok': True})
    finally:
        conn.close()


def heat_mark_dnf(hid):
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if h and h['status'] == 'finished':
            dnf_rids = set(int(x) for x in request.form.getlist('dnf_race_ids', type=int))
            races = conn.execute('SELECT * FROM races WHERE heat_id = ?', (hid,)).fetchall()
            lap_counts = _race_lap_counts(conn, [r['id'] for r in races])
            with conn:
                for r in races:
                    if r['status'] == 'dns': continue
                    if r['id'] in dnf_rids: conn.execute("UPDATE races SET status = 'dnf', laps_behind = 0 WHERE id = ?", (r['id'],))
                    elif r['status'] == 'dnf':
                        laps = lap_counts[r['id']]
                        if laps >= r['laps_total']: conn.execute("UPDATE races SET status = 'ok', finish_time = ?, laps_behind = 0 WHERE id = ?", (r['finish_time'] or datetime.now().isoformat(), r['id']))
                        else: conn.execute("UPDATE races SET status = 'lapped', laps_behind = ? WHERE id = ?", (r['laps_total'] - laps, r['id']))
            flash('DNF обновлены', 'success')
        return redirect(url_for('races_page', cid=h['competition_id']))
    finally: conn.close()


def heat_delete(hid):
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if h:
            with conn:
                conn.execute('DELETE FROM auto_laps WHERE race_id IN (SELECT id FROM races WHERE heat_id = ?)', (hid,))
                conn.execute('DELETE FROM laps WHERE race_id IN (SELECT id FROM races WHERE heat_id = ?)', (hid,))
                conn.execute('DELETE FROM races WHERE heat_id = ?', (hid,))
                conn.execute('DELETE FROM heat_participants WHERE heat_id = ?', (hid,))
                conn.execute('DELETE FROM heat_categories WHERE heat_id = ?', (hid,))
                conn.execute('DELETE FROM heats WHERE id = ?', (hid,))
            flash('Старт удалён', 'success'); return redirect(url_for('races_page', cid=h['competition_id']))
        return redirect(url_for('races_page'))
    finally: conn.close()


def heat_edit_start(hid):
    """Правка фактического времени старта (автосохранение, отвечает JSON)."""
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if not h:
            return jsonify({'ok': False, 'message': 'Старт не найден'}), 404
        raw = (request.form.get('start_time') or '').strip()
        try:
            dt = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'message': 'Неверный формат даты и времени'}), 400
        with conn:
            conn.execute('UPDATE heats SET started_at = ? WHERE id = ?', (dt.isoformat(), hid))
            conn.execute("UPDATE races SET start_time = ? WHERE heat_id = ? AND status != 'dns'",
                         (dt.isoformat(), hid))
        return jsonify({'ok': True, 'display': dt.strftime('%H:%M:%S')})
    finally: conn.close()


def heat_order_save(hid):
    """Порядок участников старта (перетаскивание в карточке старта).

    Работает и для разделки, и для группового старта: в групповом это порядок
    вызова участников на старт.
    """
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if not h:
            return jsonify({'ok': False, 'message': 'Старт не найден'}), 404
        raw = request.form.get('order')
        if raw is None:
            raw = (request.get_json(silent=True) or {}).get('order')
        if isinstance(raw, (list, tuple)):
            ids = [int(x) for x in raw if str(x).strip().isdigit()]
        else:
            ids = [int(x) for x in re.split(r'[,\s]+', str(raw or '')) if x.strip().isdigit()]
        ids = list(dict.fromkeys(ids))
        if not ids:
            return jsonify({'ok': False, 'message': 'Пустой порядок'}), 400
        members = {r['participant_id'] for r in conn.execute(
            'SELECT participant_id FROM heat_participants WHERE heat_id = ?', (hid,)).fetchall()}
        if not set(ids) <= members:
            return jsonify({'ok': False, 'message': 'Участник не из этого старта'}), 400
        started = {r['participant_id'] for r in conn.execute(
            'SELECT participant_id FROM races WHERE heat_id = ?', (hid,)).fetchall()}
        if started & set(ids):
            return jsonify({'ok': False, 'message': 'Участник уже стартовал'}), 400
        with conn:
            for pos, pid in enumerate(ids, start=1):
                conn.execute('UPDATE heat_participants SET sort_order = ? '
                             'WHERE heat_id = ? AND participant_id = ?', (pos, hid, pid))
        return jsonify({'ok': True, 'saved': len(ids)})
    finally:
        conn.close()


def heat_order_reset(hid):
    """Сортировка по ключу: выбранный ключ становится главным (по возрастанию).

    Остальные ключи остаются в прежнем порядке как уточняющие.
    """
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if not h:
            return jsonify({'ok': False, 'message': 'Старт не найден'}), 404
        mode = (request.form.get('mode') or 'number').strip()
        if mode not in SORT_MODES:
            mode = 'number'
        spec = _heat_sort_spec(h)
        new_spec = [(mode, 'asc')] + [(m, d) for m, d in spec if m != mode]
        with conn:
            conn.execute('UPDATE heat_participants SET sort_order = NULL WHERE heat_id = ?', (hid,))
            conn.execute('UPDATE heats SET start_order = ?, start_dir = ? WHERE id = ?',
                         (_format_sort_spec(new_spec), 'asc', hid))
        return jsonify({'ok': True, 'mode': mode, 'spec': _format_sort_spec(new_spec)})
    finally:
        conn.close()


def heat_sort_save(hid):
    """Ключи сортировки старта: порядок чипов и направления (автосохранение)."""
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if not h:
            return jsonify({'ok': False, 'message': 'Старт не найден'}), 404
        raw = request.form.get('spec')
        if raw is None:
            raw = (request.get_json(silent=True) or {}).get('spec')
        spec = _parse_sort_spec(raw)
        with conn:
            conn.execute('UPDATE heat_participants SET sort_order = NULL WHERE heat_id = ?', (hid,))
            conn.execute('UPDATE heats SET start_order = ? WHERE id = ?',
                         (_format_sort_spec(spec), hid))
        return jsonify({'ok': True, 'spec': _format_sort_spec(spec)})
    finally:
        conn.close()


def heat_start_at_save(hid):
    """Плановое время старта первого участника."""
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if not h:
            return jsonify({'ok': False, 'message': 'Старт не найден'}), 404
        raw = (request.form.get('start_at') or '').strip()
        value = None
        if raw:
            try:
                value = datetime.fromisoformat(raw).replace(second=0, microsecond=0).isoformat()
            except ValueError:
                return jsonify({'ok': False, 'message': 'Неверный формат даты и времени'}), 400
        with conn:
            conn.execute('UPDATE heats SET start_at = ? WHERE id = ?', (value, hid))
        return jsonify({'ok': True, 'start_at': (value or '')[:16], 'display': _fmt_start_at(value)})
    finally:
        conn.close()


def startlist_page(hid):
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if not h:
            return 'Старт не найден', 404
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (h['competition_id'],)).fetchone()
        data = _heat_startlist_data(conn, h, comp)
        token = h['startlist_token'] if 'startlist_token' in h.keys() else None
        public_url = ''
        if token:
            public_url = _base_url() + url_for('startlist_public', token=token)
        return render_template('startlist.html', competition=comp, data=data, heat=h,
                               public_url=public_url)
    finally:
        conn.close()


def startlist_pdf(hid):
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if not h:
            return 'Старт не найден', 404
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (h['competition_id'],)).fetchone()
        data = _heat_startlist_data(conn, h, comp)
        pdf = _render_startlist_pdf(comp, data)
        name = f'стартовый_протокол_старт_{data["heat"]["number"]}.pdf'
        return send_file(io.BytesIO(pdf), as_attachment=True, download_name=name, mimetype='application/pdf')
    finally:
        conn.close()


def startlist_xlsx(hid):
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if not h:
            return 'Старт не найден', 404
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (h['competition_id'],)).fetchone()
        data = _heat_startlist_data(conn, h, comp)
        wb = Workbook()
        ws = wb.active
        ws.title = f'Старт {data["heat"]["number"]}'
        ws.append([comp['name'] if comp else ''])
        ws['A1'].font = Font(bold=True, size=13)
        ws.append([f'Стартовый протокол · {data["heat"]["name"] or ("старт №" + str(data["heat"]["number"]))}'])
        ws['A2'].font = Font(bold=True, size=11)
        meta = []
        if data['heat']['format'] == 'itt':
            meta.append(f'раздельный старт, интервал {data["heat"]["interval"]} с')
            if data['heat']['start_at']:
                meta.append(f'первый старт {data["heat"]["start_at"]}')
        else:
            meta.append('групповой старт')
            if data['heat']['start_at']:
                meta.append(f'старт {data["heat"]["start_at"]}')
            meta.append('№ — порядок вызова')
        meta.append(f'участников {data["total"]}')
        ws.append([' · '.join(meta)])
        ws.append([])
        headers = ['№ п/п', 'Номер', 'ФИО', 'Год', 'Категория', 'Круги', 'Город', 'Команда', 'Время старта', 'Прим.']
        ws.append(headers)
        head_row = ws.max_row
        for c in range(1, len(headers) + 1):
            ws.cell(row=head_row, column=c).font = Font(bold=True)
        for r in data['rows']:
            note = ''
            if r['status'] == 'dns':
                note = 'DNS'
            elif r['status'] == 'running':
                note = 'на дистанции'
            elif r['status'] == 'finished':
                note = 'финиш ' + (r['finish_display'] or '')
            ws.append([
                r['position'] or '',
                r['number'] if r['number'] is not None else '',
                r['full_name'],
                r['birth_year'] or '',
                r['category'],
                r.get('laps') or 1,
                r['city'],
                r['team'],
                r['actual_display'] or r['planned_display'] or '',
                note,
            ])
        widths = [7, 8, 30, 7, 20, 8, 16, 18, 13, 14]
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        name = f'стартовый_протокол_старт_{data["heat"]["number"]}.xlsx'
        return send_file(buf, as_attachment=True, download_name=name,
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    finally:
        conn.close()


def startlist_link(hid):
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if not h:
            return jsonify({'ok': False, 'message': 'Старт не найден'}), 404
        token = h['startlist_token'] if 'startlist_token' in h.keys() else None
        rotate = request.form.get('rotate') == '1'
        if not token or rotate:
            token = _new_startlist_token(conn)
            with conn:
                conn.execute('UPDATE heats SET startlist_token = ? WHERE id = ?', (token, hid))
        url = _base_url() + url_for('startlist_public', token=token)
        return jsonify({'ok': True, 'token': token, 'url': url})
    finally:
        conn.close()


def startlist_public(token):
    found = _find_heat_by_token((token or '').strip())
    if not found:
        return render_template('startlist_public.html', data={
            'competition': 'Стартовый протокол', 'heat': {'number': '', 'name': '', 'format': 'mass',
            'interval': 0, 'start_at': ''}, 'rows': [], 'total': 0, 'started': 0, 'finished': 0,
        }), 404
    conn, h, comp = found
    try:
        data = _heat_startlist_data(conn, h, comp)
        return render_template('startlist_public.html', data=data)
    finally:
        conn.close()


def lap_edit(lid):
    conn = get_db()
    try:
        lap = conn.execute('SELECT * FROM laps WHERE id = ?', (lid,)).fetchone()
        if not lap: return 'Круг не найден', 404
        race = conn.execute('SELECT * FROM races WHERE id = ?', (lap['race_id'],)).fetchone()
        cid = race['competition_id']

        new_str = (request.form.get('lap_time') or '').strip()
        try: new_dt = datetime.fromisoformat(new_str)
        except: flash('Неверный формат времени', 'error'); return redirect(url_for('races_page', cid=cid))

        if new_dt <= datetime.fromisoformat(race['start_time']): flash('Время круга должно быть позже старта', 'error'); return redirect(url_for('races_page', cid=cid))

        if lap['lap_number'] > 1:
            prev = conn.execute('SELECT lap_time FROM laps WHERE race_id = ? AND lap_number = ?', (race['id'], lap['lap_number'] - 1)).fetchone()
            if prev and new_dt <= datetime.fromisoformat(prev['lap_time']): flash('Время круга должно быть позже предыдущего', 'error'); return redirect(url_for('races_page', cid=cid))

        nxt = conn.execute('SELECT lap_time FROM laps WHERE race_id = ? AND lap_number = ?', (race['id'], lap['lap_number'] + 1)).fetchone()
        if nxt and new_dt >= datetime.fromisoformat(nxt['lap_time']): flash('Время круга должно быть раньше следующего', 'error'); return redirect(url_for('races_page', cid=cid))

        if race['finish_time'] and new_dt >= datetime.fromisoformat(race['finish_time']): flash('Время круга должно быть раньше финиша', 'error'); return redirect(url_for('races_page', cid=cid))

        editor = session.get('user')
        editor_id = session.get('user_id')
        with conn: conn.execute('UPDATE laps SET lap_time = ?, user_id = ?, user_login = ? WHERE id = ?',
                                (new_dt.isoformat(), editor_id, editor, lid))
        flash(f'Время круга обновлено (правил: {editor})', 'success'); return redirect(url_for('races_page', cid=cid))
    finally: conn.close()


def lap_delete(lid):
    conn = get_db()
    try:
        lap = conn.execute('SELECT * FROM laps WHERE id = ?', (lid,)).fetchone()
        if not lap: return 'Круг не найден', 404
        race = conn.execute('SELECT * FROM races WHERE id = ?', (lap['race_id'],)).fetchone()
        cid = race['competition_id']

        max_lap = conn.execute('SELECT MAX(lap_number) FROM laps WHERE race_id = ?', (race['id'],)).fetchone()[0]
        if lap['lap_number'] != max_lap: flash('Можно удалять только последний круг', 'error'); return redirect(url_for('races_page', cid=cid))

        had_finish = bool(race['finish_time'])
        with conn:
            conn.execute('DELETE FROM laps WHERE id = ?', (lid,))
            if had_finish:
                conn.execute("UPDATE races SET finish_time = NULL, status = 'running', laps_behind = 0 WHERE id = ?", (race['id'],))
                h = conn.execute('SELECT status FROM heats WHERE id = ?', (race['heat_id'],)).fetchone()
                if h and h['status'] == 'finished': conn.execute("UPDATE heats SET status = 'running', finished_at = NULL WHERE id = ?", (race['heat_id'],))

        flash('Круг удалён' + (' (финиш снят)' if had_finish else ''), 'success'); return redirect(url_for('races_page', cid=cid))
    finally: conn.close()


def race_edit_finish(rid):
    conn = get_db()
    try:
        r = conn.execute('SELECT * FROM races WHERE id = ?', (rid,)).fetchone()
        if not r: return 'Заезд не найден', 404
        cid = r['competition_id']
        if r['status'] != 'ok': flash('Финиш можно править только у завершённого заезда', 'error'); return redirect(url_for('races_page', cid=cid))

        new_str = (request.form.get('finish_time') or '').strip()
        elapsed_str = (request.form.get('elapsed') or '').strip()
        start_dt = datetime.fromisoformat(r['start_time'])
        if elapsed_str:
            # ввели время прохождения дистанции — финиш считаем от старта
            seconds = _parse_duration(elapsed_str)
            if seconds is None or seconds <= 0:
                flash('Неверный формат времени на дистанции', 'error'); return redirect(url_for('races_page', cid=cid))
            new_dt = start_dt + timedelta(seconds=seconds)
        else:
            try: new_dt = datetime.fromisoformat(new_str)
            except: flash('Неверный формат времени', 'error'); return redirect(url_for('races_page', cid=cid))

        if new_dt <= start_dt: flash('Финиш должен быть позже старта', 'error'); return redirect(url_for('races_page', cid=cid))

        last_lap = conn.execute('SELECT lap_time FROM laps WHERE race_id = ? ORDER BY lap_number DESC LIMIT 1', (rid,)).fetchone()
        if last_lap and new_dt <= datetime.fromisoformat(last_lap['lap_time']): flash('Финиш должен быть позже последнего круга', 'error'); return redirect(url_for('races_page', cid=cid))

        with conn: conn.execute('UPDATE races SET finish_time = ? WHERE id = ?', (new_dt.isoformat(), rid))
        total = (new_dt - start_dt).total_seconds()
        flash(f'Время финиша обновлено: {new_dt.strftime("%H:%M:%S")} (дистанция {format_secs(total)})', 'success')
        return redirect(url_for('races_page', cid=cid))
    finally: conn.close()


RULES = [
    ('/races', 'races_page', races_page, ['GET']),
    ('/races/data', 'races_data', races_data, ['GET']),
    ('/heats/<int:hid>/rename', 'heat_rename', heat_rename, ['POST']),
    ('/races/create_heat', 'heat_create', heat_create, ['POST']),
    ('/heats/<int:hid>/start', 'heat_start', heat_start, ['POST']),
    ('/heats/<int:hid>/force_finish', 'heat_force_finish', heat_force_finish, ['POST']),
    ('/heats/<int:hid>/force_finish_and_next', 'heat_force_finish_and_next', heat_force_finish_and_next, ['POST']),
    ('/heats/<int:hid>/undns', 'heat_undns', heat_undns, ['POST']),
    ('/heats/<int:hid>/mark_dnf', 'heat_mark_dnf', heat_mark_dnf, ['POST']),
    ('/heats/<int:hid>/delete', 'heat_delete', heat_delete, ['POST']),
    ('/heats/<int:hid>/edit_start', 'heat_edit_start', heat_edit_start, ['POST']),
    ('/heats/<int:hid>/order', 'heat_order_save', heat_order_save, ['POST']),
    ('/heats/<int:hid>/reset_order', 'heat_order_reset', heat_order_reset, ['POST']),
    ('/heats/<int:hid>/sort', 'heat_sort_save', heat_sort_save, ['POST']),
    ('/heats/<int:hid>/start_at', 'heat_start_at_save', heat_start_at_save, ['POST']),
    ('/heats/<int:hid>/startlist', 'startlist_page', startlist_page, ['GET']),
    ('/heats/<int:hid>/startlist.pdf', 'startlist_pdf', startlist_pdf, ['GET']),
    ('/heats/<int:hid>/startlist.xlsx', 'startlist_xlsx', startlist_xlsx, ['GET']),
    ('/heats/<int:hid>/startlist_link', 'startlist_link', startlist_link, ['POST']),
    ('/sl/<token>', 'startlist_public', startlist_public, ['GET']),
    ('/laps/<int:lid>/edit', 'lap_edit', lap_edit, ['POST']),
    ('/laps/<int:lid>/delete', 'lap_delete', lap_delete, ['POST']),
    ('/races/<int:rid>/edit_finish', 'race_edit_finish', race_edit_finish, ['POST']),
]
