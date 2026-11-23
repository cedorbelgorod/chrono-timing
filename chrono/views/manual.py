# -*- coding: utf-8 -*-
"""Маршруты ручного хронометража и табло старта."""

from datetime import datetime

from flask import flash, jsonify, redirect, render_template, request, session, url_for

from chrono.db import (_get_competition, get_db)
from chrono.settings import (_comp_format)
from chrono.timing import (
    _build_merged_laps, _manual_start_queue, _manual_start_state, _process_race_finish,
    _running_itt_heat, _start_heat, maybe_finish_heat,
)
from chrono.manual_helpers import (_get_manual_data, _log_timing, _push_manual_log)


def manual_page():
    conn = get_db()
    try:
        comp = _get_competition(conn)
        selected_cid = comp['id']
        race_fmt = _comp_format(comp)
        if race_fmt == 'itt' and (request.args.get('mode') or '').strip() == 'start':
            state = _manual_start_state(conn, selected_cid)
            return render_template('manual_start.html', competition=comp, state=state)
        competition, active_races = _get_manual_data(conn, selected_cid)
        if competition:
            if session.get('manual_cid') != selected_cid: session.pop('manual_log', None); session.pop('manual_last', None); session.pop('last_action', None); session['manual_cid'] = selected_cid
        else: session.pop('manual_log', None); session.pop('manual_last', None); session.pop('last_action', None); session.pop('manual_cid', None)
        return render_template('manual.html', competition=competition, active_races=active_races,
                               last=session.get('manual_last'), log=session.get('manual_log', []),
                               race_format=race_fmt)
    finally: conn.close()


def manual_start_data():
    """JSON для табло раздельного старта."""
    cid = request.args.get('cid', type=int)
    conn = get_db()
    try:
        if not cid:
            comp = _get_competition(conn)
            cid = comp['id']
        return jsonify(_manual_start_state(conn, cid))
    finally:
        conn.close()


def manual_start_action():
    """СТАРТ / Пропустить / отмена старта на табло разделки."""
    cid = request.form.get('competition_id', type=int)
    action = (request.form.get('action') or '').strip()
    judge = session.get('user')
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone() if cid else None
        if not comp:
            return jsonify({'ok': False, 'message': 'Соревнование не найдено'}), 404
        if _comp_format(comp) != 'itt':
            return jsonify({'ok': False, 'message': 'Это соревнование не раздельного типа'}), 400
        heat = _running_itt_heat(conn, cid)
        if not heat and action in ('reorder', 'begin'):
            heat = conn.execute(
                "SELECT * FROM heats WHERE competition_id = ? AND status = 'pending' AND format = 'itt' "
                'ORDER BY heat_number ASC LIMIT 1', (cid,)).fetchone()
        if not heat:
            return jsonify({'ok': False, 'message': 'Нет запущенного старта'}), 400
        hid = heat['id']
        now = datetime.now()
        now_iso = now.isoformat()

        if action == 'reorder':
            pid = request.form.get('pid', type=int)
            direction = (request.form.get('direction') or '').strip()
            ids = [r['participant_id'] for r in _manual_start_queue(conn, hid)]
            if pid not in ids:
                return jsonify({'ok': False, 'message': 'Этот участник уже стартовал'})
            i = ids.index(pid)
            if direction == 'up' and i > 0:
                ids[i - 1], ids[i] = ids[i], ids[i - 1]
            elif direction == 'down' and i < len(ids) - 1:
                ids[i + 1], ids[i] = ids[i], ids[i + 1]
            elif direction == 'top':
                ids.insert(0, ids.pop(i))
            elif direction == 'bottom':
                ids.append(ids.pop(i))
            with conn:
                for pos, x in enumerate(ids, start=1):
                    conn.execute('UPDATE heat_participants SET sort_order = ? '
                                 'WHERE heat_id = ? AND participant_id = ?', (pos, hid, x))
            return jsonify({'ok': True, 'message': 'Порядок старта сохранён'})

        if action == 'begin':
            if heat['status'] != 'pending':
                return jsonify({'ok': False, 'message': 'Старт уже запущен'})
            active = _start_heat(conn, heat)
            if active is None:
                return jsonify({'ok': False, 'message': 'Некому стартовать: нет участников с номерами'})
            _log_timing(conn, 'manual', 'heat_start', True, competition_id=cid, heat_id=hid,
                        user_login=judge, detail=f'Старт №{heat["heat_number"]} запущен с табло')
            return jsonify({'ok': True, 'message': f'Старт №{heat["heat_number"]} запущен'})

        if heat['status'] != 'running':
            return jsonify({'ok': False,
                            'message': 'Старт ещё не запущен — сначала нажмите «Начать старт»'})

        if action == 'undo':
            last = conn.execute(
                'SELECT r.*, p.number, p.full_name FROM races r JOIN participants p ON p.id = r.participant_id '
                "WHERE r.heat_id = ? AND r.status = 'running' ORDER BY r.start_time DESC LIMIT 1", (hid,)).fetchone()
            if not last:
                return jsonify({'ok': False, 'message': 'Нет стартов для отмены'})
            marks = conn.execute('SELECT COUNT(*) FROM laps WHERE race_id = ?', (last['id'],)).fetchone()[0]
            marks += conn.execute('SELECT COUNT(*) FROM auto_laps WHERE race_id = ?', (last['id'],)).fetchone()[0]
            if marks:
                return jsonify({'ok': False, 'message': f'У №{last["number"]} уже есть отметки — сначала отмените финиш'})
            with conn:
                conn.execute('DELETE FROM races WHERE id = ?', (last['id'],))
                _log_timing(conn, 'manual', 'undo_start', True, competition_id=cid, heat_id=hid,
                            participant_id=last['participant_id'], number=last['number'],
                            full_name=last['full_name'], user_login=judge, detail='Старт отменён (разделка)')
            return jsonify({'ok': True, 'message': f'Старт отменён: №{last["number"]} {last["full_name"]}'})

        nxt = _manual_start_queue(conn, hid)
        nxt = nxt[0] if nxt else None
        if not nxt:
            return jsonify({'ok': False, 'message': 'Все участники старта уже стартовали или отмечены'})

        if action == 'skip':
            with conn:
                conn.execute(
                    'INSERT INTO races (participant_id, competition_id, heat_id, laps_total, planned_laps, start_time, status) '
                    "VALUES (?, ?, ?, ?, ?, ?, 'dns')",
                    (nxt['participant_id'], cid, hid, nxt['laps_total'] or 1, nxt['laps_total'] or 1, now_iso))
                conn.execute("UPDATE heat_participants SET status = 'dns' WHERE heat_id = ? AND participant_id = ?",
                             (hid, nxt['participant_id']))
                _log_timing(conn, 'manual', 'skip', True, competition_id=cid, heat_id=hid,
                            participant_id=nxt['participant_id'], number=nxt['number'],
                            full_name=nxt['full_name'], user_login=judge, detail='Пропуск (DNS) в разделке')
                maybe_finish_heat(conn, hid)
            return jsonify({'ok': True, 'message': f'Пропущен: №{nxt["number"]} {nxt["full_name"]}'})

        if action != 'start':
            return jsonify({'ok': False, 'message': 'Неизвестное действие'}), 400

        with conn:
            conn.execute(
                'INSERT INTO races (participant_id, competition_id, heat_id, laps_total, planned_laps, start_time, status) '
                "VALUES (?, ?, ?, ?, ?, ?, 'running')",
                (nxt['participant_id'], cid, hid, nxt['laps_total'] or 1, nxt['laps_total'] or 1, now_iso))
            _log_timing(conn, 'manual', 'start', True, competition_id=cid, heat_id=hid,
                        participant_id=nxt['participant_id'], number=nxt['number'],
                        full_name=nxt['full_name'], user_login=judge, detail='Старт (разделка)')
        return jsonify({'ok': True, 'message': f'Старт: №{nxt["number"]} {nxt["full_name"]}',
                        'number': nxt['number'], 'time': now.strftime('%H:%M:%S')})
    finally:
        conn.close()


def manual_active():
    cid = request.args.get('cid', type=int)
    if not cid: return '', 400
    conn = get_db()
    try:
        competition, active_races = _get_manual_data(conn, cid)
        if not competition: return '', 404
        return render_template('_manual_active.html', competition=competition, active_races=active_races, last=session.get('manual_last'), log=session.get('manual_log', []))
    finally: conn.close()


def manual_action():
    cid = request.form.get('competition_id', type=int); number_str = request.form.get('number', '').strip()
    def back(): return redirect(url_for('manual_page', cid=cid)) if cid else redirect(url_for('manual_page'))
    if not cid or not number_str: return back()
    _now = datetime.now(); now_ts = _now.strftime('%H:%M:%S'); action_ts = _now.isoformat()
    judge = session.get('user'); judge_id = session.get('user_id')
    conn = get_db()
    try:
        def log(ok, action, reason=None, detail=None, race=None, number=None, full_name=None):
            _log_timing(conn, 'manual', action, ok, reason=reason, competition_id=cid,
                        heat_id=race['heat_id'] if race is not None else None,
                        race_id=race['id'] if race is not None else None,
                        participant_id=race['participant_id'] if race is not None else None,
                        number=number, full_name=full_name, user_login=judge, detail=detail)

        def done(last):
            """Общий выход: JSON для быстрого ввода или редирект для формы."""
            session['manual_last'] = last; _push_manual_log(last)
            if request.form.get('json') == '1': return jsonify(last)
            return back()

        try: number_int = int(number_str)
        except:
            last = {'ok': False, 'number': number_str, 'message': 'Номер должен быть числом', 'time': now_ts, 'action_ts': action_ts, 'is_finish': False}
            with conn: log(False, 'lap', reason='Номер должен быть числом', detail=number_str, number=number_str)
            return done(last)

        race = conn.execute('SELECT r.*, p.full_name, h.min_lap_time FROM races r JOIN participants p ON p.id = r.participant_id JOIN heats h ON h.id = r.heat_id WHERE r.competition_id = ? AND p.number = ? AND r.status = "running" ORDER BY r.start_time DESC LIMIT 1', (cid, number_int)).fetchone()
        if not race:
            last = {'ok': False, 'number': number_int, 'message': 'не найден среди активных', 'time': now_ts, 'action_ts': action_ts, 'is_finish': False}
            with conn: log(False, 'lap', reason='Участник не найден среди активных', number=number_int)
            return done(last)

        min_lap = race['min_lap_time'] if race['min_lap_time'] is not None else 0
        merged = _build_merged_laps(conn, race['id'], min_lap)
        laps_done = len(merged)
        now = datetime.now()

        if laps_done < race['laps_total']:
            last_time = datetime.fromisoformat(merged[-1]['time']) if merged else datetime.fromisoformat(race['start_time'])
            spent = (now - last_time).total_seconds()
            if spent < min_lap:
                last = {'ok': False, 'number': number_int, 'full_name': race['full_name'], 'message': f'слишком быстро (< {min_lap} с). Отменено', 'time': now_ts, 'action_ts': action_ts, 'is_finish': False}
                with conn: log(False, 'lap', reason='Слишком быстрый круг', race=race,
                               number=number_int, full_name=race['full_name'],
                               detail=f'{spent:.1f} с < {min_lap} с')
                return done(last)
            is_last_lap = (laps_done + 1) == race['laps_total']
            with conn:
                lap_id = conn.execute('INSERT INTO laps (race_id, lap_number, lap_time, user_id, user_login) VALUES (?, ?, ?, ?, ?)',
                                      (race['id'], laps_done + 1, now.isoformat(), judge_id, judge)).lastrowid
                if is_last_lap: _process_race_finish(conn, race, now.isoformat(), force=False)
                log(True, 'finish' if is_last_lap else 'lap', race=race, number=number_int,
                    full_name=race['full_name'], detail=f'Круг {laps_done + 1}/{race["laps_total"]}')
            if is_last_lap:
                session['last_action'] = {'type': 'finish', 'id': race['id'], 'time': now.timestamp()}
                last = {'ok': True, 'number': number_int, 'full_name': race['full_name'], 'action': f'ФИНИШ (круг {laps_done + 1}/{race["laps_total"]})', 'time': now_ts, 'action_ts': action_ts, 'is_finish': True}
            else:
                session['last_action'] = {'type': 'lap', 'id': lap_id, 'time': now.timestamp()}
                last = {'ok': True, 'number': number_int, 'full_name': race['full_name'], 'action': f'Круг {laps_done + 1}/{race["laps_total"]}', 'time': now_ts, 'action_ts': action_ts, 'is_finish': False}
        else:
            with conn:
                _process_race_finish(conn, race, now.isoformat(), force=False)
                log(True, 'finish', race=race, number=number_int, full_name=race['full_name'],
                    detail=f'Финиш (кругов {laps_done}/{race["laps_total"]})')
            session['last_action'] = {'type': 'finish', 'id': race['id'], 'time': now.timestamp()}
            last = {'ok': True, 'number': number_int, 'full_name': race['full_name'], 'action': 'ФИНИШ', 'time': now_ts, 'action_ts': action_ts, 'is_finish': True}
        return done(last)
    finally: conn.close()


def manual_undo():
    action = session.get('last_action'); cid = request.form.get('competition_id', type=int)
    judge = session.get('user')
    def back(): return redirect(url_for('manual_page', cid=cid)) if cid else redirect(url_for('manual_page'))

    def log_outside(ok, reason=None, detail=None):
        c = get_db()
        try:
            with c:
                _log_timing(c, 'manual', 'undo', ok, reason=reason, competition_id=cid,
                            user_login=judge, detail=detail)
        finally: c.close()

    if not action:
        flash('Нечего отменять', 'error'); log_outside(False, 'Нечего отменять'); return back()
    if datetime.now().timestamp() - action.get('time', 0) > 15:
        session.pop('last_action', None); flash('Время на отмену истекло', 'error')
        log_outside(False, 'Время на отмену истекло'); return back()
    conn = get_db()
    try:
        if action['type'] == 'lap':
            lap = conn.execute('SELECT * FROM laps WHERE id = ?', (action['id'],)).fetchone()
            if lap:
                race = conn.execute('SELECT * FROM races WHERE id = ?', (lap['race_id'],)).fetchone()
                with conn:
                    conn.execute('DELETE FROM laps WHERE id = ?', (action['id'],))
                    if race['finish_time']:
                        conn.execute("UPDATE races SET finish_time = NULL, status = 'running', laps_behind = 0 WHERE id = ?", (race['id'],))
                        if conn.execute('SELECT status FROM heats WHERE id = ?', (race['heat_id'],)).fetchone()['status'] == 'finished': conn.execute("UPDATE heats SET status = 'running', finished_at = NULL WHERE id = ?", (race['heat_id'],))
                    _log_timing(conn, 'manual', 'undo', True, competition_id=cid, heat_id=race['heat_id'],
                                race_id=race['id'], participant_id=race['participant_id'], user_login=judge,
                                detail='Отменён последний круг')
                flash('Последний круг отменён', 'success')
            else:
                flash('Круг не найден', 'error')
                with conn:
                    _log_timing(conn, 'manual', 'undo', False, reason='Круг не найден', competition_id=cid, user_login=judge)
        elif action['type'] == 'finish':
            race = conn.execute('SELECT * FROM races WHERE id = ?', (action['id'],)).fetchone()
            if race and race['status'] == 'ok':
                with conn:
                    conn.execute("UPDATE races SET finish_time = NULL, status = 'running', laps_behind = 0 WHERE id = ?", (race['id'],))
                    if conn.execute('SELECT status FROM heats WHERE id = ?', (race['heat_id'],)).fetchone()['status'] == 'finished': conn.execute("UPDATE heats SET status = 'running', finished_at = NULL WHERE id = ?", (race['heat_id'],))
                    _log_timing(conn, 'manual', 'undo', True, competition_id=cid, heat_id=race['heat_id'],
                                race_id=race['id'], participant_id=race['participant_id'], user_login=judge,
                                detail='Отменён финиш')
                flash('Финиш отменён', 'success')
            else:
                flash('Заезд не найден', 'error')
                with conn:
                    _log_timing(conn, 'manual', 'undo', False, reason='Заезд не найден', competition_id=cid, user_login=judge)
        session.pop('last_action', None); return back()
    finally: conn.close()


RULES = [
    ('/manual', 'manual_page', manual_page, ['GET']),
    ('/manual/start/data', 'manual_start_data', manual_start_data, ['GET']),
    ('/manual/start/action', 'manual_start_action', manual_start_action, ['POST']),
    ('/manual/active', 'manual_active', manual_active, ['GET']),
    ('/manual/action', 'manual_action', manual_action, ['POST']),
    ('/manual/undo', 'manual_undo', manual_undo, ['POST']),
]
