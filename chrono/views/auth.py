# -*- coding: utf-8 -*-
"""auth: маршруты (регистрируются в app.py)."""


from flask import flash, redirect, render_template, request, session, url_for

from chrono.db import (_ensure_registry, _registry_find)
from chrono.auth import (_apply_auth, _current_comp, _safe_next, _verify_user)
from chrono.logging_setup import (_app_logger)


def login():
    entry = _current_comp()
    if not entry:
        flash('Сначала выберите соревнование', 'error')
        return redirect(url_for('index'))

    slug = entry['slug']
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        next_target = _safe_next(request.form.get('next')) or _safe_next(request.args.get('next'))
        user = _verify_user(username, password)
        if user:
            auth = dict(session.get('auth') or {})
            auth[slug] = {
                'user': user['username'],
                'user_id': user['id'],
                'role': user['role'],
                'is_admin': (user['role'] == 'admin'),
            }
            session['auth'] = auth
            _apply_auth(auth[slug])
            session.permanent = True
            _app_logger.info('Login OK for user=%s comp=%s', user['username'], slug)

            if user['role'] == 'timekeeper':
                return redirect(url_for('manual_page'))
            return redirect(next_target or url_for('competitions_list'))

        _app_logger.warning('Login FAILED for user=%s from %s', username, request.remote_addr)
        flash('Неверный логин или пароль', 'error')
    else:
        next_target = _safe_next(request.args.get('next'))
    return render_template('login.html', next=next_target or '', competition=entry)


def logout():
    user = session.get('user')
    slug = session.get('comp')
    auth = dict(session.get('auth') or {})
    if slug:
        auth.pop(slug, None)
    session['auth'] = auth
    for key in ('user', 'user_id', 'role', 'is_admin'):
        session.pop(key, None)
    _app_logger.info('Logout user=%s comp=%s', user, slug)
    flash('Вы вышли из системы', 'success')
    return redirect(url_for('index'))


def index():
    """Стартовая страница: выбор соревнования."""
    embed = request.args.get('embed') == '1'
    if session.get('role') == 'timekeeper' and session.get('comp'):
        return redirect(url_for('manual_page'))
    # В режиме приложения (embed=1) уже выбранное соревнование открываем сразу
    if embed and session.get('comp'):
        auth = (session.get('auth') or {}).get(session['comp'])
        if auth:
            _apply_auth(auth)
            return redirect(url_for('manual_page', embed='1'))
    reg = _ensure_registry()
    auth = session.get('auth') or {}
    current = session.get('comp')
    items = []
    for c in reg.get('competitions', []):
        # архивные и скрытые на странице входа не показываем (доступны по прямой ссылке /c/<slug>)
        if c.get('archived') or c.get('hidden'):
            continue
        item = dict(c)
        item['authorized'] = c.get('slug') in auth
        item['current'] = (c.get('slug') == current)
        items.append(item)
    return render_template('chooser.html', competitions=items, current=current, embed=embed)


def select_competition(slug):
    """Прямая ссылка на соревнование: /c/domashnyaya-gonka."""
    entry = _registry_find(slug)
    if not entry:
        flash('Соревнование не найдено', 'error')
        return redirect(url_for('index'))
    session['comp'] = slug
    next_target = _safe_next(request.args.get('next'))
    auth = (session.get('auth') or {}).get(slug)
    if auth:
        _apply_auth(auth)
        if auth.get('role') == 'timekeeper':
            return redirect(url_for('manual_page'))
        return redirect(next_target or url_for('competitions_list'))
    if next_target:
        return redirect(url_for('login', next=next_target))
    return redirect(url_for('login'))


def manual_select(slug):
    """Выбор соревнования из мобильного приложения (остаётся внутри /manual)."""
    entry = _registry_find(slug)
    if not entry:
        flash('Соревнование не найдено', 'error')
        return redirect(url_for('index'))
    session['comp'] = slug
    auth = (session.get('auth') or {}).get(slug)
    if auth:
        _apply_auth(auth)
        return redirect(url_for('manual_page', embed='1'))
    return redirect(url_for('login', next='/manual?embed=1'))


RULES = [
    ('/login', 'login', login, ['GET', 'POST']),
    ('/logout', 'logout', logout, ['POST']),
    ('/', 'index', index, ['GET']),
    ('/c/<slug>', 'select_competition', select_competition, ['GET']),
    ('/manual/select/<slug>', 'manual_select', manual_select, ['GET']),
]
