# -*- coding: utf-8 -*-
"""users: маршруты (регистрируются в app.py)."""
from flask import flash, jsonify, redirect, render_template, request, session, url_for
from chrono.db import (get_db)
from chrono.auth import (_require_admin)
from chrono.logging_setup import (_app_logger)
from chrono.people import (_people_search)
from app import (app, datetime, generate_password_hash, re, sqlite3)


def users_page():
    if not _require_admin(): return redirect(url_for('competitions_list'))
    conn = get_db()
    try:
        users = conn.execute('SELECT id, username, is_admin, role, created_at FROM users ORDER BY username').fetchall()
        return render_template('users.html', users=users)
    finally:
        conn.close()


def user_add():
    if not _require_admin(): return redirect(url_for('competitions_list'))
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    role = request.form.get('role', 'judge')
    if role not in ('admin', 'judge', 'timekeeper'): role = 'judge'

    if not username or len(password) < 4:
        flash('Логин обязателен, пароль ≥ 4 символов', 'error')
        return redirect(url_for('users_page'))
    if not re.match(r'^[A-Za-z0-9_.\-]{2,32}$', username):
        flash('Логин: 2–32 символа, латиница, цифры, _, ., -', 'error')
        return redirect(url_for('users_page'))
    
    is_admin = 1 if role == 'admin' else 0
    conn = get_db()
    try:
        try:
            with conn:
                conn.execute('INSERT INTO users (username, password_hash, is_admin, role, created_at) VALUES (?, ?, ?, ?, ?)',
                             (username, generate_password_hash(password), is_admin, role, datetime.now().isoformat()))
            flash(f'Пользователь «{username}» создан', 'success')
        except sqlite3.IntegrityError:
            flash(f'Пользователь «{username}» уже существует', 'error')
    finally:
        conn.close()
    return redirect(url_for('users_page'))


def user_change_password(uid):
    if not _require_admin(): return redirect(url_for('competitions_list'))
    password = request.form.get('password') or ''
    if len(password) < 4:
        flash('Пароль должен быть не короче 4 символов', 'error')
        return redirect(url_for('users_page'))
    conn = get_db()
    try:
        row = conn.execute('SELECT username FROM users WHERE id = ?', (uid,)).fetchone()
        if not row:
            flash('Пользователь не найден', 'error')
            return redirect(url_for('users_page'))
        with conn:
            conn.execute('UPDATE users SET password_hash = ? WHERE id = ?', (generate_password_hash(password), uid))
        _app_logger.info('Password changed for user=%s by=%s', row['username'], session.get('user'))
        flash(f'Пароль пользователя «{row["username"]}» обновлён', 'success')
    finally:
        conn.close()
    return redirect(url_for('users_page'))


def user_change_role(uid):
    if not _require_admin(): return redirect(url_for('competitions_list'))
    role = request.form.get('role')
    if role not in ('admin', 'judge', 'timekeeper'):
        flash('Неверная роль', 'error')
        return redirect(url_for('users_page'))
    conn = get_db()
    try:
        row = conn.execute('SELECT * FROM users WHERE id = ?', (uid,)).fetchone()
        if not row:
            flash('Пользователь не найден', 'error')
            return redirect(url_for('users_page'))
        if row['username'] == session.get('user'):
            flash('Нельзя изменить свою роль', 'error')
            return redirect(url_for('users_page'))
        if row['role'] == 'admin' and role != 'admin':
            admins_count = conn.execute('SELECT COUNT(*) FROM users WHERE role = "admin"').fetchone()[0]
            if admins_count <= 1:
                flash('Нельзя понизить последнего администратора', 'error')
                return redirect(url_for('users_page'))
        is_admin = 1 if role == 'admin' else 0
        with conn:
            conn.execute('UPDATE users SET role = ?, is_admin = ? WHERE id = ?', (role, is_admin, uid))
        flash('Роль обновлена', 'success')
    finally:
        conn.close()
    return redirect(url_for('users_page'))


def user_delete(uid):
    if not _require_admin(): return redirect(url_for('competitions_list'))
    conn = get_db()
    try:
        row = conn.execute('SELECT * FROM users WHERE id = ?', (uid,)).fetchone()
        if not row:
            flash('Пользователь не найден', 'error')
            return redirect(url_for('users_page'))
        if row['username'] == session.get('user'):
            flash('Нельзя удалить себя', 'error')
            return redirect(url_for('users_page'))
        if row['role'] == 'admin' and conn.execute('SELECT COUNT(*) FROM users WHERE role = "admin"').fetchone()[0] <= 1:
            flash('Нельзя удалить последнего администратора', 'error')
            return redirect(url_for('users_page'))
        with conn:
            conn.execute('DELETE FROM users WHERE id = ?', (uid,))
        flash(f'Пользователь «{row["username"]}» удалён', 'success')
    finally:
        conn.close()
    return redirect(url_for('users_page'))


def api_people():
    """Подсказки по ранее зарегистрировавшимся (публичный список)."""
    return jsonify({'items': _people_search(request.args.get('q', ''))})


RULES = [
    ('/users', 'users_page', users_page, ['GET']),
    ('/users/add', 'user_add', user_add, ['POST']),
    ('/users/<int:uid>/password', 'user_change_password', user_change_password, ['POST']),
    ('/users/<int:uid>/role', 'user_change_role', user_change_role, ['POST']),
    ('/users/<int:uid>/delete', 'user_delete', user_delete, ['POST']),
    ('/api/people', 'api_people', api_people, ['GET']),
]
