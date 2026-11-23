# -*- coding: utf-8 -*-
"""auth: помощники (перенесены из app.py)."""
import hmac, json, os
from flask import flash, session, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash
from chrono.db import DATA_DIR, _connect, _registry_find, get_db, _ensure_registry

MANAGE_USER = os.environ.get('CHRONO_MANAGE_USER', 'cedor')

MANAGE_PASS = os.environ.get('CHRONO_MANAGE_PASS', 'cedor')

MANAGE_FILE = os.path.join(DATA_DIR, 'manage.json')

def _manage_creds():
    """(логин, хэш пароля или None, признак «из файла»)."""
    try:
        with open(MANAGE_FILE, encoding='utf-8') as f:
            data = json.load(f)
        user = (data.get('username') or '').strip()
        password_hash = data.get('password_hash') or ''
        if user and password_hash:
            return user, password_hash, True
    except (OSError, ValueError):
        pass
    return MANAGE_USER, None, False

def _manage_password_ok(password):
    user, password_hash, from_file = _manage_creds()
    if from_file:
        try:
            return check_password_hash(password_hash, password)
        except Exception:
            return False
    return hmac.compare_digest((password or '').encode('utf-8'), MANAGE_PASS.encode('utf-8'))

SESSION_DAYS = int(os.environ.get('CHRONO_SESSION_DAYS', '7'))

PUBLIC_ENDPOINTS = {
    'results_page', 'results_details', 'results_export',
    'service_worker', 'static',
    'finish_screen', 'finish_screen_data',
    'login', 'logout', 'app_download', 'app_page', 'startlist_public',
    'public_hub', 'public_results', 'public_live', 'public_live_data', 'public_startlist',
    'index', 'select_competition', 'manual_select',
    'manage_page', 'manage_login', 'manage_logout', 'manage_create',
    'manage_delete', 'manage_readonly', 'manage_people_refresh',
    'manage_rename', 'manage_flags', 'manage_export', 'manage_import', 'manage_copy',
    'manage_password', 'manage_account',
    'reg_intro', 'reg_form', 'reg_done', 'api_people',
    'reg_list', 'reg_list_xlsx', 'reg_step',
}

def _safe_next(target):
    if not target: return None
    t = target.strip()
    if not t.startswith('/') or t.startswith('//') or t.startswith('/\\') or '\\' in t:
        return None
    return t

def _verify_user(username, password):
    conn = get_db()
    try:
        row = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if not row or not check_password_hash(row['password_hash'], password):
            return None
        return row
    finally:
        conn.close()

def _current_comp():
    """Запись реестра для выбранного соревнования (или None)."""
    slug = session.get('comp')
    return _registry_find(slug) if slug else None

def _is_read_only(slug=None):
    entry = _registry_find(slug) if slug else _current_comp()
    return bool(entry and entry.get('read_only'))

def _apply_auth(auth):
    """Переносит данные входа соревнования в общие ключи сессии."""
    session['user'] = auth.get('user')
    session['user_id'] = auth.get('user_id')
    session['role'] = auth.get('role', 'judge')
    session['is_admin'] = bool(auth.get('is_admin'))

READONLY_ALLOWED = {
    'login', 'logout', 'index', 'select_competition',
    'manage_page', 'manage_login', 'manage_logout', 'manage_create',
    'manage_delete', 'manage_readonly',
}

def _manage_ok():
    return bool(session.get('manage'))

def _clear_competition(path, name, clear):
    """Очищает выбранные сущности в базе-копии."""
    conn = _connect(path)
    try:
        with conn:
            if 'heats' in clear or 'categories' in clear:
                conn.execute('DELETE FROM auto_laps')
                conn.execute('DELETE FROM laps')
                conn.execute('DELETE FROM races')
                conn.execute('DELETE FROM heat_participants')
                conn.execute('DELETE FROM heat_categories')
                conn.execute('DELETE FROM heats')
            if 'participants' in clear:
                conn.execute('DELETE FROM auto_laps')
                conn.execute('DELETE FROM laps')
                conn.execute('DELETE FROM races')
                conn.execute('DELETE FROM heat_participants')
                conn.execute('DELETE FROM participants')
            if 'categories' in clear:
                conn.execute('UPDATE participants SET category_id = NULL')
                conn.execute('DELETE FROM categories')
            if 'registration' in clear:
                conn.execute("DELETE FROM settings WHERE key LIKE 'reg\\_%' ESCAPE '\\'")
            if 'journal' in clear:
                conn.execute('DELETE FROM registrations')
            if 'protocol' in clear:
                conn.execute("DELETE FROM settings WHERE key LIKE 'proto\\_%' ESCAPE '\\'")
            conn.execute('UPDATE competitions SET name = ?', (name,))
    finally:
        conn.close()

def _require_admin():
    if session.get('role') != 'admin':
        flash('Доступ только для администратора', 'error')
        return False
    return True



def require_auth_hook():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None

    slug = session.get('comp')
    if not slug:
        # Совместимость с мобильным приложением: выбор соревнования прямо на /manual
        if request.endpoint == 'manual_page' and request.args.get('embed') == '1':
            reg = _ensure_registry()
            auth = session.get('auth') or {}
            items = []
            for c in reg.get('competitions', []):
                item = dict(c)
                item['authorized'] = c.get('slug') in auth
                item['current'] = False
                items.append(item)
            return render_template('chooser.html', competitions=items, current=None, embed='manual')
        return redirect(url_for('index'))

    # Соревнование могло быть удалено, пока сессия была открыта
    if not _registry_find(slug):
        session.pop('comp', None)
        flash('Соревнование больше не существует', 'error')
        return redirect(url_for('index'))

    auth = (session.get('auth') or {}).get(slug)
    if not auth:
        next_target = request.path
        if request.query_string:
            next_target += '?' + request.query_string.decode('utf-8', 'ignore')
        return redirect(url_for('login', next=next_target))

    _apply_auth(auth)

    # Режим «только чтение»: разрешаем просмотр, запрещаем любые изменения
    if request.method == 'POST' and _is_read_only(slug) and request.endpoint not in READONLY_ALLOWED:
        flash('Соревнование в режиме «только чтение» — изменения запрещены', 'error')
        back = request.referrer or url_for('competitions_list')
        return redirect(back)

    # Ролевая модель доступа
    role = auth.get('role', 'judge')
    if role == 'timekeeper':
        allowed = {'manual_page', 'manual_active', 'manual_action', 'manual_undo', 'index', 'logout', 'static'}
        if request.endpoint not in allowed:
            flash('Доступ разрешён только к странице хронометража', 'error')
            return redirect(url_for('manual_page'))
    return None
