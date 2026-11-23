# -*- coding: utf-8 -*-
"""manage: маршруты (регистрируются в app.py)."""

import json
import os
import shutil
import sqlite3
import secrets
from datetime import datetime

from flask import flash, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import generate_password_hash

from chrono.db import (COMPS_DIR, DATA_DIR, _comp_db_path, _connect, _create_competition_db, _ensure_registry, _registry_find, _registry_load, _registry_save, _rel_db_path, init_db)
from chrono.utils import (_slugify, _unique_slug)
from chrono.auth import (MANAGE_FILE, MANAGE_PASS, _clear_competition, _manage_creds, _manage_ok, _manage_password_ok)
from chrono.logging_setup import (_app_logger)
from chrono.people import (PEOPLE_URL, _people_refresh, _people_stats)


def manage_page():
    if not _manage_ok():
        return render_template('manage_login.html')
    reg = _ensure_registry()
    items = []
    for c in reg.get('competitions', []):
        item = dict(c)
        path = _comp_db_path(item)
        item['size'] = os.path.getsize(path) if os.path.exists(path) else 0
        item['participants'] = item['heats'] = item['unfinished'] = 0
        item['users'] = []
        item['db_name'] = ''
        try:
            conn = _connect(path)
            row = conn.execute('SELECT name FROM competitions ORDER BY id LIMIT 1').fetchone()
            item['db_name'] = (row['name'] if row else '') or ''
            item['participants'] = conn.execute('SELECT COUNT(*) FROM participants').fetchone()[0]
            item['heats'] = conn.execute('SELECT COUNT(*) FROM heats').fetchone()[0]
            item['unfinished'] = conn.execute(
                "SELECT COUNT(*) FROM heats WHERE status IN ('pending', 'running')").fetchone()[0]
            item['users'] = [dict(r) for r in conn.execute(
                'SELECT username, role FROM users ORDER BY username').fetchall()]
            conn.close()
        except Exception:
            pass
        items.append(item)

    # архив — отдельной секцией внизу
    items.sort(key=lambda x: (bool(x.get('archived')), (x.get('name') or '').lower()))
    active_items = [c for c in items if not c.get('archived')]
    archived_items = [c for c in items if c.get('archived')]
    counts = {
        'all': len(items),
        'active': len(active_items),
        'archived': len(archived_items),
        'hidden': sum(1 for c in items if c.get('hidden')),
        'readonly': sum(1 for c in items if c.get('read_only')),
        'unfinished': sum(1 for c in items if c.get('unfinished')),
    }
    people_count, people_updated = _people_stats()
    return render_template('manage.html', competitions=items,
                           active_items=active_items, archived_items=archived_items,
                           counts=counts, manage_user=_manage_creds()[0],
                           people_count=people_count, people_updated=people_updated,
                           people_url=PEOPLE_URL)


def manage_people_refresh():
    """Кнопка «Обновить список участников» на странице управления."""
    if not _manage_ok():
        return redirect(url_for('manage_page'))
    count, error = _people_refresh()
    if error:
        flash(error, 'error')
    else:
        flash(f'Список обновлён: {count} участников', 'success')
    return redirect(url_for('manage_page'))


def manage_login():
    user = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    expected_user, _hash, _from_file = _manage_creds()
    if user == expected_user and _manage_password_ok(password):
        session['manage'] = True
        _app_logger.info('Manage login OK')
        flash('Добро пожаловать в управление соревнованиями', 'success')
    else:
        _app_logger.warning('Manage login FAILED from %s', request.remote_addr)
        flash('Неверный логин или пароль', 'error')
    return redirect(url_for('manage_page'))


def manage_account():
    """Смена логина/пароля самой страницы управления (нужен текущий пароль)."""
    if not _manage_ok():
        return redirect(url_for('manage_page'))
    current = request.form.get('current_password') or ''
    new_user = (request.form.get('new_username') or '').strip()
    new_pass = request.form.get('new_password') or ''

    if not _manage_password_ok(current):
        _app_logger.warning('Manage account change: wrong current password from %s', request.remote_addr)
        flash('Текущий пароль указан неверно', 'error')
        return redirect(url_for('manage_page'))
    if not new_user and not new_pass:
        flash('Укажите новый логин или новый пароль', 'error')
        return redirect(url_for('manage_page'))
    if new_user and not (3 <= len(new_user) <= 40):
        flash('Логин — от 3 до 40 символов', 'error')
        return redirect(url_for('manage_page'))
    if new_pass and len(new_pass) < 4:
        flash('Пароль — минимум 4 символа', 'error')
        return redirect(url_for('manage_page'))

    old_user, old_hash, from_file = _manage_creds()
    username = new_user or old_user
    password_hash = generate_password_hash(new_pass) if new_pass else (old_hash or generate_password_hash(MANAGE_PASS))
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = MANAGE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'username': username, 'password_hash': password_hash,
                       'updated': datetime.now().isoformat(timespec='seconds')}, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, MANAGE_FILE)
    except OSError as ex:
        _app_logger.exception('manage account save failed')
        flash(f'Не удалось сохранить: {ex}', 'error')
        return redirect(url_for('manage_page'))

    _app_logger.info('Manage account changed: user=%s new_user=%s pass=%s',
                     old_user, username, bool(new_pass))
    flash(f'Доступ к управлению обновлён: логин «{username}»'
          + (', пароль изменён' if new_pass else ''), 'success')
    return redirect(url_for('manage_page'))


def manage_logout():
    session.pop('manage', None)
    return redirect(url_for('manage_page'))


def manage_create():
    if not _manage_ok():
        return redirect(url_for('manage_page'))
    name = (request.form.get('name') or '').strip()
    admin_login = (request.form.get('admin_login') or '').strip()
    admin_password = request.form.get('admin_password') or ''
    if not name or not admin_login or not admin_password:
        flash('Заполните название, логин и пароль администратора', 'error')
        return redirect(url_for('manage_page'))
    reg = _ensure_registry()
    slug = _unique_slug(_slugify(name), reg)
    path = os.path.join(COMPS_DIR, slug + '.db')
    try:
        _create_competition_db(path, name, admin_login, admin_password)
    except Exception as ex:
        _app_logger.exception('create competition failed')
        flash(f'Не удалось создать базу: {ex}', 'error')
        return redirect(url_for('manage_page'))
    reg['competitions'].append({
        'slug': slug,
        'name': name,
        'db': _rel_db_path(path),
        'created': datetime.now().isoformat(timespec='seconds'),
        'read_only': False,
    })
    _registry_save(reg)
    _app_logger.info('Competition created: %s (%s)', name, slug)
    flash(f'Соревнование «{name}» создано. Ссылка: /c/{slug}', 'success')
    return redirect(url_for('manage_page'))


def manage_delete():
    if not _manage_ok():
        return redirect(url_for('manage_page'))
    slug = (request.form.get('slug') or '').strip()
    entry = _registry_find(slug)
    if not entry:
        flash('Соревнование не найдено', 'error')
        return redirect(url_for('manage_page'))
    if len(_registry_load().get('competitions', [])) <= 1:
        flash('Нельзя удалить последнее соревнование', 'error')
        return redirect(url_for('manage_page'))
    # усиленное подтверждение: нужно ввести название соревнования
    confirm = (request.form.get('confirm') or '').strip()
    if confirm != (entry.get('name') or '').strip():
        flash('Удаление отменено: введите название соревнования точно так, как оно указано', 'error')
        return redirect(url_for('manage_page'))
    path = _comp_db_path(entry)
    try:
        for suffix in ('', '-wal', '-shm'):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
    except Exception as ex:
        _app_logger.exception('delete competition failed')
        flash(f'Не удалось удалить базу: {ex}', 'error')
        return redirect(url_for('manage_page'))
    reg = _registry_load()
    reg['competitions'] = [c for c in reg['competitions'] if c.get('slug') != slug]
    _registry_save(reg)
    if session.get('comp') == slug:
        session.pop('comp', None)
        for key in ('user', 'user_id', 'role', 'is_admin'):
            session.pop(key, None)
    auth = dict(session.get('auth') or {})
    auth.pop(slug, None)
    session['auth'] = auth
    _app_logger.info('Competition deleted permanently: %s', slug)
    flash(f'Соревнование «{entry.get("name")}» удалено без возможности восстановления', 'success')
    return redirect(url_for('manage_page'))


def manage_rename():
    """Переименование соревнования (то, что видно на странице входа и в протоколе)."""
    if not _manage_ok():
        return redirect(url_for('manage_page'))
    slug = (request.form.get('slug') or '').strip()
    name = (request.form.get('name') or '').strip()
    entry = _registry_find(slug)
    if not entry:
        flash('Соревнование не найдено', 'error')
        return redirect(url_for('manage_page'))
    if not name:
        flash('Укажите название', 'error')
        return redirect(url_for('manage_page'))
    reg = _registry_load()
    for c in reg.get('competitions', []):
        if c.get('slug') == slug:
            c['name'] = name
    _registry_save(reg)
    # название внутри соревнования (в приложении) не трогаем — это отдельный параметр
    _app_logger.info('Competition renamed on chooser: %s -> %s', slug, name)
    flash(f'Название на странице входа изменено: «{name}»', 'success')
    return redirect(url_for('manage_page'))


def manage_flags():
    """Архив и «скрытое»: оба флага убирают соревнование со страницы входа."""
    if not _manage_ok():
        return redirect(url_for('manage_page'))
    slug = (request.form.get('slug') or '').strip()
    entry = _registry_find(slug)
    if not entry:
        flash('Соревнование не найдено', 'error')
        return redirect(url_for('manage_page'))
    action = (request.form.get('action') or '').strip()
    if action in ('archive', 'hide'):
        # нельзя убирать со страницы входа соревнование с незавершёнными стартами
        try:
            conn = _connect(_comp_db_path(entry))
            try:
                unfinished = conn.execute(
                    "SELECT COUNT(*) FROM heats WHERE status IN ('pending', 'running')").fetchone()[0]
            finally:
                conn.close()
        except Exception as ex:
            flash(f'Не удалось проверить старты: {ex}', 'error')
            return redirect(url_for('manage_page'))
        if unfinished:
            flash(f'Нельзя: в соревновании незавершённых стартов — {unfinished}. '
                  f'Сначала завершите их.', 'error')
            return redirect(url_for('manage_page'))
    reg = _registry_load()
    for c in reg.get('competitions', []):
        if c.get('slug') != slug:
            continue
        if action == 'archive':
            c['archived'] = True
        elif action == 'unarchive':
            c['archived'] = False
        elif action == 'hide':
            c['hidden'] = True
        elif action == 'show':
            c['hidden'] = False
    _registry_save(reg)
    _app_logger.info('Competition flags: %s action=%s', slug, action)
    messages = {
        'archive': 'перемещено в архив — на странице входа не показывается',
        'unarchive': 'возвращено из архива',
        'hide': 'скрыто — доступно только по прямой ссылке',
        'show': 'снова показывается на странице входа',
    }
    flash(f'«{entry.get("name")}»: {messages.get(action, "флаги обновлены")}', 'success')
    return redirect(url_for('manage_page'))


def manage_password():
    """Смена логина и/или пароля пользователя соревнования (например, если забыли)."""
    if not _manage_ok():
        return redirect(url_for('manage_page'))
    slug = (request.form.get('slug') or '').strip()
    username = (request.form.get('username') or '').strip()
    new_username = (request.form.get('new_username') or '').strip()
    password = request.form.get('password') or ''
    entry = _registry_find(slug)
    if not entry:
        flash('Соревнование не найдено', 'error')
        return redirect(url_for('manage_page'))
    if not username:
        flash('Выберите пользователя', 'error')
        return redirect(url_for('manage_page'))
    if new_username and len(new_username) < 2:
        flash('Логин — минимум 2 символа', 'error')
        return redirect(url_for('manage_page'))
    if len(new_username) > 40:
        flash('Логин — максимум 40 символов', 'error')
        return redirect(url_for('manage_page'))
    if password and len(password) < 4:
        flash('Пароль — минимум 4 символа', 'error')
        return redirect(url_for('manage_page'))
    if not new_username and not password:
        flash('Укажите новый логин или новый пароль', 'error')
        return redirect(url_for('manage_page'))
    try:
        conn = _connect(_comp_db_path(entry))
        try:
            row = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
            if not row:
                flash(f'Пользователь «{username}» не найден', 'error')
                return redirect(url_for('manage_page'))
            if new_username and new_username != username:
                taken = conn.execute('SELECT id FROM users WHERE username = ?', (new_username,)).fetchone()
                if taken:
                    flash(f'Логин «{new_username}» уже занят', 'error')
                    return redirect(url_for('manage_page'))
            with conn:
                if new_username and new_username != username:
                    conn.execute('UPDATE users SET username = ? WHERE id = ?', (new_username, row['id']))
                if password:
                    conn.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                                 (generate_password_hash(password), row['id']))
        finally:
            conn.close()
    except Exception as ex:
        _app_logger.exception('user change failed')
        flash(f'Не удалось изменить пользователя: {ex}', 'error')
        return redirect(url_for('manage_page'))

    done = []
    if new_username and new_username != username:
        done.append(f'логин «{username}» → «{new_username}»')
    if password:
        done.append('пароль обновлён')
    _app_logger.info('User changed via manage: comp=%s user=%s new=%s pass=%s',
                     slug, username, new_username or '-', bool(password))
    flash('Сохранено: ' + ', '.join(done), 'success')
    return redirect(url_for('manage_page'))


def manage_export(slug):
    """Скачать файл базы соревнования."""
    if not _manage_ok():
        return redirect(url_for('manage_page'))
    entry = _registry_find(slug)
    if not entry:
        flash('Соревнование не найдено', 'error')
        return redirect(url_for('manage_page'))
    path = _comp_db_path(entry)
    if not os.path.exists(path):
        flash('Файл базы не найден', 'error')
        return redirect(url_for('manage_page'))
    # переносим данные из WAL в основной файл, чтобы выгрузка была полной
    try:
        conn = sqlite3.connect(path)
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.close()
    except Exception:
        _app_logger.exception('export: checkpoint failed')
    fname = f"{slug}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    _app_logger.info('Competition exported: %s -> %s', slug, fname)
    return send_file(path, as_attachment=True, download_name=fname,
                     mimetype='application/octet-stream')


def manage_import():
    """Создать соревнование из загруженного файла базы."""
    if not _manage_ok():
        return redirect(url_for('manage_page'))
    upload = request.files.get('file')
    name = (request.form.get('name') or '').strip()
    if not upload or not upload.filename:
        flash('Выберите файл базы (.db)', 'error')
        return redirect(url_for('manage_page'))
    if os.path.splitext(upload.filename)[1].lower() not in ('.db', '.sqlite', '.sqlite3'):
        flash('Нужен файл базы SQLite: .db, .sqlite или .sqlite3', 'error')
        return redirect(url_for('manage_page'))

    os.makedirs(COMPS_DIR, exist_ok=True)
    tmp = os.path.join(COMPS_DIR, f'_upload-{secrets.token_hex(8)}.db')
    upload.save(tmp)
    try:
        probe = sqlite3.connect(tmp)
        probe.row_factory = sqlite3.Row
        tables = {r['name'] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        needed = {'competitions', 'categories', 'participants', 'users'}
        if not needed <= tables:
            probe.close()
            os.remove(tmp)
            flash('Это не база «Хронометража»: нет нужных таблиц', 'error')
            return redirect(url_for('manage_page'))
        row = probe.execute('SELECT name FROM competitions ORDER BY id LIMIT 1').fetchone()
        db_name = (row['name'] if row else '') or ''
        probe.close()
    except Exception as ex:
        try:
            os.remove(tmp)
        except OSError:
            pass
        flash(f'Не удалось прочитать файл базы: {ex}', 'error')
        return redirect(url_for('manage_page'))

    if not name:
        name = db_name or 'Импортированное соревнование'
    reg = _ensure_registry()
    slug = _unique_slug(_slugify(name), reg)
    dest = os.path.join(COMPS_DIR, slug + '.db')
    try:
        shutil.move(tmp, dest)
        init_db(dest)
        conn = _connect(dest)
        with conn:
            conn.execute('UPDATE competitions SET name = ?', (name,))
        conn.close()
    except Exception as ex:
        _app_logger.exception('import failed')
        flash(f'Не удалось подготовить базу: {ex}', 'error')
        return redirect(url_for('manage_page'))

    reg['competitions'].append({
        'slug': slug, 'name': name, 'db': _rel_db_path(dest),
        'created': datetime.now().isoformat(timespec='seconds'),
        'read_only': False, 'archived': False, 'hidden': False,
    })
    _registry_save(reg)
    _app_logger.info('Competition imported: %s (%s)', name, slug)
    flash(f'Соревнование «{name}» загружено. Ссылка: /c/{slug}', 'success')
    return redirect(url_for('manage_page'))


def manage_copy():
    """Новое соревнование копированием существующего (с выбором, что очистить)."""
    if not _manage_ok():
        return redirect(url_for('manage_page'))
    slug = (request.form.get('slug') or '').strip()
    name = (request.form.get('name') or '').strip()
    entry = _registry_find(slug)
    if not entry:
        flash('Соревнование не найдено', 'error')
        return redirect(url_for('manage_page'))
    src = _comp_db_path(entry)
    if not os.path.exists(src):
        flash('Файл базы не найден', 'error')
        return redirect(url_for('manage_page'))
    name = name or f"{entry.get('name')} (копия)"
    clear = set(request.form.getlist('clear'))

    reg = _ensure_registry()
    new_slug = _unique_slug(_slugify(name), reg)
    dest = os.path.join(COMPS_DIR, new_slug + '.db')
    os.makedirs(COMPS_DIR, exist_ok=True)
    try:
        s = sqlite3.connect(src)
        d = sqlite3.connect(dest)
        s.backup(d)
        d.close()
        s.close()
        init_db(dest)
        _clear_competition(dest, name, clear)
    except Exception as ex:
        _app_logger.exception('copy failed')
        try:
            os.remove(dest)
        except OSError:
            pass
        flash(f'Не удалось скопировать: {ex}', 'error')
        return redirect(url_for('manage_page'))

    reg['competitions'].append({
        'slug': new_slug, 'name': name, 'db': _rel_db_path(dest),
        'created': datetime.now().isoformat(timespec='seconds'),
        'read_only': False, 'archived': False, 'hidden': False,
    })
    _registry_save(reg)
    _app_logger.info('Competition copied: %s -> %s (cleared: %s)', slug, new_slug, ','.join(sorted(clear)) or '—')
    flash(f'Создана копия «{name}». Ссылка: /c/{new_slug}', 'success')
    return redirect(url_for('manage_page'))


def manage_readonly():
    if not _manage_ok():
        return redirect(url_for('manage_page'))
    slug = (request.form.get('slug') or '').strip()
    entry = _registry_find(slug)
    if not entry:
        flash('Соревнование не найдено', 'error')
        return redirect(url_for('manage_page'))
    want = request.form.get('read_only') == '1'

    if want:
        try:
            conn = _connect(_comp_db_path(entry))
            try:
                unfinished = conn.execute(
                    "SELECT COUNT(*) FROM heats WHERE status IN ('pending', 'running')").fetchone()[0]
            finally:
                conn.close()
        except Exception as ex:
            flash(f'Не удалось проверить старты: {ex}', 'error')
            return redirect(url_for('manage_page'))
        if unfinished:
            flash(f'Нельзя включить «только чтение»: незавершённых стартов — {unfinished}. '
                  f'Сначала завершите их.', 'error')
            return redirect(url_for('manage_page'))

    reg = _registry_load()
    for c in reg.get('competitions', []):
        if c.get('slug') == slug:
            c['read_only'] = want
    _registry_save(reg)
    _app_logger.info('Competition %s read_only=%s', slug, want)
    flash(f'«{entry.get("name")}»: режим «только чтение» ' + ('включён' if want else 'выключен'), 'success')
    return redirect(url_for('manage_page'))


RULES = [
    ('/manage', 'manage_page', manage_page, ['GET']),
    ('/manage/people', 'manage_people_refresh', manage_people_refresh, ['POST']),
    ('/manage/login', 'manage_login', manage_login, ['POST']),
    ('/manage/account', 'manage_account', manage_account, ['POST']),
    ('/manage/logout', 'manage_logout', manage_logout, ['POST']),
    ('/manage/create', 'manage_create', manage_create, ['POST']),
    ('/manage/delete', 'manage_delete', manage_delete, ['POST']),
    ('/manage/rename', 'manage_rename', manage_rename, ['POST']),
    ('/manage/flags', 'manage_flags', manage_flags, ['POST']),
    ('/manage/password', 'manage_password', manage_password, ['POST']),
    ('/manage/export/<slug>', 'manage_export', manage_export, ['GET']),
    ('/manage/import', 'manage_import', manage_import, ['POST']),
    ('/manage/copy', 'manage_copy', manage_copy, ['POST']),
    ('/manage/readonly', 'manage_readonly', manage_readonly, ['POST']),
]
