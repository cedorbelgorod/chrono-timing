# -*- coding: utf-8 -*-
"""public: маршруты (регистрируются в app.py)."""
from flask import redirect, render_template, request, send_from_directory, url_for
from chrono.db import (_comp_db_path, _connect, _get_competition, get_db)
from chrono.settings import (_reload_settings_cache_from_db)
from chrono.timing import (_heat_startlist_data)
from chrono.results_helpers import (_build_dashboard_heats, _finish_screen_data_response, _finish_screen_response, _hub_data, _public_competition, _results_response)
from app import (app, os)


def app_page():
    """Страница скачивания Android-приложения."""
    return render_template('app_page.html')


def app_download():
    """Скачивание Android-приложения."""
    path = os.path.join(app.static_folder, 'chrono.apk')
    if not os.path.exists(path):
        return 'Приложение ещё не собрано', 404
    return send_from_directory(app.static_folder, 'chrono.apk', as_attachment=True,
                               download_name='Хронометраж.apk',
                               mimetype='application/vnd.android.package-archive')


def service_worker():
    response = send_from_directory('static', 'sw.js', mimetype='application/javascript')
    response.headers['Service-Worker-Allowed'] = '/'
    return response


def health_page():
    return redirect(url_for('summary_page'))


def dashboard_page():
    conn = get_db()
    try:
        competition = _get_competition(conn)
        cid = competition['id']
        heats_data = _build_dashboard_heats(conn, cid)
        return render_template('dashboard.html', competition=competition, heats=heats_data)
    finally: conn.close()


def dashboard_data():
    cid = request.args.get('cid', type=int)
    if not cid: return '', 400
    conn = get_db()
    try:
        if not conn.execute('SELECT id FROM competitions WHERE id = ?', (cid,)).fetchone(): return '', 404
        return render_template('_dashboard_heats.html', heats=_build_dashboard_heats(conn, cid))
    finally: conn.close()


def finish_screen():
    conn = get_db()
    try:
        return _finish_screen_response(conn)
    finally: conn.close()


def finish_screen_data():
    conn = get_db()
    try:
        cid = request.args.get('cid', type=int)
        if not cid:
            cid = _get_competition(conn)['id']
        return _finish_screen_data_response(conn, cid)
    finally: conn.close()


def public_hub(slug):
    entry = _public_competition(slug)
    if not entry:
        return render_template('public_hub.html', data=None, slug=slug), 404
    path = _comp_db_path(entry)
    conn = _connect(path)
    try:
        _reload_settings_cache_from_db(conn, path)
        data = _hub_data(conn, entry)
        if not data:
            return render_template('public_hub.html', data=None, slug=slug), 404
        return render_template('public_hub.html', data=data, slug=slug)
    finally:
        conn.close()


def public_results(slug):
    entry = _public_competition(slug)
    if not entry:
        return 'Соревнование не найдено', 404
    conn = _connect(_comp_db_path(entry))
    try:
        comp = conn.execute('SELECT * FROM competitions LIMIT 1').fetchone()
        if not comp:
            return 'Соревнование не найдено', 404
        return _results_response(conn, comp['id'],
                                 request.args.get('target', '').strip(),
                                 request.args.get('include_auto', '1') == '1',
                                 public_view=True)
    finally:
        conn.close()


def public_live(slug):
    entry = _public_competition(slug)
    if not entry:
        return 'Соревнование не найдено', 404
    conn = _connect(_comp_db_path(entry))
    try:
        return _finish_screen_response(conn, url_for('public_live_data', slug=slug))
    finally:
        conn.close()


def public_live_data(slug):
    entry = _public_competition(slug)
    if not entry:
        return '', 404
    conn = _connect(_comp_db_path(entry))
    try:
        comp = conn.execute('SELECT * FROM competitions LIMIT 1').fetchone()
        if not comp:
            return '', 404
        return _finish_screen_data_response(conn, comp['id'])
    finally:
        conn.close()


def public_startlist(slug, hid):
    entry = _public_competition(slug)
    if not entry:
        return 'Соревнование не найдено', 404
    conn = _connect(_comp_db_path(entry))
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        comp = conn.execute('SELECT * FROM competitions LIMIT 1').fetchone()
        if not h or not comp or h['competition_id'] != comp['id']:
            return 'Старт не найден', 404
        data = _heat_startlist_data(conn, h, comp)
        return render_template('startlist_public.html', data=data)
    finally:
        conn.close()


RULES = [
    ('/app', 'app_page', app_page, ['GET']),
    ('/app.apk', 'app_download', app_download, ['GET']),
    ('/sw.js', 'service_worker', service_worker, ['GET']),
    ('/health', 'health_page', health_page, ['GET']),
    ('/dashboard', 'dashboard_page', dashboard_page, ['GET']),
    ('/dashboard/data', 'dashboard_data', dashboard_data, ['GET']),
    ('/finish-screen', 'finish_screen', finish_screen, ['GET']),
    ('/finish-screen/data', 'finish_screen_data', finish_screen_data, ['GET']),
    ('/p/<slug>', 'public_hub', public_hub, ['GET']),
    ('/p/<slug>/results', 'public_results', public_results, ['GET']),
    ('/p/<slug>/live', 'public_live', public_live, ['GET']),
    ('/p/<slug>/live/data', 'public_live_data', public_live_data, ['GET']),
    ('/p/<slug>/sl/<int:hid>', 'public_startlist', public_startlist, ['GET']),
]
