import io
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import logging
import traceback
import secrets
import hmac
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, session, send_file, send_from_directory,
    Response, jsonify, has_request_context
)
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash, check_password_hash
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

try:
    from swrfid import RFIDReader
    SWRFID_AVAILABLE = True
except ImportError:
    SWRFID_AVAILABLE = False

app = Flask(__name__)

# ======================== ПУТИ И ПАПКИ ========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Пути можно переопределить переменными окружения (например, в Docker — на смонтированный том)
DB_PATH = os.environ.get('CHRONO_DB') or os.path.join(BASE_DIR, 'chrono.db')
LOG_DIR = os.environ.get('CHRONO_LOG_DIR') or os.path.join(BASE_DIR, 'log')
CERT_DIR = os.environ.get('CHRONO_CERT_DIR') or os.path.join(BASE_DIR, 'cert')

# Папка данных: здесь лежат базы соревнований, реестр и резервные копии
DATA_DIR = os.path.dirname(os.path.abspath(DB_PATH))
REGISTRY_PATH = os.environ.get('CHRONO_REGISTRY') or os.path.join(DATA_DIR, 'competitions.json')
COMPS_DIR = os.path.join(DATA_DIR, 'comps')
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')

# Учётка страницы управления соревнованиями (не путать с логинами соревнований)
MANAGE_USER = os.environ.get('CHRONO_MANAGE_USER', 'cedor')
MANAGE_PASS = os.environ.get('CHRONO_MANAGE_PASS', 'cedor')
# Смена логина/пароля управления из интерфейса: файл в каталоге данных.
# Пока файла нет — работают переменные окружения (или значения по умолчанию).
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

# ======================== РЕЕСТР СОРЕВНОВАНИЙ ========================
# Одно соревнование = одна база SQLite. Список баз хранится в competitions.json.
_TRANSLIT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e', 'ж': 'zh',
    'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o',
    'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'kh', 'ц': 'ts',
    'ч': 'ch', 'ш': 'sh', 'щ': 'shch', 'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu',
    'я': 'ya',
}


def _slugify(name):
    """«Домашняя гонка» → «domashnyaya-gonka» — для ссылки /c/<slug>."""
    out = []
    for ch in (name or '').strip().lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and (ch.isalnum()):
            out.append(ch)
        elif ch in ' -_':
            out.append('-')
    slug = re.sub(r'-+', '-', ''.join(out)).strip('-')
    return (slug or 'competition')[:60]


def _unique_slug(base, reg):
    used = {c.get('slug') for c in reg.get('competitions', [])}
    if base not in used:
        return base
    i = 2
    while f'{base}-{i}' in used:
        i += 1
    return f'{base}-{i}'


def _registry_load():
    try:
        with open(REGISTRY_PATH, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {'competitions': []}
    if not isinstance(data, dict):
        return {'competitions': []}
    data.setdefault('competitions', [])
    return _registry_norm(data)


def _registry_save(reg):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = REGISTRY_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REGISTRY_PATH)


def _registry_find(slug):
    if not slug:
        return None
    for c in _registry_load().get('competitions', []):
        if c.get('slug') == slug:
            return c
    return None


def _comp_db_path(entry):
    path = (entry or {}).get('db') or ''
    # в реестре пути хранятся с «/» — чтобы база переезжала между Windows и Linux
    path = path.replace('\\', '/')
    return path if os.path.isabs(path) else os.path.join(DATA_DIR, path)


def _rel_db_path(path):
    return os.path.relpath(path, DATA_DIR).replace('\\', '/')


def _public_competition(slug):
    """Соревнование для публичной страницы: None, если скрыто/архив/нет базы."""
    entry = _registry_find(slug)
    if not entry or entry.get('hidden') or entry.get('archived'):
        return None
    path = _comp_db_path(entry)
    if not os.path.exists(path):
        return None
    return entry


def _heat_participant_counts(conn, cid):
    """{heat_id: {'total': n, 'active': m}} — для списков стартов."""
    out = {}
    for r in conn.execute(
            'SELECT hp.heat_id, COUNT(*) AS total, '
            "SUM(CASE WHEN hp.status != 'dns' THEN 1 ELSE 0 END) AS active "
            'FROM heat_participants hp JOIN heats h ON h.id = hp.heat_id '
            'WHERE h.competition_id = ? GROUP BY hp.heat_id', (cid,)).fetchall():
        out[r['heat_id']] = {'total': r['total'], 'active': r['active'] or 0}
    return out


def _registry_norm(reg):
    """Проставляет флаги, появившиеся позже (архив, скрытое, только чтение)."""
    for c in reg.get('competitions', []):
        c.setdefault('archived', False)
        c.setdefault('hidden', False)
        c.setdefault('read_only', False)
        c.setdefault('created', '')
    return reg


def _ensure_registry():
    """Первый запуск: существующая база регистрируется как первое соревнование."""
    if os.path.exists(REGISTRY_PATH):
        reg = _registry_load()
        _migrate_competitions()
        return _registry_norm(reg)
    name = 'Соревнование'
    if os.path.exists(DB_PATH):
        try:
            probe = sqlite3.connect(DB_PATH)
            probe.row_factory = sqlite3.Row
            row = probe.execute('SELECT name FROM competitions ORDER BY id LIMIT 1').fetchone()
            if row and row['name']:
                name = row['name']
            probe.close()
        except Exception:
            pass
    reg = {'competitions': []}
    reg['competitions'].append({
        'slug': _unique_slug(_slugify(name), reg),
        'name': name,
        'db': _rel_db_path(DB_PATH),
        'created': datetime.now().isoformat(timespec='seconds'),
        'read_only': False,
    })
    _registry_save(reg)
    return reg


def _active_db_path():
    """Файл базы текущего соревнования (из сессии), иначе — база по умолчанию."""
    slug = None
    if has_request_context():
        slug = session.get('comp')
    if slug:
        entry = _registry_find(slug)
        if entry:
            return _comp_db_path(entry)
    return DB_PATH


# Создаём папку для логов, если её нет
os.makedirs(LOG_DIR, exist_ok=True)

# ======================== секретный ключ ========================
_secret = os.environ.get('CHRONO_SECRET_KEY')
if not _secret:
    _secret = secrets.token_hex(32)
    print('⚠  CHRONO_SECRET_KEY не задан — сгенерирован временный.', flush=True)
app.secret_key = _secret

REG_NUMBER_BASE = 999000
LOG_SIZE = 8
AUTO_LOG_SIZE = 20

# ---------------------- Аутентификация (форма входа) ----------------------
AUTH_USERNAME = os.environ.get('CHRONO_USER', 'admin')
AUTH_PASSWORD = os.environ.get('CHRONO_PASS', 'admin')
SESSION_DAYS = int(os.environ.get('CHRONO_SESSION_DAYS', '7'))
app.permanent_session_lifetime = timedelta(days=SESSION_DAYS)

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

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)

# За обратным прокси (Caddy) — доверяем X-Forwarded-*, чтобы ссылки были https://
if os.environ.get('CHRONO_PROXY') == '1':
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

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

# ---- текущее соревнование и режим «только чтение» ----

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

# POST-запросы, которые не меняют данные соревнования
READONLY_ALLOWED = {
    'login', 'logout', 'index', 'select_competition',
    'manage_page', 'manage_login', 'manage_logout', 'manage_create',
    'manage_delete', 'manage_readonly',
}

@app.before_request
def _require_auth():
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

@app.route('/login', methods=['GET', 'POST'])
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

@app.route('/logout', methods=['POST'])
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

# ---- выбор соревнования ----

@app.route('/')
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

@app.route('/c/<slug>')
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

@app.route('/manual/select/<slug>')
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

# ======================= УПРАВЛЕНИЕ СОРЕВНОВАНИЯМИ =======================
# Отдельная страница под своим паролем (по умолчанию cedor / cedor).

def _create_competition_db(path, name, admin_login, admin_password):
    """Создаёт пустую базу соревнования с заданным администратором."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    init_db(path)
    conn = _connect(path)
    try:
        with conn:
            row = conn.execute('SELECT id FROM competitions ORDER BY id LIMIT 1').fetchone()
            if row:
                conn.execute('UPDATE competitions SET name = ? WHERE id = ?', (name, row['id']))
            else:
                conn.execute('INSERT INTO competitions (name) VALUES (?)', (name,))
            conn.execute('DELETE FROM users')
            conn.execute(
                'INSERT INTO users (username, password_hash, is_admin, role, created_at) VALUES (?, ?, 1, ?, ?)',
                (admin_login, generate_password_hash(admin_password), 'admin', datetime.now().isoformat()))
    finally:
        conn.close()


def _manage_ok():
    return bool(session.get('manage'))


@app.route('/manage', methods=['GET'])
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


@app.route('/manage/people', methods=['POST'])
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


@app.route('/manage/login', methods=['POST'])
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


@app.route('/manage/account', methods=['POST'])
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


@app.route('/manage/logout', methods=['POST'])
def manage_logout():
    session.pop('manage', None)
    return redirect(url_for('manage_page'))


@app.route('/manage/create', methods=['POST'])
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


@app.route('/manage/delete', methods=['POST'])
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


@app.route('/manage/rename', methods=['POST'])
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


@app.route('/manage/flags', methods=['POST'])
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


@app.route('/manage/password', methods=['POST'])
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


@app.route('/manage/export/<slug>')
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


@app.route('/manage/import', methods=['POST'])
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


@app.route('/manage/copy', methods=['POST'])
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


@app.route('/manage/readonly', methods=['POST'])
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

# ---------------- Диагностика и логи ----------------
def _make_file_logger(name, filename, fmt='%(asctime)s %(message)s', level=logging.INFO, max_bytes=5*1024*1024, backup_count=3):
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        try:
            h = RotatingFileHandler(filename, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8')
            h.setFormatter(logging.Formatter(fmt))
            logger.addHandler(h)
        except Exception:
            pass
    return logger

_access_logger = _make_file_logger('access', os.path.join(LOG_DIR, 'access.log'), fmt='%(message)s')
_app_logger = _make_file_logger('chrono', os.path.join(LOG_DIR, 'app.log'), fmt='%(asctime)s %(levelname)s %(message)s')
_swrfid_logger = _make_file_logger('swrfid', os.path.join(LOG_DIR, 'swrfid.log'), fmt='%(asctime)s %(message)s')

logging.getLogger('werkzeug').setLevel(logging.WARNING)
logging.getLogger('werkzeug').propagate = False

_NOISY_PATHS = {'/manual/active', '/auto/detail_rows', '/auto/log', '/results/details', '/finish-screen/data'}

@app.after_request
def _log_request(response):
    if request.path in _NOISY_PATHS or (response.status_code == 404 and request.path == '/favicon.ico'):
        return response
    try:
        _access_logger.info(f'{datetime.now().isoformat()} {request.method} {request.path} -> {response.status_code}')
    except Exception:
        pass
    return response

@app.errorhandler(Exception)
def _handle_exception(e):
    if isinstance(e, HTTPException):
        return e
    tb = traceback.format_exc()
    try:
        with open(os.path.join(LOG_DIR, 'error_trace.log'), 'a', encoding='utf-8') as f:
            f.write(f'\n===== {datetime.now().isoformat()} {request.method} {request.path} =====\n')
            f.write(tb)
            f.write('\n')
    except Exception:
        pass
    _app_logger.error('Unhandled exception on %s %s', request.method, request.path, exc_info=e)
    return ('Внутренняя ошибка сервера. Подробности в файле log/error_trace.log', 500)

# ===================== RFID-статусы и лог =====================
_swrfid_thread = None
_swrfid_stop_event = threading.Event()
_swrfid_status_lock = threading.Lock()
_swrfid_status = {'connected': False, 'last_error': None, 'port': None}

_auto_log_lock = threading.Lock()
_auto_log = []

_settings_lock = threading.Lock()
_settings_cache = {}

RFID_DEFAULTS = {
    'swrfid_enabled': '0',
    'swrfid_port': 'COM3',
    'swrfid_baud': '115200',
}

# Данные для официального протокола (хранятся в settings)
PROTOCOL_DEFAULTS = {
    'proto_title': '',
    'proto_venue': '',
    'proto_date': '',
    'proto_start_time': '',
    'proto_chief_judge': '',
    'proto_start_judge': '',
    'proto_finish_judge': '',
    'proto_chief_secretary': '',
    'proto_track_name': '',
    'proto_track_reg': '',
    'proto_route_length': '',
    'proto_weather_start': '',
    'proto_weather_end': '',
    'proto_temp_start': '',
    'proto_temp_end': '',
    'proto_humidity': '',
    'proto_weather_city': '',
    'proto_weather_lat': '',
    'proto_weather_lon': '',
}

# Формат старта (у соревнования один на все старты)
RACE_FORMATS = {
    'mass': 'Групповой старт',
    'itt': 'Раздельный старт',
}

ITT_DEFAULTS = {
    'itt_start_interval': '30',
    'itt_countdown_sound': '1',
}


def _comp_format(comp):
    """Формат соревнования: 'mass' (по умолчанию) или 'itt'."""
    if comp is None:
        return 'mass'
    try:
        value = comp['format']
    except (KeyError, IndexError):
        return 'mass'
    return value if value in RACE_FORMATS else 'mass'


def _itt_interval():
    """Интервал старта в разделке, секунды (минимум 5)."""
    try:
        value = int(float(get_cached_setting('itt_start_interval', '30') or 30))
    except (TypeError, ValueError):
        value = 30
    return max(5, value)


# Коды погоды WMO → короткое описание
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

def _reload_settings_cache_from_db(conn, path=None):
    """Загружает настройки в кэш для конкретной базы (у каждого соревнования свой)."""
    key = path or _active_db_path()
    rows = conn.execute('SELECT key, value FROM settings').fetchall()
    with _settings_lock:
        _settings_cache[key] = {r['key']: r['value'] for r in rows}

def get_cached_setting(key, default=None):
    path = _active_db_path()
    with _settings_lock:
        data = _settings_cache.get(path)
    if data is None:
        conn = _connect(path)
        try:
            _reload_settings_cache_from_db(conn, path)
        finally:
            conn.close()
        with _settings_lock:
            data = _settings_cache.get(path, {})
    return data.get(key, default)

def set_cached_setting(conn, key, value):
    conn.execute('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, str(value)))
    path = _active_db_path()
    with _settings_lock:
        _settings_cache.setdefault(path, {})[key] = str(value)

def _push_auto_log(entry):
    with _auto_log_lock:
        _auto_log.insert(0, entry)
        del _auto_log[AUTO_LOG_SIZE:]

# ---------------------- Журнал событий хронометража ----------------------
def _create_timing_log(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS timing_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        competition_id INTEGER,
        heat_id INTEGER,
        race_id INTEGER,
        participant_id INTEGER,
        source TEXT NOT NULL,
        action TEXT NOT NULL,
        ok INTEGER NOT NULL DEFAULT 0,
        reason TEXT,
        number TEXT,
        full_name TEXT,
        user_login TEXT,
        detail TEXT
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_timing_log_created ON timing_log (created_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_timing_log_comp ON timing_log (competition_id)')

def _log_timing(conn, source, action, ok, reason=None, competition_id=None, heat_id=None, race_id=None,
                participant_id=None, number=None, full_name=None, user_login=None, detail=None):
    """Пишет событие хронометража в БД. Никогда не роняет основной сценарий."""
    params = (datetime.now().isoformat(timespec='seconds'), competition_id, heat_id, race_id, participant_id,
              source, action, 1 if ok else 0, reason or None,
              None if number is None else str(number), full_name, user_login, detail)
    sql = ('INSERT INTO timing_log (created_at, competition_id, heat_id, race_id, participant_id, source, action, ok,'
           ' reason, number, full_name, user_login, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
    try:
        conn.execute(sql, params)
    except sqlite3.OperationalError:
        # таблицы ещё нет (старая база до первого запуска init_db) — создаём и пробуем снова
        try:
            _create_timing_log(conn)
            conn.execute(sql, params)
        except Exception:
            pass
    except Exception:
        pass

# ============================ Утилиты ============================
def _connect(path):
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn

def get_db():
    """Соединение с базой текущего соревнования."""
    return _connect(_active_db_path())

def init_db(path=None):
    conn = _connect(path or DB_PATH)
    try:
        conn.execute('PRAGMA journal_mode = WAL')
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL DEFAULT 'judge',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS competitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                format TEXT NOT NULL DEFAULT 'mass'
            );
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, competition_id INTEGER NOT NULL, name TEXT NOT NULL,
                laps_total INTEGER NOT NULL DEFAULT 1, gender TEXT, year_from INTEGER, year_to INTEGER,
                sort_order INTEGER, lap_length TEXT, route_length TEXT, hd TEXT, ms TEXT, ts TEXT,
                FOREIGN KEY (competition_id) REFERENCES competitions(id) ON DELETE CASCADE,
                UNIQUE (competition_id, name)
            );
            CREATE TABLE IF NOT EXISTS participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT, competition_id INTEGER NOT NULL, number INTEGER,
                full_name TEXT NOT NULL, birth_year INTEGER, category_id INTEGER, gender TEXT,
                city TEXT, team TEXT, phone TEXT, consent_at TEXT, reg_source TEXT,
                FOREIGN KEY (competition_id) REFERENCES competitions(id) ON DELETE CASCADE,
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE RESTRICT, UNIQUE (competition_id, number)
            );
            CREATE TABLE IF NOT EXISTS registrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                competition_id INTEGER NOT NULL,
                participant_id INTEGER,
                full_name TEXT,
                birth_year INTEGER,
                gender TEXT,
                city TEXT,
                team TEXT,
                phone TEXT,
                category_id INTEGER,
                category_name TEXT,
                consent_at TEXT,
                created_at TEXT NOT NULL,
                ip TEXT,
                user_agent TEXT,
                browser TEXT,
                os TEXT,
                device TEXT,
                accept_language TEXT,
                referer TEXT,
                raw_json TEXT,
                FOREIGN KEY (competition_id) REFERENCES competitions(id) ON DELETE CASCADE,
                FOREIGN KEY (participant_id) REFERENCES participants(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reg_comp ON registrations(competition_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_reg_pid ON registrations(participant_id);
            CREATE TABLE IF NOT EXISTS bib_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                competition_id INTEGER NOT NULL,
                number INTEGER NOT NULL,
                tag TEXT NOT NULL,
                slot INTEGER NOT NULL DEFAULT 1,
                created_at TEXT,
                FOREIGN KEY (competition_id) REFERENCES competitions(id) ON DELETE CASCADE,
                UNIQUE (competition_id, number, slot),
                UNIQUE (competition_id, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_bib_tags_number ON bib_tags(competition_id, number);
            CREATE TABLE IF NOT EXISTS heats (
                id INTEGER PRIMARY KEY AUTOINCREMENT, competition_id INTEGER NOT NULL, heat_number INTEGER NOT NULL,
                name TEXT, finish_on_leader INTEGER NOT NULL DEFAULT 0, min_lap_time REAL NOT NULL DEFAULT 5,
                auto_enabled INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
                started_at TEXT, finished_at TEXT, format TEXT NOT NULL DEFAULT 'mass',
                start_order TEXT NOT NULL DEFAULT 'number',
                start_at TEXT, startlist_token TEXT,
                FOREIGN KEY (competition_id) REFERENCES competitions(id) ON DELETE CASCADE,
                UNIQUE (competition_id, heat_number)
            );
            CREATE TABLE IF NOT EXISTS heat_categories (
                heat_id INTEGER NOT NULL, category_id INTEGER NOT NULL, PRIMARY KEY (heat_id, category_id),
                FOREIGN KEY (heat_id) REFERENCES heats(id) ON DELETE CASCADE, FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS heat_participants (
                heat_id INTEGER NOT NULL, participant_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                sort_order INTEGER,
                PRIMARY KEY (heat_id, participant_id), FOREIGN KEY (heat_id) REFERENCES heats(id) ON DELETE CASCADE,
                FOREIGN KEY (participant_id) REFERENCES participants(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS races (
                id INTEGER PRIMARY KEY AUTOINCREMENT, participant_id INTEGER NOT NULL, competition_id INTEGER NOT NULL,
                heat_id INTEGER, laps_total INTEGER NOT NULL DEFAULT 1, planned_laps INTEGER,
                start_time TEXT NOT NULL, finish_time TEXT,
                status TEXT NOT NULL DEFAULT 'running', laps_behind INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (participant_id) REFERENCES participants(id) ON DELETE CASCADE,
                FOREIGN KEY (competition_id) REFERENCES competitions(id) ON DELETE CASCADE, FOREIGN KEY (heat_id) REFERENCES heats(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS laps (
                id INTEGER PRIMARY KEY AUTOINCREMENT, race_id INTEGER NOT NULL, lap_number INTEGER NOT NULL,
                lap_time TEXT NOT NULL, user_id INTEGER, user_login TEXT,
                FOREIGN KEY (race_id) REFERENCES races(id) ON DELETE CASCADE, UNIQUE (race_id, lap_number)
            );
            CREATE TABLE IF NOT EXISTS auto_laps (
                id INTEGER PRIMARY KEY AUTOINCREMENT, race_id INTEGER NOT NULL, lap_number INTEGER NOT NULL,
                scan_time TEXT NOT NULL, rfid_tag TEXT NOT NULL, FOREIGN KEY (race_id) REFERENCES races(id) ON DELETE CASCADE, UNIQUE (race_id, lap_number)
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        _create_timing_log(conn)

        for k, v in ITT_DEFAULTS.items():
            conn.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (k, v))
        for k, v in RFID_DEFAULTS.items():
            conn.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (k, v))
        for k, v in PROTOCOL_DEFAULTS.items():
            conn.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (k, v))

        if not conn.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            conn.execute(
                'INSERT INTO users (username, password_hash, is_admin, role, created_at) VALUES (?, ?, 1, ?, ?)',
                (AUTH_USERNAME, generate_password_hash(AUTH_PASSWORD), 'admin', datetime.now().isoformat())
            )
            print(f'👤 Создан администратор по умолчанию: {AUTH_USERNAME}', flush=True)

        conn.executescript('''
            CREATE INDEX IF NOT EXISTS idx_participants_competition ON participants(competition_id);
            CREATE INDEX IF NOT EXISTS idx_categories_competition   ON categories(competition_id);
            CREATE INDEX IF NOT EXISTS idx_heats_competition        ON heats(competition_id);
            CREATE INDEX IF NOT EXISTS idx_races_competition        ON races(competition_id);
            CREATE INDEX IF NOT EXISTS idx_races_participant        ON races(participant_id);
            CREATE INDEX IF NOT EXISTS idx_races_heat               ON races(heat_id);
            CREATE INDEX IF NOT EXISTS idx_laps_race                ON laps(race_id);
            CREATE INDEX IF NOT EXISTS idx_auto_laps_race           ON auto_laps(race_id);
            CREATE INDEX IF NOT EXISTS idx_laps_race_lap            ON laps(race_id, lap_number);
            CREATE INDEX IF NOT EXISTS idx_participants_comp_number ON participants(competition_id, number);
            CREATE INDEX IF NOT EXISTS idx_races_participant_status ON races(participant_id, status);
            CREATE INDEX IF NOT EXISTS idx_races_heat_status        ON races(heat_id, status);
        ''')

        conn.commit()
        _reload_settings_cache_from_db(conn, path or DB_PATH)
        _get_competition(conn)   # в базе всегда есть одно соревнование
    finally:
        conn.close()

_competitions_migrated = False


def _migrate_competitions():
    """Обновляет схему во всех базах соревнований (один раз за процесс)."""
    global _competitions_migrated
    if _competitions_migrated:
        return
    _competitions_migrated = True
    try:
        reg = _registry_load()
    except Exception:
        return
    for c in reg.get('competitions', []):
        path = _comp_db_path(c)
        if not os.path.exists(path):
            continue
        try:
            init_db(path)
        except Exception:
            try:
                _app_logger.exception('Миграция базы не удалась: %s', path)
            except Exception:
                pass


def format_secs(s):
    if s < 0: s = 0
    sec = int(round(s))
    return f'{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}'

def format_secs_short(s):
    if s < 0: s = 0
    total_sec = int(round(s))
    return f'{total_sec // 60:02d}:{total_sec % 60:02d}'

def _fmt_eta_short(secs):
    if secs is None: return None
    s = int(round(secs))
    if s <= 0: return 'сейчас'
    if s < 60: return f'{s} с'
    return f'{s // 60}:{s % 60:02d}'


def _fmt_hms(secs):
    """Секунды → «мм:сс» или «ч:мм:сс» для времени в пути."""
    if secs is None:
        return '—'
    s = max(0, int(round(secs)))
    if s >= 3600:
        return f'{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}'
    return f'{s // 60:02d}:{s % 60:02d}'

MAX_TAGS_PER_NUMBER = 2


def _tags_by_number(conn, cid):
    """{номер: [метка1, метка2]} — для списка участников."""
    out = {}
    for r in conn.execute(
            'SELECT number, tag FROM bib_tags WHERE competition_id = ? ORDER BY number, slot', (cid,)).fetchall():
        out.setdefault(r['number'], []).append(r['tag'])
    return out


def _save_bib_tags(conn, cid, number, tags):
    """Заменяет набор меток номера. tags — список до 2 непустых строк. Возвращает (ok, ошибка)."""
    cleaned = []
    for tag in tags:
        t = re.sub(r'\s+', '', (tag or '')).upper()
        if not t:
            continue
        if t in cleaned:
            return False, 'Одна и та же метка указана дважды'
        cleaned.append(t)
    if len(cleaned) > MAX_TAGS_PER_NUMBER:
        return False, f'На номере может быть не больше {MAX_TAGS_PER_NUMBER} меток'
    for t in cleaned:
        other = conn.execute(
            'SELECT number FROM bib_tags WHERE competition_id = ? AND tag = ? AND number != ?',
            (cid, t, number)).fetchone()
        if other:
            return False, f'Метка {t} уже наклеена на номер {other["number"]}'
    with conn:
        conn.execute('DELETE FROM bib_tags WHERE competition_id = ? AND number = ?', (cid, number))
        for i, t in enumerate(cleaned, start=1):
            conn.execute(
                'INSERT INTO bib_tags (competition_id, number, tag, slot, created_at) VALUES (?, ?, ?, ?, ?)',
                (cid, number, t, i, datetime.now().isoformat(timespec='seconds')))
    return True, None


def _participant_by_tag(conn, tag):
    """Участник, чей номер помечен этой меткой."""
    if not tag:
        return None
    return conn.execute(
        'SELECT p.id, p.number, p.full_name, p.competition_id, bt.tag AS tag, bt.slot AS slot '
        'FROM bib_tags bt JOIN participants p '
        '  ON p.competition_id = bt.competition_id AND p.number = bt.number '
        'WHERE bt.tag = ?', (tag,)).fetchone()


def _load_auto_laps_by_race(conn, race_ids):
    if not race_ids: return {}
    placeholders = ','.join('?' * len(race_ids))
    rows = conn.execute(f'SELECT * FROM auto_laps WHERE race_id IN ({placeholders}) ORDER BY race_id, lap_number', race_ids).fetchall()
    out = {}
    for r in rows: out.setdefault(r['race_id'], []).append(r)
    return out

def _merge_laps(manual_rows, auto_rows, min_lap_sec, laps_total=None):
    """Объединяет ручные и авто-отметки в одну хронологию без дублей (чистая функция)."""
    marks = [{'time': a['scan_time'], 'source': 'A', 'id': a['id'], 'user': None} for a in auto_rows]

    for m in manual_rows:
        m_dt = datetime.fromisoformat(m['lap_time'])
        conflict = any(
            abs((m_dt - datetime.fromisoformat(a['scan_time'])).total_seconds()) <= min_lap_sec
            for a in auto_rows)
        if not conflict:
            marks.append({'time': m['lap_time'], 'source': 'M', 'id': m['id'], 'user': m['user_login']})

    marks.sort(key=lambda x: x['time'])
    return marks[:laps_total] if laps_total is not None else marks

def _build_merged_laps(conn, race_id, min_lap_sec, laps_total=None):
    """Обёртка _merge_laps для одиночного заезда (загружает отметки из БД)."""
    auto = conn.execute('SELECT id, scan_time FROM auto_laps WHERE race_id = ? ORDER BY lap_number', (race_id,)).fetchall()
    manual = conn.execute('SELECT id, lap_time, user_login FROM laps WHERE race_id = ? ORDER BY lap_number', (race_id,)).fetchall()
    return _merge_laps(manual, auto, min_lap_sec, laps_total)

def _load_laps_by_race(conn, race_ids):
    if not race_ids: return {}
    placeholders = ','.join('?' * len(race_ids))
    rows = conn.execute(f'SELECT * FROM laps WHERE race_id IN ({placeholders}) ORDER BY race_id, lap_number', race_ids).fetchall()
    out = {}
    for r in rows: out.setdefault(r['race_id'], []).append(r)
    return out

def prepare_race(row, conn, laps_rows=None, auto_laps_rows=None, min_lap=None, include_auto=False):
    d = dict(row)
    # planned_laps — дистанция по регламенту (из категории). laps_total может быть
    # уменьшен механикой «финиш после лидера», поэтому для показа берём план.
    if not d.get('planned_laps'):
        d['planned_laps'] = d.get('laps_total') or 1
    # номер может отсутствовать (выдаётся на старте) — для сортировок приводим к числу
    if d.get('number') is None:
        d['number'] = 0
    d['number_display'] = d['number'] or '—'
    start = datetime.fromisoformat(d['start_time'])
    d['start_display'] = start.strftime('%H:%M:%S')
    d['start_dt_local'] = start.strftime('%Y-%m-%dT%H:%M:%S')
    d['start_epoch_ms'] = int(start.timestamp() * 1000)
    d['sort_time'] = float('inf')

    if include_auto:
        if min_lap is None:
            h = conn.execute('SELECT min_lap_time FROM heats WHERE id = ?', (d.get('heat_id'),)).fetchone()
            min_lap = h['min_lap_time'] if h and h['min_lap_time'] is not None else 0
        if auto_laps_rows is None:
            auto_laps_rows = conn.execute('SELECT id, scan_time FROM auto_laps WHERE race_id = ? ORDER BY lap_number', (d['id'],)).fetchall()
        if laps_rows is None:
            laps_rows = conn.execute('SELECT id, lap_time, user_login FROM laps WHERE race_id = ? ORDER BY lap_number', (d['id'],)).fetchall()
        marks = _merge_laps(laps_rows, auto_laps_rows, min_lap, d['laps_total'])

        laps = []
        prev = start
        last_elapsed = None
        for i, m in enumerate(marks):
            lap_dt = datetime.fromisoformat(m['time'])
            elapsed = (lap_dt - start).total_seconds()
            split_secs = (lap_dt - prev).total_seconds()
            lap = {
                'lap_number': i + 1,
                'lap_time': m['time'],
                'source': m['source'],
                'id': m.get('id'),
                'user': m.get('user'),
                'display': lap_dt.strftime('%H:%M:%S'),
                'elapsed_seconds': elapsed,
                'elapsed_display': format_secs(elapsed),
                'split_seconds': split_secs,
                'split_display': format_secs(split_secs),
            }
            prev = lap_dt
            last_elapsed = elapsed
            laps.append(lap)
        d['laps'] = laps
        d['last_lap_display'] = laps[-1]['elapsed_display'] if laps else None

        orig_status = d.get('status') or 'running'
        if orig_status not in ('dns', 'dnf', 'lapped'):
            if len(laps) >= d['laps_total']:
                finish_dt = datetime.fromisoformat(laps[d['laps_total'] - 1]['lap_time'])
                d['finish_time'] = finish_dt.isoformat()
                d['finish_display'] = finish_dt.strftime('%H:%M:%S')
                d['total_display'] = format_secs((finish_dt - start).total_seconds())
                d['sort_time'] = (finish_dt - start).total_seconds()
                d['status'] = 'ok'
                d['status_display'] = 'OK'
                d['result_display'] = d['total_display']
            else:
                d['finish_time'] = None
                d['finish_display'] = None
                d['total_display'] = None
                d['status'] = 'running'
                d['status_display'] = '—'
                d['result_display'] = ''
                if last_elapsed is not None:
                    d['sort_time'] = last_elapsed

        status = d.get('status') or 'running'
        if status == 'ok':
            d['status_display'] = 'OK'; d['result_display'] = d['total_display'] or '—'
        elif status == 'dns':
            d['status_display'] = 'DNS'; d['result_display'] = 'DNS'
        elif status == 'dnf':
            d['status_display'] = 'DNF'; d['result_display'] = 'DNF'
        elif status == 'lapped':
            d['status_display'] = f'+{d.get("laps_behind", 0)} кр.'; d['result_display'] = d['status_display']
        else:
            d['status_display'] = '—'; d['result_display'] = ''
        return d

    if d['finish_time']:
        finish = datetime.fromisoformat(d['finish_time'])
        d['finish_display'] = finish.strftime('%H:%M:%S')
        d['total_display'] = format_secs((finish - start).total_seconds())
        d['sort_time'] = (finish - start).total_seconds()
    else:
        d['finish_display'] = None
        d['total_display'] = None

    if laps_rows is None:
        laps_rows = conn.execute('SELECT * FROM laps WHERE race_id = ? ORDER BY lap_number', (d['id'],)).fetchall()

    laps = []
    prev = start
    last_elapsed = None
    for lap_row in laps_rows:
        lap = dict(lap_row)
        lap_dt = datetime.fromisoformat(lap['lap_time'])
        elapsed = (lap_dt - start).total_seconds()
        split_secs = (lap_dt - prev).total_seconds()
        lap.update({
            'display': lap_dt.strftime('%H:%M:%S'), 'elapsed_seconds': elapsed,
            'elapsed_display': format_secs(elapsed), 'split_seconds': split_secs,
            'split_display': format_secs(split_secs),
            'source': 'M',
            'user': lap.get('user_login'),
        })
        prev = lap_dt
        last_elapsed = elapsed
        laps.append(lap)
    d['laps'] = laps
    d['last_lap_display'] = laps[-1]['elapsed_display'] if laps else None

    if not d['finish_time'] and last_elapsed is not None:
        d['sort_time'] = last_elapsed

    status = d.get('status') or 'running'
    if status == 'ok':
        d['status_display'] = 'OK'; d['result_display'] = d['total_display'] or '—'
    elif status == 'dns':
        d['status_display'] = 'DNS'; d['result_display'] = 'DNS'
    elif status == 'dnf':
        d['status_display'] = 'DNF'; d['result_display'] = 'DNF'
    elif status == 'lapped':
        d['status_display'] = f'+{d.get("laps_behind", 0)} кр.'; d['result_display'] = d['status_display']
    else:
        d['status_display'] = '—'; d['result_display'] = ''
    return d

def _predict_race(race, now=None, fallback_lap_secs=None):
    if now is None: now = datetime.now()
    start_dt = datetime.fromisoformat(race['start_time'])
    laps = race.get('laps') or []
    laps_done = len(laps)
    remaining = max(0, race['laps_total'] - laps_done)
    last_lap_dt = datetime.fromisoformat(laps[-1]['lap_time']) if laps else start_dt

    if laps:
        splits = [l['split_seconds'] for l in laps]
        predicted = splits[-1] * 0.6 + (sum(splits) / len(splits)) * 0.4
        predicted_source = 'own'
    elif fallback_lap_secs:
        predicted = fallback_lap_secs
        predicted_source = 'heat'
    else:
        predicted = None
        predicted_source = None

    race['predicted_lap_secs'] = predicted
    race['predicted_source'] = predicted_source

    if predicted is None or remaining == 0:
        race.update({'eta_next_event_secs': None, 'eta_next_display': None, 'overdue_next': False,
                     'eta_finish_secs': None, 'eta_finish_display': None, 'next_event_label': None, 'is_finishing': False})
        return race

    elapsed = (now - last_lap_dt).total_seconds()
    eta_next = predicted - elapsed
    race['eta_next_event_secs'] = eta_next
    race['overdue_next'] = (eta_next <= 0)
    race['is_finishing'] = (remaining == 1)

    if remaining == 1:
        race['next_event_label'] = 'финиш'
        race['eta_finish_secs'] = eta_next
    else:
        race['next_event_label'] = f'круг {laps_done + 1}'
        race['eta_finish_secs'] = eta_next + predicted * (remaining - 1)

    race['eta_next_display'] = _fmt_eta_short(eta_next)
    race['eta_finish_display'] = _fmt_eta_short(race['eta_finish_secs'])
    return race

def maybe_finish_heat(conn, hid):
    if not hid: return
    h = conn.execute('SELECT status, format FROM heats WHERE id = ?', (hid,)).fetchone()
    if not h or h['status'] != 'running': return
    if conn.execute("SELECT COUNT(*) FROM races WHERE heat_id = ? AND status = 'running'", (hid,)).fetchone()[0] == 0:
        # разделка: пока есть не стартовавшие, старт не закрываем
        if _heat_format(h) == 'itt':
            waiting = conn.execute(
                "SELECT COUNT(*) FROM heat_participants hp WHERE hp.heat_id = ? AND hp.status = 'active' "
                "AND NOT EXISTS (SELECT 1 FROM races r WHERE r.heat_id = hp.heat_id "
                "                AND r.participant_id = hp.participant_id)", (hid,)).fetchone()[0]
            if waiting:
                return
        conn.execute("UPDATE heats SET status = 'finished', finished_at = ? WHERE id = ?", (datetime.now().isoformat(), hid))

def _process_race_finish(conn, race, now_iso, force=False):
    h = conn.execute('SELECT min_lap_time FROM heats WHERE id = ?', (race['heat_id'],)).fetchone()
    min_lap = h['min_lap_time'] if h and h['min_lap_time'] is not None else 0
    merged = _build_merged_laps(conn, race['id'], min_lap)
    laps_done = len(merged)

    if force or not merged:
        finish_time = now_iso
    else:
        finish_time = merged[-1]['time']

    cat_id = conn.execute("SELECT category_id FROM participants WHERE id = ?", (race['participant_id'],)).fetchone()['category_id']
    leader_row = conn.execute(
        "SELECT MAX(laps_count) FROM (SELECT COUNT(*) as laps_count FROM laps JOIN races ON races.id = laps.race_id "
        "WHERE races.heat_id = ? AND races.status = 'ok' AND races.participant_id IN (SELECT id FROM participants WHERE category_id = ?) GROUP BY races.id)",
        (race['heat_id'], cat_id)
    ).fetchone()
    laps_behind = max(0, (leader_row[0] if leader_row and leader_row[0] else laps_done) - laps_done)
    conn.execute("UPDATE races SET finish_time = ?, status = 'ok', laps_behind = ? WHERE id = ?", (finish_time, laps_behind, race['id']))

    finished_count = conn.execute(
        "SELECT COUNT(*) FROM races WHERE heat_id = ? AND status = 'ok' AND participant_id IN (SELECT id FROM participants WHERE category_id = ?)",
        (race['heat_id'], cat_id)
    ).fetchone()[0]

    h2 = conn.execute('SELECT finish_on_leader FROM heats WHERE id = ?', (race['heat_id'],)).fetchone()
    if h2 and h2['finish_on_leader'] and finished_count == 1:
        others = conn.execute("SELECT id FROM races WHERE heat_id = ? AND status = 'running' AND participant_id IN (SELECT id FROM participants WHERE category_id = ?)", (race['heat_id'], cat_id)).fetchall()
        for o in others:
            o_laps = len(_build_merged_laps(conn, o['id'], min_lap))
            conn.execute("UPDATE races SET laps_total = ? WHERE id = ?", (o_laps + 1, o['id']))
    maybe_finish_heat(conn, race['heat_id'])

def _force_finish_heat(conn, hid, dnf_ids=None):
    h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
    if not h or h['status'] != 'running': return False
    dnf_ids = set(dnf_ids or [])
    races = conn.execute('SELECT r.*, p.category_id FROM races r JOIN participants p ON p.id = r.participant_id WHERE r.heat_id = ?', (hid,)).fetchall()
    lap_counts = _race_lap_counts(conn, [r['id'] for r in races])
    cat_max_laps = {}
    for r in races:
        if r['category_id'] not in cat_max_laps or lap_counts[r['id']] > cat_max_laps[r['category_id']]:
            cat_max_laps[r['category_id']] = lap_counts[r['id']]
    for r in races:
        if r['status'] != 'running': continue
        if r['id'] in dnf_ids:
            conn.execute("UPDATE races SET status = 'dnf', laps_behind = 0 WHERE id = ?", (r['id'],))
            continue
        laps = lap_counts[r['id']]
        cat_max = cat_max_laps.get(r['category_id'], 0)
        if cat_max == 0:
            conn.execute("UPDATE races SET status = 'dnf', laps_behind = 0 WHERE id = ?", (r['id'],))
            continue
        conn.execute("UPDATE races SET laps_total = ? WHERE id = ?", (cat_max, r['id']))
        if laps >= cat_max:
            last_lap = conn.execute('SELECT lap_time FROM laps WHERE race_id = ? ORDER BY lap_number DESC LIMIT 1', (r['id'],)).fetchone()
            conn.execute("UPDATE races SET status = 'ok', finish_time = ?, laps_behind = 0 WHERE id = ?", (last_lap['lap_time'] if last_lap else r['start_time'], r['id']))
        else:
            conn.execute("UPDATE races SET status = 'lapped', laps_behind = ? WHERE id = ?", (cat_max - laps, r['id']))
    conn.execute("UPDATE heats SET status = 'finished', finished_at = ? WHERE id = ?", (datetime.now().isoformat(), hid))
    return True

def _normalize_gender(raw):
    if not raw: return None
    g = str(raw).strip().upper().replace('Ё', 'Е')
    if g in ('М', 'M', 'МУЖ', 'МУЖСКОЙ', 'MALE', 'МАЛЬЧИК'): return 'M'
    if g in ('Ж', 'F', 'ЖЕН', 'ЖЕНСКИЙ', 'FEMALE', 'ДЕВОЧКА'): return 'F'
    if g.startswith('МУЖ') or g.startswith('MALE'): return 'M'
    if g.startswith('ЖЕН') or g.startswith('FEMALE'): return 'F'
    return None

def _parse_year(raw):
    """Год рождения. Две цифры трактуются по правилу: больше текущих двух цифр — 19XX, иначе 20XX.

    Полный год (4 цифры) берётся как есть. Всё остальное — None.
    """
    if raw is None: return None
    s = str(raw).strip()
    if not s: return None
    try: num = int(float(s.replace(',', '.')))
    except (TypeError, ValueError): return None
    if num < 0: return None
    if num < 100:
        current_yy = datetime.now().year % 100
        return (1900 if num > current_yy else 2000) + num
    if 1000 <= num <= 2100: return num
    return None

def _qr_svg(data, box=6, border=2):
    """QR-код как inline-SVG. Возвращает None, если библиотеки нет."""
    try:
        import qrcode
        qr = qrcode.QRCode(border=border, box_size=box, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(data)
        qr.make(fit=True)
        matrix = qr.get_matrix()
    except Exception:
        return None
    size = len(matrix) * box
    cells = []
    for y, row in enumerate(matrix):
        for x, filled in enumerate(row):
            if filled:
                cells.append(f'<rect x="{x * box}" y="{y * box}" width="{box}" height="{box}"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
            f'viewBox="0 0 {size} {size}" role="img" aria-label="QR-код со ссылкой на страницу">'
            f'<rect width="{size}" height="{size}" fill="#ffffff"/>'
            f'<g fill="#111827">{"".join(cells)}</g></svg>')

def _parse_duration(raw):
    """Длительность в секунды: «01:23:45», «23:45», «5025» или «1:02:03,5».

    Возвращает float или None, если строку не удалось разобрать.
    """
    if raw is None: return None
    s = str(raw).strip().replace(',', '.')
    if not s: return None
    if ':' in s:
        parts = s.split(':')
        if len(parts) > 3: return None
        total = 0.0
        for part in parts:
            try: value = float(part)
            except ValueError: return None
            if value < 0: return None
            total = total * 60 + value
        return total
    try: return float(s)
    except ValueError: return None

# ---------------------- Общие помощники (формат, счётчики, публичные ссылки) ----------------------

def _to_float(raw):
    """Число из строки с запятой-разделителем («2,3» → 2.3); None, если не число."""
    try:
        return float(str(raw or '').replace(',', '.'))
    except (TypeError, ValueError):
        return None

def _speed_kmh(sec, km):
    """Средняя скорость в км/ч строкой с запятой («27,1»); пусто, если нет данных."""
    if not km or not sec:
        return ''
    return f'{km / (sec / 3600):.1f}'.replace('.', ',')

def _scalar(conn, sql, *args):
    """Первый столбец первой строки запроса (None, если пусто)."""
    row = conn.execute(sql, args).fetchone()
    return row[0] if row else None

def _count_races(races):
    """Счётчики статусов заездов: total, running, ok, dnf, lapped."""
    counts = {'total': len(races), 'running': 0, 'ok': 0, 'dnf': 0, 'lapped': 0}
    for r in races:
        status = r.get('status')
        if status in counts:
            counts[status] += 1
    return counts

def _assign_category_gaps(races):
    """Проставляет gap_display/gap_kind внутри одной категории: лидер + отставание от него."""
    leader = None
    for r in races:
        if not r['laps']:
            continue
        if (leader is None
                or len(r['laps']) > len(leader['laps'])
                or (len(r['laps']) == len(leader['laps']) and r['sort_time'] < leader['sort_time'])):
            leader = r

    for r in races:
        gap, kind = '', ''
        if leader and r['laps']:
            if r['id'] == leader['id']:
                gap, kind = 'лидер', 'leader'
            else:
                lap_diff = len(leader['laps']) - len(r['laps'])
                if lap_diff > 0:
                    gap, kind = f'+{lap_diff} кр.', 'laps'
                else:
                    diff = r['sort_time'] - leader['sort_time']
                    if diff >= 0.05:
                        gap = f'+{diff:.1f}' if diff < 60 else '+' + format_secs_short(diff)
                        kind = 'time'
        r['gap_display'] = gap
        r['gap_kind'] = kind
    return races

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

def _norm_name(s):
    return ' '.join(str(s).lower().replace('ё', 'е').split()) if s else ''

# ---------------------- Разбор заголовков Excel при импорте ----------------------
HEADER_ALIASES = {
    'number':   ['номер', 'номер на старте', 'стартовый номер', 'bib', 'number'],
    'fio':      ['фио', 'ф и о', 'фамилия имя', 'фамилия и имя', 'full name', 'fullname', 'имя', 'фи'],
    'year':     ['год рождения', 'год рожд', 'год', 'birth year', 'birthyear', 'г р'],
    'category': ['категория', 'категория (группа)', 'группа', 'category', 'кат'],
    'gender':   ['пол', 'gender', 'sex'],
    'rfid':     ['rfid', 'метка', 'tag', 'epc'],
    'city':     ['город', 'населенный пункт', 'населённый пункт', 'city'],
    'team':     ['команда', 'клуб', 'team', 'организация'],
}

def _norm_header(h):
    """Заголовок к сравнимому виду: регистр, ё→е, пунктуация → пробел."""
    if h is None: return ''
    s = str(h).strip().lower().replace('ё', 'е')
    s = re.sub(r'[^0-9a-zа-я]+', ' ', s)
    return ' '.join(s.split())

def _word_matches(word, alias):
    """Слово заголовка совпадает с псевдонимом, допуская падежные окончания."""
    if word == alias: return True
    return len(alias) >= 4 and word.startswith(alias[:-1])

def _find_header_col(header, aliases):
    """Ищет колонку: точное имя, затем отдельное слово, затем вхождение в слово.

    Нужно, чтобы «RFID-метка», «RFID-метка (EPC)», «Номер метки» и т.п.
    находились, а не только точные имена из списка. Вхождение засчитывается
    только с начала слова — иначе «Отметка времени» попала бы в RFID.
    """
    for a in aliases:
        if a in header: return header.index(a)
    for i, h in enumerate(header):
        if any(_word_matches(w, a) for w in h.split() for a in aliases): return i
    for i, h in enumerate(header):
        for a in aliases:
            if len(a) < 4: continue
            if re.search(r'(?<![0-9a-zа-я])' + re.escape(a[:-1]), h): return i
    return None

def _cell_text(v):
    """Текст ячейки без хвостов вида «123.0» и «1.23e+23» у числовых значений."""
    if v is None: return ''
    if isinstance(v, float) and v.is_integer(): return str(int(v))
    return str(v).strip()

DEFAULT_COMPETITION_NAME = os.environ.get('CHRONO_COMPETITION', 'Соревнование')

def _get_competition(conn):
    """Единственное соревнование в базе. Создаётся автоматически при первом запуске."""
    comp = conn.execute('SELECT * FROM competitions ORDER BY id LIMIT 1').fetchone()
    if comp: return comp
    with conn:
        cid = conn.execute('INSERT INTO competitions (name) VALUES (?)', (DEFAULT_COMPETITION_NAME,)).lastrowid
    return conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()

def _next_reg_number(conn, cid):
    return max(REG_NUMBER_BASE, conn.execute('SELECT COALESCE(MAX(number), 0) FROM participants WHERE competition_id = ?', (cid,)).fetchone()[0] + 1)


def _next_number(conn, cid):
    """Следующий свободный стартовый номер."""
    current = conn.execute('SELECT COALESCE(MAX(number), 0) FROM participants WHERE competition_id = ?',
                           (cid,)).fetchone()[0]
    return int(current or 0) + 1


def _plabel(row):
    """Подпись участника в сообщениях: номер, а если его нет — ID."""
    number = row['number'] if 'number' in row.keys() else None
    return f'№{number}' if number else f'ID {row["id"]}'


def _issue_number(conn, cid, pid, raw_number):
    """Выдаёт участнику стартовый номер. Возвращает (номер, ошибка)."""
    raw = (raw_number or '').strip()
    if not raw:
        return None, 'Укажите номер'
    try:
        number = int(raw)
    except ValueError:
        return None, 'Номер должен быть числом'
    if number < 1:
        return None, 'Номер должен быть больше нуля'
    row = conn.execute('SELECT id FROM participants WHERE competition_id = ? AND number = ? AND id != ?',
                       (cid, number, pid)).fetchone()
    if row:
        owner = conn.execute('SELECT id, full_name FROM participants WHERE id = ?', (row['id'],)).fetchone()
        return None, f'Номер {number} уже занят участником {owner["full_name"]} (ID {owner["id"]})'
    person = conn.execute('SELECT * FROM participants WHERE id = ? AND competition_id = ?', (pid, cid)).fetchone()
    if not person:
        return None, 'Участник не найден'
    with conn:
        conn.execute('UPDATE participants SET number = ? WHERE id = ?', (number, pid))
    return number, None


# ---- условия категорий: пол и диапазон года рождения ----

def _cat_genders(value):
    """Допустимые полы категории. Пусто — оба пола."""
    s = (value or '').upper()
    allowed = {g for g in ('M', 'F') if g in s}
    return allowed or {'M', 'F'}


def _join_genders(values):
    """['M','F'] → 'MF' (в стабильном порядке)."""
    chosen = {v for v in (values or []) if v in ('M', 'F')}
    return ''.join(g for g in ('M', 'F') if g in chosen) or None


def _category_matches(cat, gender, year):
    """Подходит ли участник под условия категории."""
    allowed = _cat_genders(cat['gender'])
    if gender:
        if gender.upper() not in allowed:
            return False
    elif allowed != {'M', 'F'}:
        # пол участника неизвестен — мужскую/женскую категорию не назначаем
        return False
    yf, yt = cat['year_from'], cat['year_to']
    if yf is not None or yt is not None:
        if year is None:
            return False
        if yf is not None and year < yf:
            return False
        if yt is not None and year > yt:
            return False
    return True


def _auto_category(conn, cid, gender, year):
    """Первая подходящая категория по порядку создания (id)."""
    for cat in conn.execute('SELECT * FROM categories WHERE competition_id = ? ORDER BY id', (cid,)).fetchall():
        if _category_matches(cat, gender, year):
            return cat['id']
    return None


def _cats_overlap(a, b):
    """Пересекаются ли условия двух категорий (пол + годы)."""
    if not (_cat_genders(a['gender']) & _cat_genders(b['gender'])):
        return False
    af, at = a['year_from'], a['year_to']
    bf, bt = b['year_from'], b['year_to']
    lows = [x for x in (af, bf) if x is not None]
    highs = [x for x in (at, bt) if x is not None]
    low = max(lows) if lows else None
    high = min(highs) if highs else None
    if low is not None and high is not None and low > high:
        return False
    return True


def _cat_has_conditions(cat):
    return bool(cat['gender']) or cat['year_from'] is not None or cat['year_to'] is not None


def _category_overlaps(categories):
    """{id: [названия пересекающихся]} — только для подсветки, ввод не запрещаем."""
    result = {}
    cats = list(categories)
    for i, a in enumerate(cats):
        names = [b['name'] for j, b in enumerate(cats)
                 if j != i and (_cat_has_conditions(a) or _cat_has_conditions(b)) and _cats_overlap(a, b)]
        if names:
            result[a['id']] = names
    return result

_competition_meta = None  # кэш (id, name) единственного соревнования

def _invalidate_competition_meta():
    global _competition_meta
    _competition_meta = None

@app.context_processor
def _inject_competition():
    """Название и id текущего соревнования, признак активного старта — для шапки.

    Кэш привязан к выбранному соревнованию (slug), а не глобально.
    """
    global _competition_meta
    try:
        slug = session.get('comp')
        if _competition_meta is None or _competition_meta[0] != slug:
            conn = get_db()
            try:
                comp = _get_competition(conn)
                _competition_meta = (slug, comp['id'], comp['name'])
            finally:
                conn.close()
        _, cid, name = _competition_meta
        conn = get_db()
        try:
            running = conn.execute("SELECT 1 FROM heats WHERE competition_id = ? AND status = 'running' LIMIT 1",
                                   (cid,)).fetchone()
        finally:
            conn.close()
        return {'current_cid': cid, 'competition_name': name, 'has_running_heat': bool(running),
                'current_comp_slug': slug, 'read_only': _is_read_only(slug)}
    except Exception:
        return {'current_cid': None, 'competition_name': None, 'has_running_heat': False,
                'current_comp_slug': session.get('comp'), 'read_only': False}

def _category_participants(conn, cat_id):
    """Участники категории. cat_id = 0 — участники без группы (category_id IS NULL)."""
    if cat_id:
        return conn.execute('SELECT id, number, full_name, birth_year, gender FROM participants WHERE category_id = ? ORDER BY number', (cat_id,)).fetchall()
    return conn.execute('SELECT id, number, full_name, birth_year, gender FROM participants WHERE category_id IS NULL ORDER BY number').fetchall()

def _race_lap_counts(conn, race_ids):
    result = {rid: 0 for rid in race_ids}
    if not race_ids: return result
    placeholders = ','.join('?' * len(race_ids))
    for r in conn.execute(f'SELECT race_id, COUNT(*) as c FROM laps WHERE race_id IN ({placeholders}) GROUP BY race_id', race_ids).fetchall():
        result[r['race_id']] = r['c']
    return result

def _push_manual_log(entry):
    log = session.get('manual_log', [])
    log.insert(0, {'ok': entry.get('ok'), 'number': entry.get('number'), 'time': entry.get('time'), 'action': entry.get('action') or entry.get('message') or ''})
    session['manual_log'] = log[:LOG_SIZE]

def _get_manual_data(conn, cid):
    competition = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
    active_races = []
    if competition:
        races_rows = conn.execute('''
            SELECT r.*, p.number, p.full_name, h.heat_number, h.finish_on_leader, c.name AS category_name
            FROM races r JOIN participants p ON p.id = r.participant_id JOIN heats h ON h.id = r.heat_id
            LEFT JOIN categories c ON c.id = p.category_id WHERE r.competition_id = ? AND r.status = 'running' ORDER BY h.heat_number, p.number
        ''', (cid,)).fetchall()
        laps_map = _load_laps_by_race(conn, [r['id'] for r in races_rows])
        active_races = [prepare_race(r, conn, laps_rows=laps_map.get(r['id'], []), include_auto=True) for r in races_rows]

        active_races = [r for r in active_races if r['status'] == 'running']

        heat_splits = {}
        for r in active_races:
            for l in r['laps']: heat_splits.setdefault(r['heat_id'], []).append(l['split_seconds'])
        heat_avg = {hid: sum(v)/len(v) for hid, v in heat_splits.items() if v}

        now = datetime.now()
        active_races = [_predict_race(r, now, heat_avg.get(r['heat_id'])) for r in active_races]
        active_races.sort(key=lambda r: (1, float('inf'), r['number']) if r.get('eta_next_event_secs') is None else (0, r['eta_next_event_secs'], r['number']))
        for r in active_races:
            try:
                r['elapsed_display'] = _fmt_hms((now - datetime.fromisoformat(r['start_time'])).total_seconds())
            except (TypeError, ValueError):
                r['elapsed_display'] = '—'
    return competition, active_races


def _running_itt_heat(conn, cid):
    """Активный старт разделки (последний запущенный)."""
    return conn.execute(
        "SELECT * FROM heats WHERE competition_id = ? AND status = 'running' "
        "ORDER BY heat_number DESC LIMIT 1", (cid,)).fetchone()


def _manual_start_queue(conn, hid):
    """Участники старта, которые ещё не стартовали.

    Порядок: ручная расстановка (sort_order), затем — по настройке старта:
    «number» — по номеру, «category» — по ID группы, затем по номеру.
    """
    heat = conn.execute('SELECT start_order FROM heats WHERE id = ?', (hid,)).fetchone()
    mode = 'number'
    if heat is not None:
        try:
            mode = heat['start_order'] or 'number'
        except (KeyError, IndexError, TypeError):
            mode = heat[0] or 'number'
    if mode == 'category':
        # порядок групп — по колонке «сортировка» категории (по умолчанию равна её ID)
        order_by = ('COALESCE(hp.sort_order, 1000000), (c.id IS NULL), '
                    'COALESCE(c.sort_order, c.id), p.number')
    else:
        order_by = 'COALESCE(hp.sort_order, 1000000), p.number'
    return conn.execute(
        'SELECT hp.participant_id, hp.sort_order, p.number, p.full_name, '
        '       p.category_id, c.name AS category_name '
        'FROM heat_participants hp '
        'JOIN participants p ON p.id = hp.participant_id '
        'LEFT JOIN categories c ON c.id = p.category_id '
        "WHERE hp.heat_id = ? AND hp.status = 'active' AND p.number IS NOT NULL "
        '  AND NOT EXISTS (SELECT 1 FROM races r WHERE r.heat_id = hp.heat_id '
        '                  AND r.participant_id = hp.participant_id) '
        f'ORDER BY {order_by} LIMIT ?', (hid, 50)).fetchall()


def _manual_start_state(conn, cid):
    """Состояние табло раздельного старта."""
    comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
    interval = _itt_interval()
    state = {
        'ok': True,
        'format': _comp_format(comp),
        'interval': interval,
        'sound': get_cached_setting('itt_countdown_sound', '1') == '1',
        'now_ts': time.time(),
        'heat': None,
        'queue': [],
        'next': None,
        'last': [],
        'planned_ts': None,
        'total': 0, 'started': 0, 'finished': 0, 'dns': 0, 'waiting': 0,
        'mode': 'none', 'undo_target': None,
    }
    if comp is None:
        return state
    heat = _running_itt_heat(conn, cid)
    state['mode'] = 'running' if heat else 'none'
    if not heat:
        # до запуска старта табло показывает очередь следующего старта — можно переставить порядок
        heat = conn.execute(
            "SELECT * FROM heats WHERE competition_id = ? AND status = 'pending' AND format = 'itt' "
            'ORDER BY heat_number ASC LIMIT 1', (cid,)).fetchone()
        if heat:
            state['mode'] = 'preview'
    if not heat:
        return state
    hid = heat['id']
    state['heat'] = {'id': hid, 'number': heat['heat_number'], 'status': heat['status'],
                     'name': heat['name'] or '', 'started_at': heat['started_at'],
                     'start_order': (heat['start_order'] if 'start_order' in heat.keys() else 'number') or 'number'}

    rows = conn.execute(
        'SELECT hp.participant_id, hp.status AS hp_status, p.number, p.full_name, c.name AS category_name, '
        '       (SELECT r.id FROM races r WHERE r.heat_id = hp.heat_id AND r.participant_id = hp.participant_id) AS race_id, '
        '       (SELECT r.start_time FROM races r WHERE r.heat_id = hp.heat_id AND r.participant_id = hp.participant_id) AS start_time, '
        '       (SELECT r.status FROM races r WHERE r.heat_id = hp.heat_id AND r.participant_id = hp.participant_id) AS race_status '
        'FROM heat_participants hp '
        'JOIN participants p ON p.id = hp.participant_id '
        'LEFT JOIN categories c ON c.id = p.category_id '
        'WHERE hp.heat_id = ? AND p.number IS NOT NULL ORDER BY p.number', (hid,)).fetchall()

    waiting_rows = _manual_start_queue(conn, hid)
    waiting = waiting_rows
    started = [r for r in rows if r['race_id'] is not None and (r['race_status'] or '') != 'dns']
    dns = [r for r in rows if (r['hp_status'] == 'dns') or (r['race_status'] or '') == 'dns']
    finished = [r for r in started if (r['race_status'] or '') == 'ok']

    state['total'] = len(rows)
    state['waiting'] = len(waiting)
    state['started'] = len(started)
    state['finished'] = len(finished)
    state['dns'] = len(dns)
    state['queue'] = [{'pid': r['participant_id'], 'number': r['number'], 'name': r['full_name'],
                       'category': r['category_name'] or ''} for r in waiting[:8]]
    state['next'] = state['queue'][0] if state['queue'] else None

    last_rows = conn.execute(
        'SELECT r.start_time, r.status, p.number, p.full_name FROM races r '
        'JOIN participants p ON p.id = r.participant_id '
        "WHERE r.heat_id = ? AND r.status != 'dns' "
        'ORDER BY r.start_time DESC LIMIT 8', (hid,)).fetchall()
    state['last'] = [{'number': r['number'], 'name': r['full_name'],
                      'time': (r['start_time'] or '')[11:19],
                      'finished': r['status'] == 'ok'} for r in last_rows]

    # кто будет отменён кнопкой «↶» — последний стартовавший и ещё едущий
    undo_row = conn.execute(
        'SELECT p.number, p.full_name FROM races r JOIN participants p ON p.id = r.participant_id '
        "WHERE r.heat_id = ? AND r.status = 'running' ORDER BY r.start_time DESC LIMIT 1", (hid,)).fetchone()
    state['undo_target'] = ({'number': undo_row['number'], 'name': undo_row['full_name']}
                            if undo_row else None)

    if started and waiting:
        times = []
        for r in started:
            try:
                times.append(datetime.fromisoformat(r['start_time']).timestamp())
            except (TypeError, ValueError):
                pass
        if times:
            state['planned_ts'] = max(times) + interval
    return state

def _fmt_start_at(value):
    """Плановое время старта: ISO → «23.11.2026 12:00» (или пусто)."""
    if not value:
        return ''
    try:
        return datetime.fromisoformat(value).strftime('%d.%m.%Y %H:%M')
    except (TypeError, ValueError):
        return ''


def _heat_start_rows(conn, heat):
    """Участники старта в порядке старта: позиция, план/факт время, статус."""
    hid = heat['id']
    try:
        mode = heat['start_order'] or 'number'
    except (KeyError, IndexError):
        mode = 'number'
    rows = conn.execute(
        'SELECT hp.participant_id, hp.status AS hp_status, hp.sort_order, '
        '       p.number, p.full_name, p.birth_year, p.city, p.team, '
        "       COALESCE(c.name, '') AS category_name, c.sort_order AS cat_sort, "
        '       r.id AS race_id, r.status AS race_status, r.start_time, r.finish_time '
        'FROM heat_participants hp '
        'JOIN participants p ON p.id = hp.participant_id '
        'LEFT JOIN categories c ON c.id = p.category_id '
        'LEFT JOIN races r ON r.heat_id = hp.heat_id AND r.participant_id = hp.participant_id '
        'WHERE hp.heat_id = ?', (hid,)).fetchall()

    def rank(r):
        is_dns = (r['hp_status'] == 'dns') or ((r['race_status'] or '') == 'dns')
        if is_dns:
            return (2, 0, 0, r['number'] or 0)
        if r['race_id'] is not None:
            return (0, 0, 0, r['start_time'] or '')
        manual = r['sort_order']
        if manual is not None:
            return (1, 0, manual, r['number'] or 0)
        cat = r['cat_sort'] if r['cat_sort'] is not None else 1000000
        return (1, 1, cat if mode == 'category' else 0, r['number'] or 0)

    rows = sorted(rows, key=rank)
    interval = _itt_interval()
    start_at = None
    try:
        if heat['start_at']:
            start_at = datetime.fromisoformat(heat['start_at'])
    except (KeyError, IndexError, TypeError, ValueError):
        start_at = None

    out, position = [], 0
    for r in rows:
        is_dns = (r['hp_status'] == 'dns') or ((r['race_status'] or '') == 'dns')
        race_status = (r['race_status'] or '') if r['race_id'] is not None else ''
        if is_dns:
            status = 'dns'
        elif race_status in ('ok', 'dnf', 'lapped'):
            status = 'finished'
        elif race_status == 'running':
            status = 'running'
        else:
            status = 'waiting'
        planned = ''
        if not is_dns:
            position += 1
            if start_at is not None:
                planned = (start_at + timedelta(seconds=interval * (position - 1))).strftime('%H:%M:%S')
        actual = (r['start_time'] or '')[11:19] if r['race_id'] is not None and not is_dns else ''
        out.append({
            'participant_id': r['participant_id'],
            'position': position if not is_dns else None,
            'number': r['number'],
            'full_name': r['full_name'],
            'birth_year': r['birth_year'],
            'category': r['category_name'] or '',
            'city': r['city'] or '',
            'team': r['team'] or '',
            'status': status,
            'manual': (r['sort_order'] is not None) and status == 'waiting',
            'planned_display': planned,
            'actual_display': actual,
            'finish_display': (r['finish_time'] or '')[11:19] if status == 'finished' else '',
        })
    return out


def _heat_startlist_data(conn, heat, comp):
    """Данные стартового протокола."""
    rows = _heat_start_rows(conn, heat)
    total = sum(1 for r in rows if r['status'] != 'dns')
    started = sum(1 for r in rows if r['status'] in ('running', 'finished'))
    finished = sum(1 for r in rows if r['status'] == 'finished')
    return {
        'heat': {
            'id': heat['id'],
            'number': heat['heat_number'],
            'name': heat['name'] or '',
            'status': heat['status'],
            'format': _heat_format(heat),
            'interval': _itt_interval(),
            'start_at': _fmt_start_at(heat['start_at'] if 'start_at' in heat.keys() else None),
            'started_display': (heat['started_at'] or '')[11:19] if heat['started_at'] else '',
        },
        'competition': comp['name'] if comp else '',
        'rows': rows,
        'total': total,
        'started': started,
        'finished': finished,
        'date_display': datetime.now().strftime('%d.%m.%Y'),
    }


_TOKEN_ALPHABET = 'abcdefghijkmnpqrstuvwxyz23456789'


def _new_startlist_token(conn):
    """Короткий токен публичной ссылки, уникальный в этой базе."""
    for _ in range(30):
        token = ''.join(secrets.choice(_TOKEN_ALPHABET) for _ in range(8))
        row = conn.execute('SELECT 1 FROM heats WHERE startlist_token = ?', (token,)).fetchone()
        if not row:
            return token
    return secrets.token_hex(6)


def _find_heat_by_token(token):
    """Ищет старт по токену во всех базах соревнований."""
    if not token:
        return None
    try:
        reg = _registry_load()
    except Exception:
        return None
    for entry in reg.get('competitions', []):
        path = _comp_db_path(entry)
        if not os.path.exists(path):
            continue
        conn = None
        try:
            conn = _connect(path)
            h = conn.execute('SELECT * FROM heats WHERE startlist_token = ?', (token,)).fetchone()
            if h:
                comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (h['competition_id'],)).fetchone()
                if comp:
                    return conn, h, comp
        except sqlite3.Error:
            pass
        if conn is not None:
            conn.close()
    return None


def _pdf_font_pair():
    """Шрифт с кириллицей для PDF: (обычный, жирный) или Helvetica."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    regular, bold = 'Helvetica', 'Helvetica-Bold'
    app_dir = os.path.dirname(os.path.abspath(__file__))
    pairs = [
        (r'C:\Windows\Fonts\arial.ttf', r'C:\Windows\Fonts\arialbd.ttf'),
        ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
         '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
        ('/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
         '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf'),
        (os.path.join(app_dir, 'static', 'fonts', 'DejaVuSans.ttf'),
         os.path.join(app_dir, 'static', 'fonts', 'DejaVuSans-Bold.ttf')),
    ]
    for reg_path, bold_path in pairs:
        if not os.path.exists(reg_path):
            continue
        try:
            pdfmetrics.registerFont(TTFont('SlFont', reg_path))
            regular = 'SlFont'
            if os.path.exists(bold_path):
                pdfmetrics.registerFont(TTFont('SlFontBd', bold_path))
                bold = 'SlFontBd'
            break
        except Exception:
            pass
    return regular, bold


def _render_startlist_pdf(comp, data):
    """PDF стартового протокола (A4, один старт)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

    regular, bold = _pdf_font_pair()

    def st(size, font=None, align=TA_LEFT, leading=None):
        return ParagraphStyle(f'sl{size}{font}{align}', fontName=font or regular, fontSize=size,
                              leading=leading or size * 1.25, alignment=align)

    def esc(v):
        return (str(v or '')).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm,
                            title=f'Стартовый протокол — старт №{data["heat"]["number"]}')
    story = []
    story.append(Paragraph(esc(data['competition']), st(15, bold, TA_CENTER)))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph('СТАРТОВЫЙ ПРОТОКОЛ', st(12, bold, TA_CENTER)))
    head_bits = [f'Старт №{data["heat"]["number"]}']
    if data['heat']['name']:
        head_bits.append(data['heat']['name'])
    if data['heat']['format'] == 'itt':
        head_bits.append(f'раздельный старт, интервал {data["heat"]["interval"]} с')
        if data['heat']['start_at']:
            head_bits.append(f'первый старт {data["heat"]["start_at"]}')
    else:
        head_bits.append('групповой старт')
    story.append(Paragraph(esc(' · '.join(head_bits)), st(9, None, TA_CENTER)))
    story.append(Paragraph(esc(f'Участников: {data["total"]} · стартовали: {data["started"]} · финишировали: {data["finished"]}'),
                           st(8.5, None, TA_CENTER)))
    story.append(Spacer(1, 4 * mm))

    widths_mm = [11, 13, 47, 11, 30, 20, 21, 19, 14]
    scale = 186.0 / sum(widths_mm)
    col_w = [w * scale * mm for w in widths_mm]
    head = ['№<br/>п/п', 'Номер', 'ФИО', 'Год', 'Группа', 'Город', 'Команда', 'Время<br/>старта', 'Прим.']
    head_style = st(8, bold, TA_CENTER, 9.5)
    cell = st(8, None, TA_LEFT, 9.5)
    cell_c = st(8, None, TA_CENTER, 9.5)
    body = [[Paragraph(h, head_style) for h in head]]
    for r in data['rows']:
        note = ''
        if r['status'] == 'dns':
            note = 'DNS'
        elif r['status'] == 'running':
            note = 'на дистанции'
        elif r['status'] == 'finished':
            note = 'финиш ' + (r['finish_display'] or '')
        body.append([
            Paragraph(str(r['position'] or '—'), cell_c),
            Paragraph(esc(r['number'] if r['number'] is not None else '—'), cell_c),
            Paragraph(esc(r['full_name']), cell),
            Paragraph(str(r['birth_year'] or ''), cell_c),
            Paragraph(esc(r['category']), cell),
            Paragraph(esc(r['city']), cell),
            Paragraph(esc(r['team']), cell),
            Paragraph(esc(r['actual_display'] or r['planned_display'] or '—'), cell_c),
            Paragraph(esc(note), cell_c),
        ])
    t = Table(body, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BACKGROUND', (0, 0), (-1, 0), colors.Color(0.94, 0.94, 0.94)),
        ('LEFTPADDING', (0, 0), (-1, -1), 2),
        ('RIGHTPADDING', (0, 0), (-1, -1), 2),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    story.append(t)
    story.append(Spacer(1, 10 * mm))
    sign = Table([[Paragraph('Главный судья ____________________', st(9)),
                   Paragraph('Секретарь ____________________', st(9))]],
                 colWidths=[93 * mm, 93 * mm])
    sign.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
                              ('LEFTPADDING', (0, 0), (-1, -1), 0)]))
    story.append(sign)
    doc.build(story)
    return buf.getvalue()


def _build_results_detail(conn, hid, cat_filter_id=None, include_auto=True):
    heat_row = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
    heat_fmt = _heat_format(heat_row)
    q = '''SELECT r.*, p.number, p.full_name, p.birth_year, p.gender, c.id AS category_id, c.name AS category_name, c.laps_total AS category_laps
           FROM races r JOIN participants p ON p.id = r.participant_id LEFT JOIN categories c ON c.id = p.category_id WHERE r.heat_id = ?'''
    params = [hid]
    if cat_filter_id is not None:
        q += ' AND p.category_id = ?'; params.append(cat_filter_id)
    q += ' ORDER BY p.number'
    rows = conn.execute(q, params).fetchall()
    laps_map = _load_laps_by_race(conn, [row['id'] for row in rows])

    grouped = {}
    for row in rows:
        prepared = prepare_race(row, conn, laps_map.get(row['id'], []), include_auto=include_auto)
        cat_id = row['category_id']; cat_name = row['category_name'] or 'Без категории'; cat_laps = row['category_laps'] or prepared['laps_total'] or 1
        key = cat_id if cat_id is not None else 0
        if key not in grouped: grouped[key] = {'id': cat_id, 'name': cat_name, 'laps_total': cat_laps, 'races': []}
        grouped[key]['races'].append(prepared)

    blocks = []
    for cat in grouped.values():
        races = cat['races']
        races.sort(key=lambda r: (1 if r['status'] == 'dnf' else (2 if r['status'] == 'dns' else 0), -len(r['laps']), r.get('sort_time', float('inf')), r['number']))
        leader = next((r for r in races if r['status'] == 'ok'), None)
        max_laps = max(cat['laps_total'], max(len(r['laps']) for r in races))
        cat['laps_total'] = max_laps

        rows_data = []
        for r in races:
            lap_cells = [{
                'cumulative': format_secs_short(l['elapsed_seconds']),
                'split': format_secs_short(l['split_seconds']),
                'source': l.get('source', 'M'),
                'user': l.get('user'),
            } for l in r['laps']]
            while len(lap_cells) < max_laps: lap_cells.append(None)

            if r['status'] == 'ok':
                total_short = format_secs_short(r['sort_time']) if r['sort_time'] != float('inf') else '—'
                total_display = f"{total_short} (+{r['laps_behind']} кр.)" if r['laps_behind'] > 0 else total_short
                if leader and r['id'] == leader['id']: gap = '—'
                elif leader and leader['sort_time'] is not None:
                    gap = f"+{r['laps_behind']} кр." if r['laps_behind'] > 0 else ('+' + format_secs_short(r['sort_time'] - leader['sort_time']) if r['sort_time'] - leader['sort_time'] > 0.0005 else '—')
                else: gap = ''
            elif r['status'] == 'lapped':
                total_display = f"+{r['laps_behind']} кр."; gap = total_display
            elif r['status'] in ('dnf', 'dns'):
                total_display = r['status'].upper(); gap = ''
            else:
                total_display = '—'; gap = ''
            rows_data.append({'race': r, 'laps': lap_cells, 'total_display': total_display, 'gap_display': gap,
                              'start_display': (r['start_time'] or '')[11:19],
                              'finish_display': ((r.get('finish_time') or '')[11:19] if r['status'] == 'ok' else ''),
                              'place': sum(1 for x in rows_data if x['race']['status'] == 'ok') + (1 if r['status'] == 'ok' else 0)})
        blocks.append({'category': cat, 'laps_total': max_laps, 'rows': rows_data, 'format': heat_fmt})
    return sorted(blocks, key=lambda x: (x['category']['name'] or '').lower())

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

# ==================== RFID и СКАНИРОВАНИЕ ====================
def _process_tag_scan(conn, tag, scan_time=None):
    if scan_time is None: scan_time = datetime.now()
    now_ts = scan_time.strftime('%H:%M:%S')
    tag = re.sub(r'\s+', '', tag or '').upper()
    if not tag:
        _log_timing(conn, 'auto', 'scan', False, reason='Пустая метка')
        return {'ok': False, 'error': 'Пустая метка'}

    p = _participant_by_tag(conn, tag)
    if not p:
        _push_auto_log({'ok': False, 'tag': tag, 'message': 'Метка не привязана к номеру', 'time': now_ts})
        _log_timing(conn, 'auto', 'scan', False, reason='Метка не привязана к номеру', detail=f'EPC {tag}')
        return {'ok': False, 'error': 'Метка не привязана к номеру'}

    def log(ok, action, reason=None, detail=None, race=None):
        _log_timing(conn, 'auto', action, ok, reason=reason,
                    competition_id=p['competition_id'],
                    heat_id=race['heat_id'] if race is not None else None,
                    race_id=race['id'] if race is not None else None,
                    participant_id=p['id'], number=p['number'], full_name=p['full_name'],
                    detail=detail)

    race = conn.execute('''
        SELECT r.*, h.auto_enabled, h.min_lap_time, h.heat_number, h.status AS heat_status
        FROM races r JOIN heats h ON h.id = r.heat_id
        WHERE r.participant_id = ? AND r.status != 'dns'
        ORDER BY r.start_time DESC LIMIT 1
    ''', (p['id'],)).fetchone()

    if not race:
        _push_auto_log({'ok': False, 'tag': tag, 'participant': p['full_name'], 'number': p['number'], 'message': 'Не в стартах', 'time': now_ts})
        log(False, 'scan', reason='Участник не в стартах', detail=f'EPC {tag}')
        return {'ok': False, 'error': f'Участник №{p["number"]} не на дистанции'}

    if race['status'] != 'running':
        _push_auto_log({'ok': False, 'tag': tag, 'participant': p['full_name'], 'number': p['number'], 'message': 'Заезд завершён', 'time': now_ts})
        log(False, 'scan', reason='Заезд уже завершён', race=race, detail=f'Старт №{race["heat_number"]}')
        return {'ok': False, 'error': 'Заезд завершён'}

    if race['heat_status'] != 'running':
        _push_auto_log({'ok': False, 'tag': tag, 'participant': p['full_name'], 'number': p['number'], 'message': 'Старт завершён', 'time': now_ts})
        log(False, 'scan', reason='Старт завершён', race=race)
        return {'ok': False, 'error': 'Старт завершён'}

    if not race['auto_enabled']:
        _push_auto_log({'ok': False, 'tag': tag, 'participant': p['full_name'], 'number': p['number'], 'message': 'Авто выключен', 'time': now_ts})
        log(False, 'scan', reason='Автохронометраж выключен', race=race, detail=f'Старт №{race["heat_number"]}')
        return {'ok': False, 'error': f'Автохронометраж выключен (старт №{race["heat_number"]})'}

    laps_done = conn.execute('SELECT COUNT(*) FROM auto_laps WHERE race_id = ?', (race['id'],)).fetchone()[0]
    if laps_done >= race['laps_total']:
        _push_auto_log({'ok': False, 'tag': tag, 'participant': p['full_name'], 'number': p['number'], 'message': 'Уже финишировал (авто)', 'time': now_ts})
        log(False, 'scan', reason='Уже финишировал', race=race)
        return {'ok': False, 'error': f'Участник №{p["number"]} уже финишировал (авто)'}

    min_lap = race['min_lap_time'] if race['min_lap_time'] is not None else 0
    last_scan = conn.execute('SELECT scan_time FROM auto_laps WHERE race_id = ? ORDER BY lap_number DESC LIMIT 1', (race['id'],)).fetchone()
    last_dt = datetime.fromisoformat(last_scan['scan_time']) if last_scan else datetime.fromisoformat(race['start_time'])
    elapsed = (scan_time - last_dt).total_seconds()

    if elapsed < min_lap:
        msg = f'Рано ({elapsed:.1f} < {min_lap} с)'
        _push_auto_log({'ok': False, 'tag': tag, 'participant': p['full_name'], 'number': p['number'], 'message': msg, 'time': now_ts})
        log(False, 'scan', reason='Слишком рано', race=race, detail=f'{elapsed:.1f} с < {min_lap} с')
        return {'ok': False, 'error': msg}

    lap_num = laps_done + 1
    is_finish = lap_num == race['laps_total']

    conn.execute('INSERT INTO auto_laps (race_id, lap_number, scan_time, rfid_tag) VALUES (?, ?, ?, ?)', (race['id'], lap_num, scan_time.isoformat(), tag))

    if is_finish:
        _process_race_finish(conn, race, scan_time.isoformat(), force=False)

    msg = 'ФИНИШ (авто) 🏁' if is_finish else f'Круг {lap_num}/{race["laps_total"]} (авто)'
    _push_auto_log({'ok': True, 'tag': tag, 'participant': p['full_name'], 'number': p['number'], 'message': msg, 'time': now_ts})
    log(True, 'finish' if is_finish else 'lap', race=race,
        detail=f'Круг {lap_num}/{race["laps_total"]} · EPC {tag}')

    return {
        'ok': True, 'number': p['number'], 'full_name': p['full_name'],
        'lap': lap_num, 'laps_total': race['laps_total'], 'is_finish': is_finish,
        'heat_number': race['heat_number'],
        'message': msg
    }

def _swrfid_listener():
    while not _swrfid_stop_event.is_set():
        try:
            with _settings_lock:
                enabled = _settings_cache.get('swrfid_enabled', '0') == '1'
                port = _settings_cache.get('swrfid_port', 'COM3')

            if not enabled:
                with _swrfid_status_lock:
                    _swrfid_status['connected'] = False
                    _swrfid_status['last_error'] = None
                _swrfid_stop_event.wait(2)
                continue

            if not SWRFID_AVAILABLE:
                with _swrfid_status_lock:
                    _swrfid_status['connected'] = False
                    _swrfid_status['last_error'] = 'Библиотека swrfid не установлена'
                _swrfid_stop_event.wait(5)
                continue

            reader = None
            try:
                reader = RFIDReader(port)
                reader.open()
                with _swrfid_status_lock:
                    _swrfid_status['connected'] = True
                    _swrfid_status['last_error'] = None
                    _swrfid_status['port'] = port
                _swrfid_logger.info('SWRFID connected on %s', port)
            except Exception as e:
                with _swrfid_status_lock:
                    _swrfid_status['connected'] = False
                    _swrfid_status['last_error'] = str(e)
                _swrfid_logger.error('SWRFID connect error: %s', e)
                if reader:
                    try: reader.close()
                    except: pass
                _swrfid_stop_event.wait(5)
                continue

            def on_tags(dev_sn, tags):
                if _swrfid_stop_event.is_set(): return
                conn = get_db()
                try:
                    for tag in tags:
                        epc = tag.epc_hex.upper()
                        with conn: _process_tag_scan(conn, epc, datetime.now())
                except Exception as e: _swrfid_logger.error('SWRFID DB error: %s', e)
                finally: conn.close()

            try:
                reader.start_active_listener(on_tags, dedup_window=2.0, rssi_min=0x30)
                reader.start_read()
            except Exception as e:
                with _swrfid_status_lock: _swrfid_status['connected'] = False; _swrfid_status['last_error'] = str(e)
                _swrfid_logger.error('SWRFID start_read error: %s', e)
                try: reader.close()
                except: pass
                _swrfid_stop_event.wait(5)
                continue

            while not _swrfid_stop_event.is_set():
                with _settings_lock:
                    if _settings_cache.get('swrfid_enabled', '0') != '1':
                        break
                time.sleep(0.5)

            try:
                reader.stop_read()
                reader.close()
            except: pass

            with _swrfid_status_lock:
                _swrfid_status['connected'] = False
                _swrfid_status['last_error'] = None

        except Exception as e:
            with _swrfid_status_lock: _swrfid_status['connected'] = False; _swrfid_status['last_error'] = str(e)
            _swrfid_stop_event.wait(5)

    with _swrfid_status_lock: _swrfid_status['connected'] = False

def start_swrfid_listener():
    global _swrfid_thread
    if _swrfid_thread and _swrfid_thread.is_alive(): return
    if not _settings_cache:
        conn = get_db()
        try: _reload_settings_cache_from_db(conn)
        finally: conn.close()
    _swrfid_stop_event.clear()
    _swrfid_thread = threading.Thread(target=_swrfid_listener, daemon=True); _swrfid_thread.start()


# ======================= УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ =======================
def _require_admin():
    if session.get('role') != 'admin':
        flash('Доступ только для администратора', 'error')
        return False
    return True

@app.route('/users')
def users_page():
    if not _require_admin(): return redirect(url_for('competitions_list'))
    conn = get_db()
    try:
        users = conn.execute('SELECT id, username, is_admin, role, created_at FROM users ORDER BY username').fetchall()
        return render_template('users.html', users=users)
    finally:
        conn.close()

@app.route('/users/add', methods=['POST'])
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

@app.route('/users/<int:uid>/password', methods=['POST'])
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

@app.route('/users/<int:uid>/role', methods=['POST'])
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

@app.route('/users/<int:uid>/delete', methods=['POST'])
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


# ======================= СПИСОК РАНЕЕ РЕГИСТРИРОВАВШИХСЯ =======================
# Общий для всех соревнований кэш участников с bike.cedor.ru: подсказки при вводе ФИО.

PEOPLE_DB = os.path.join(DATA_DIR, 'people.db')
PEOPLE_URL = os.environ.get('CHRONO_PEOPLE_URL', 'https://bike.cedor.ru/participants.csv')
PEOPLE_TTL_HOURS = 24


def _people_db():
    conn = _connect(PEOPLE_DB)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            name_key TEXT NOT NULL,
            birth_year INTEGER,
            city TEXT,
            team TEXT,
            gender TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_people_key ON people(name_key);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    return conn


def _norm_person(s):
    return re.sub(r'\s+', ' ', (s or '').strip().lower().replace('ё', 'е'))


def _parse_people_csv(text):
    """Разбирает CSV вида «ФИО;Год рождения;Город;Клуб»."""
    import csv as _csv
    first_line = text.split('\n', 1)[0]
    delimiter = ';' if first_line.count(';') >= first_line.count(',') else ','
    rows = list(_csv.reader(io.StringIO(text), delimiter=delimiter))
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]

    def find(*names):
        for i, h in enumerate(header):
            for n in names:
                if n in h:
                    return i
        return None

    i_fio = find('фио', 'фамилия', 'name')
    i_year = find('год рождения', 'год')
    i_city = find('город', 'city')
    i_team = find('клуб', 'команд', 'team', 'club')
    i_gender = find('пол', 'gender', 'sex')
    if i_fio is None:
        i_fio, i_year, i_city, i_team = 0, 1, 2, 3

    out = []
    for r in rows[1:]:
        if not r or len(r) <= i_fio:
            continue
        fio = (r[i_fio] or '').strip()
        if len(fio) < 3:
            continue
        year = _parse_year(r[i_year]) if i_year is not None and len(r) > i_year else None
        city = (r[i_city] or '').strip() if i_city is not None and len(r) > i_city else None
        team = (r[i_team] or '').strip() if i_team is not None and len(r) > i_team else None
        gender = _normalize_gender(r[i_gender]) if i_gender is not None and len(r) > i_gender else None
        # «Лично» в колонке города — это не город, а отсутствие клуба
        if city and city.strip().lower() == 'лично':
            city = None
        # в общем списке клуб всегда заполнен: пусто → «Лично»
        out.append((fio, _norm_person(fio), year, city or None, team or 'Лично', gender))
    return out


def _people_refresh():
    """Скачивает CSV и полностью перезаливает список. Возвращает (count, error)."""
    import urllib.request
    try:
        req = urllib.request.Request(PEOPLE_URL, headers={'User-Agent': 'chrono-timer/1.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except Exception as ex:
        return None, f'Не удалось скачать список: {ex}'

    text = None
    for enc in ('utf-8-sig', 'utf-8', 'cp1251'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return None, 'Не удалось определить кодировку файла'

    rows = _parse_people_csv(text)
    if not rows:
        return None, 'В файле не найдено ни одного участника'

    conn = _people_db()
    try:
        with conn:
            conn.execute('DELETE FROM people')
            conn.executemany(
                'INSERT INTO people (full_name, name_key, birth_year, city, team, gender) '
                'VALUES (?, ?, ?, ?, ?, ?)', rows)
            now = datetime.now().isoformat(timespec='seconds')
            conn.execute("INSERT INTO meta (key, value) VALUES ('updated_at', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (now,))
            conn.execute("INSERT INTO meta (key, value) VALUES ('count', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(len(rows)),))
    finally:
        conn.close()
    _app_logger.info('People list refreshed: %s records', len(rows))
    return len(rows), None


def _people_stats():
    try:
        conn = _people_db()
    except Exception:
        return 0, None
    try:
        meta = {r['key']: r['value'] for r in conn.execute('SELECT key, value FROM meta')}
        count = conn.execute('SELECT COUNT(*) FROM people').fetchone()[0]
        return count, meta.get('updated_at')
    finally:
        conn.close()


def _people_search(query, limit=20):
    q = _norm_person(query)
    if len(q) < 2:
        return []
    words = q.split(' ')[:4]
    sql = 'SELECT id, full_name, birth_year, city, team, gender FROM people WHERE 1=1'
    params = []
    for w in words:
        sql += ' AND (name_key LIKE ? OR name_key LIKE ?)'
        params += [w + '%', '% ' + w + '%']
    sql += ' ORDER BY full_name COLLATE NOCASE LIMIT ?'
    params.append(limit)
    conn = _people_db()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _people_auto_refresh():
    """В фоне обновляет список, если кэш старше суток (ошибки не влияют на работу)."""
    try:
        count, updated = _people_stats()
        if count and updated:
            age = datetime.now() - datetime.fromisoformat(updated)
            if age < timedelta(hours=PEOPLE_TTL_HOURS):
                return
    except Exception:
        pass

    def _run():
        try:
            _people_refresh()
        except Exception:
            _app_logger.exception('People auto refresh failed')

    threading.Thread(target=_run, daemon=True).start()


@app.route('/api/people')
def api_people():
    """Подсказки по ранее зарегистрировавшимся (публичный список)."""
    return jsonify({'items': _people_search(request.args.get('q', ''))})


# ======================= САМОРЕГИСТРАЦИЯ УЧАСТНИКОВ =======================
# Публичная форма для участников. Настройки хранятся в базе соревнования.

REG_FIELDS = [
    ('fio', 'ФИО'),
    ('gender', 'Пол'),
    ('city', 'Город'),
    ('team', 'Клуб / команда'),
    ('year', 'Год рождения'),
    ('category', 'Категория'),
    ('phone', 'Контактный номер'),
]

DEFAULT_CONSENT = (
    'Я даю согласие РОО «Белгородская Региональная Федерация велосипедного спорта» '
    'на обработку своих персональных данных, указанных мной в этой форме, '
    'в соответствии с требованиями ст. 9 Федерального закона от 27.07.2006 г. '
    '«О персональных данных» №152-ФЗ.'
)

_reg_attempts = {}          # ip -> [timestamps] — простая защита от спама
_reg_attempts_lock = threading.Lock()
REG_RATE_LIMIT = 5          # не больше 5 отправок
REG_RATE_WINDOW = 300       # за 5 минут


def _reg_config(conn):
    """Настройки саморегистрации из таблицы settings этой базы."""
    raw = {}
    for r in conn.execute('SELECT key, value FROM settings'):
        if r['key'].startswith('reg_'):
            raw[r['key']] = r['value']
    try:
        saved = json.loads(raw.get('reg_fields') or '{}')
    except ValueError:
        saved = {}
    fields = {}
    for key, label in REG_FIELDS:
        # Старые настройки (до появления поля «Пол») — включаем его по умолчанию:
        # без пола категории с ограничением по полу не подбираются.
        if key == 'gender' and 'gender' not in saved:
            fields[key] = {'label': label, 'on': True, 'req': True}
            continue
        f = saved.get(key) or {}
        fields[key] = {
            'label': label,
            'on': True if key == 'fio' else bool(f.get('on')),
            'req': True if key == 'fio' else bool(f.get('req')),
        }
    return {
        'enabled': raw.get('reg_enabled') == '1',
        'info': raw.get('reg_info') or '',
        'deadline': raw.get('reg_deadline') or '',
        'consent': raw.get('reg_consent') or DEFAULT_CONSENT,
        'fields': fields,
    }


def _reg_status(cfg):
    """(открыта ли регистрация, причина отказа)."""
    if not cfg['enabled']:
        return False, 'Регистрация на это соревнование закрыта.'
    deadline = (cfg['deadline'] or '').strip()
    if deadline:
        try:
            end = datetime.fromisoformat(deadline)
        except ValueError:
            end = None
        if end is not None:
            if len(deadline) == 10:
                end = end.replace(hour=23, minute=59, second=59)
            if datetime.now() > end:
                return False, f'Регистрация завершена {deadline}.'
    return True, ''


def _reg_rate_ok(ip):
    now = time.time()
    with _reg_attempts_lock:
        marks = [t for t in _reg_attempts.get(ip, []) if now - t < REG_RATE_WINDOW]
        if len(marks) >= REG_RATE_LIMIT:
            _reg_attempts[ip] = marks
            return False
        marks.append(now)
        _reg_attempts[ip] = marks
        return True


def _public_reg_context(slug):
    """(entry, conn, competition) для публичной страницы регистрации."""
    entry = _registry_find(slug)
    if not entry:
        return None, None, None
    conn = _connect(_comp_db_path(entry))
    return entry, conn, _get_competition(conn)


@app.route('/registration')
def registration_page():
    """Настройки публичной регистрации (для администратора соревнования)."""
    conn = get_db()
    try:
        comp = _get_competition(conn)
        cfg = _reg_config(conn)
        total = conn.execute(
            "SELECT COUNT(*) FROM registrations WHERE competition_id = ?",
            (comp['id'],)).fetchone()[0]
        entries = conn.execute(
            "SELECT r.*, p.number AS participant_number, p.id AS alive_id "
            "FROM registrations r LEFT JOIN participants p ON p.id = r.participant_id "
            "WHERE r.competition_id = ? ORDER BY r.id DESC LIMIT 100", (comp['id'],)).fetchall()
        slug = session.get('comp')
        public_url = url_for('reg_intro', slug=slug, _external=True) if slug else ''
        list_url = url_for('reg_list', slug=slug, _external=True) if slug else ''
        qr = _qr_svg(public_url) if public_url else None
        cats = conn.execute('SELECT * FROM categories WHERE competition_id = ?', (comp['id'],)).fetchall()
        need_gender = any(_cat_genders(c['gender']) != {'M', 'F'} for c in cats)
        need_year = any(c['year_from'] is not None or c['year_to'] is not None for c in cats)
        uncategorized = conn.execute(
            "SELECT COUNT(*) FROM participants WHERE competition_id = ? "
            "AND reg_source = 'public' AND category_id IS NULL", (comp['id'],)).fetchone()[0]
        return render_template('registration.html', competition=comp, cfg=cfg, entries=entries,
                               total=total, public_url=public_url, list_url=list_url, qr=qr,
                               fields=REG_FIELDS,
                               warn_gender=need_gender and not cfg['fields']['gender']['on'],
                               warn_year=need_year and not cfg['fields']['year']['on'],
                               uncategorized=uncategorized)
    finally:
        conn.close()


@app.route('/registrations')
def registrations_page():
    """Журнал саморегистраций: все заявки, включая удалённых участников."""
    conn = get_db()
    try:
        comp = _get_competition(conn)
        rows = conn.execute(
            'SELECT r.*, p.number AS participant_number, p.id AS alive_id, '
            '       p.category_id AS alive_category '
            'FROM registrations r '
            'LEFT JOIN participants p ON p.id = r.participant_id '
            'WHERE r.competition_id = ? ORDER BY r.id DESC',
            (comp['id'],)).fetchall()
        total = len(rows)
        alive = sum(1 for r in rows if r['alive_id'])
        return render_template('registrations.html', competition=comp, rows=rows,
                               total=total, alive=alive, deleted=total - alive)
    finally:
        conn.close()


@app.route('/registrations.xlsx')
def registrations_xlsx():
    """Выгрузка журнала саморегистраций в Excel."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    conn = get_db()
    try:
        comp = _get_competition(conn)
        rows = conn.execute(
            'SELECT r.*, p.number AS participant_number, p.id AS alive_id '
            'FROM registrations r LEFT JOIN participants p ON p.id = r.participant_id '
            'WHERE r.competition_id = ? ORDER BY r.id DESC', (comp['id'],)).fetchall()
    finally:
        conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = 'Саморегистрации'
    header = ['ID заявки', 'Дата', 'ФИО', 'Год', 'Пол', 'Город', 'Клуб', 'Телефон',
              'Категория', 'Статус', 'Номер', 'IP', 'Браузер', 'ОС', 'Устройство', 'Язык', 'Согласие']
    ws.append(header)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for r in rows:
        ws.append([
            r['id'], (r['created_at'] or '').replace('T', ' ')[:16], r['full_name'],
            r['birth_year'], 'М' if r['gender'] == 'M' else ('Ж' if r['gender'] == 'F' else ''),
            r['city'], r['team'], r['phone'], r['category_name'],
            'в списке' if r['alive_id'] else 'удалён',
            r['participant_number'], r['ip'], r['browser'], r['os'], r['device'],
            r['accept_language'], r['consent_at'],
        ])
    widths = [11, 17, 28, 7, 5, 16, 20, 16, 20, 10, 8, 16, 16, 12, 12, 10, 18]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"samoregistracii-{comp['id']}.xlsx"
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/registration/save', methods=['POST'])
def registration_save():
    conn = get_db()
    try:
        _get_competition(conn)
        fields = {}
        for key, _label in REG_FIELDS:
            fields[key] = {
                'on': request.form.get(f'on_{key}') == '1',
                'req': request.form.get(f'req_{key}') == '1',
            }
        fields['fio'] = {'on': True, 'req': True}   # без ФИО участника не создать
        deadline = (request.form.get('deadline') or '').strip()
        consent = (request.form.get('consent') or '').strip() or DEFAULT_CONSENT
        with conn:
            set_cached_setting(conn, 'reg_enabled', '1' if request.form.get('enabled') else '0')
            set_cached_setting(conn, 'reg_info', request.form.get('info') or '')
            set_cached_setting(conn, 'reg_deadline', deadline)
            set_cached_setting(conn, 'reg_consent', consent)
            set_cached_setting(conn, 'reg_fields', json.dumps(fields, ensure_ascii=False))
        flash('Настройки регистрации сохранены', 'success')
    finally:
        conn.close()
    return redirect(url_for('registration_page'))


@app.route('/registration/autocat', methods=['POST'])
def registration_autocat():
    """Подбирает категории уже зарегистрировавшимся через форму (у кого пусто)."""
    conn = get_db()
    try:
        comp = _get_competition(conn)
        rows = conn.execute(
            "SELECT id, gender, birth_year FROM participants WHERE competition_id = ? "
            "AND reg_source = 'public' AND category_id IS NULL", (comp['id'],)).fetchall()
        strict = loose = 0
        with conn:
            for r in rows:
                found = _auto_category(conn, comp['id'], r['gender'], r['birth_year'])
                if found:
                    conn.execute('UPDATE participants SET category_id = ? WHERE id = ?', (found, r['id']))
                    strict += 1
                    continue
                # вторая попытка: пол неизвестен — подбираем только по году
                if r['gender'] is None and r['birth_year'] is not None:
                    for cat in conn.execute('SELECT * FROM categories WHERE competition_id = ? ORDER BY id',
                                            (comp['id'],)).fetchall():
                        yf, yt = cat['year_from'], cat['year_to']
                        if yf is None and yt is None:
                            continue
                        if (yf is None or r['birth_year'] >= yf) and (yt is None or r['birth_year'] <= yt):
                            conn.execute('UPDATE participants SET category_id = ? WHERE id = ?', (cat['id'], r['id']))
                            loose += 1
                            break
        left = conn.execute(
            "SELECT COUNT(*) FROM participants WHERE competition_id = ? "
            "AND reg_source = 'public' AND category_id IS NULL", (comp['id'],)).fetchone()[0]
        msg = f'Категории подобраны: {strict + loose}'
        if loose:
            msg += f' (по году, без пола: {loose} — проверьте)'
        if left:
            msg += f'. Без категории осталось: {left}'
        flash(msg, 'success')
    finally:
        conn.close()
    return redirect(url_for('registration_page'))


def _public_participants(conn, cid):
    """Список участников для публичной страницы: без телефона и прочих контактов."""
    return conn.execute(
        'SELECT p.number, p.full_name, p.birth_year, p.gender, p.city, p.team, '
        '       c.name AS category_name '
        'FROM participants p LEFT JOIN categories c ON c.id = p.category_id '
        'WHERE p.competition_id = ? '
        'ORDER BY COALESCE(p.number, 0), p.full_name', (cid,)).fetchall()


@app.route('/reg/<slug>/list')
def reg_list(slug):
    """Публичный список зарегистрированных участников (без телефонов)."""
    entry, conn, comp = _public_reg_context(slug)
    if not entry:
        return _reg_closed(slug, 'Ссылка недействительна: соревнование не найдено.', 404)
    try:
        rows = _public_participants(conn, comp['id'])
        return render_template('public_reg_list.html', comp=comp, rows=rows, slug=slug,
                               total=len(rows))
    finally:
        conn.close()


@app.route('/reg/<slug>/list.xlsx')
def reg_list_xlsx(slug):
    """Экспорт публичного списка участников в Excel."""
    entry, conn, comp = _public_reg_context(slug)
    if not entry:
        return _reg_closed(slug, 'Ссылка недействительна: соревнование не найдено.', 404)
    try:
        rows = _public_participants(conn, comp['id'])
        wb = Workbook()
        ws = wb.active
        ws.title = 'Участники'
        headers = ['№', 'ФИО', 'Год рождения', 'Пол', 'Город', 'Клуб', 'Категория']
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for r in rows:
            ws.append([
                r['number'], r['full_name'], r['birth_year'],
                'М' if r['gender'] == 'M' else ('Ж' if r['gender'] == 'F' else ''),
                r['city'] or '', r['team'] or '', r['category_name'] or '',
            ])
        widths = [6, 34, 14, 6, 18, 26, 24]
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = 'A2'
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        name = f"Участники — {comp['name']}.xlsx"
        return send_file(buf, as_attachment=True, download_name=name,
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    finally:
        conn.close()


def _parse_ua(ua):
    """Грубый разбор User-Agent: браузер, ОС, тип устройства."""
    u = (ua or '')
    low = u.lower()
    browser = 'неизвестно'
    for marker, name in (
            ('yabrowser', 'Yandex Browser'), ('opr/', 'Opera'), ('opera', 'Opera'),
            ('edg/', 'Edge'), ('samsungbrowser', 'Samsung Internet'),
            ('ucbrowser', 'UC Browser'), ('miuibrowser', 'MIUI Browser'),
            ('huaweibrowser', 'Huawei Browser'), ('firefox', 'Firefox'),
            ('chrome', 'Chrome'), ('safari', 'Safari')):
        if marker in low:
            browser = name
            break
    os_name = 'неизвестно'
    for marker, name in (('windows', 'Windows'), ('android', 'Android'), ('iphone', 'iOS'),
                         ('ipad', 'iPadOS'), ('mac os', 'macOS'), ('linux', 'Linux')):
        if marker in low:
            os_name = name
            break
    device = 'Компьютер'
    if 'ipad' in low or 'tablet' in low:
        device = 'Планшет'
    elif 'mobile' in low or 'android' in low or 'iphone' in low:
        device = 'Телефон'
    return browser, os_name, device


def _save_registration(conn, comp, values, participant_id, category_name):
    """Пишет заявку в журнал саморегистраций (сохраняется даже после удаления участника)."""
    ua = request.headers.get('User-Agent', '') or ''
    browser, os_name, device = _parse_ua(ua)
    raw = dict(values)
    raw['category_name'] = category_name
    with conn:
        conn.execute(
            'INSERT INTO registrations (competition_id, participant_id, full_name, birth_year, gender, '
            'city, team, phone, category_id, category_name, consent_at, created_at, ip, user_agent, '
            'browser, os, device, accept_language, referer, raw_json) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (comp['id'], participant_id,
             (values.get('fio') or '').strip() or None,
             _parse_year(values.get('year')) if values.get('year') else None,
             (values.get('gender') or '').upper() or None,
             (values.get('city') or '').strip() or None,
             (values.get('team') or '').strip() or None,
             (values.get('phone') or '').strip() or None,
             int(values['category']) if str(values.get('category') or '').isdigit() else None,
             category_name or None,
             datetime.now().isoformat(timespec='seconds'),
             datetime.now().isoformat(timespec='seconds'),
             request.headers.get('X-Forwarded-For', request.remote_addr or '')[:120],
             ua[:400],
             browser, os_name, device,
             (request.headers.get('Accept-Language') or '')[:120],
             (request.headers.get('Referer') or '')[:300],
             json.dumps(raw, ensure_ascii=False)))


def _reg_closed(slug, message, status=403):
    return render_template('public_reg_closed.html', message=message, slug=slug), status


@app.route('/reg/<slug>')
def reg_intro(slug):
    """Публичная страница: информация о соревновании и кнопка согласия."""
    entry, conn, comp = _public_reg_context(slug)
    if not entry:
        return _reg_closed(slug, 'Ссылка недействительна: соревнование не найдено.', 404)
    try:
        cfg = _reg_config(conn)
        ok, why = _reg_status(cfg)
        if ok and _is_read_only(slug):
            ok, why = False, 'Регистрация недоступна: соревнование в режиме «только чтение».'
        return render_template('public_reg.html', comp=comp, cfg=cfg, ok=ok, why=why, slug=slug)
    finally:
        conn.close()


REG_WIZARD_ORDER = ['fio', 'gender', 'year', 'city', 'team', 'category', 'phone']


def _reg_steps(cfg):
    """Шаги мастера: включённые поля в удобном порядке + согласие."""
    return [k for k in REG_WIZARD_ORDER if cfg['fields'][k]['on']] + ['consent']


def _reg_draft(slug):
    drafts = session.get('reg_draft') or {}
    return dict(drafts.get(slug) or {})


def _reg_save_draft(slug, values):
    drafts = dict(session.get('reg_draft') or {})
    drafts[slug] = values
    session['reg_draft'] = drafts


def _reg_clear_draft(slug):
    drafts = dict(session.get('reg_draft') or {})
    drafts.pop(slug, None)
    session['reg_draft'] = drafts


def _reg_step_validate(conn, comp, cfg, key, raw, draft):
    """Проверяет одно поле шага. Возвращает (значение, ошибка)."""
    f = cfg['fields'][key]
    raw = (raw or '').strip()

    if key == 'fio':
        if len(raw) < 3:
            return None, 'Укажите фамилию и имя'
        return raw, None

    if key == 'gender':
        value = raw.upper()
        if value in ('M', 'F'):
            return value, None
        return (None, 'Укажите пол') if f['req'] else ('', None)

    if key == 'year':
        if not raw:
            return (None, 'Укажите год рождения') if f['req'] else ('', None)
        year = _parse_year(raw)
        if year is None:
            return None, 'Год рождения указан неверно — например, 2012'
        return str(year), None

    if key == 'city':
        if not raw and f['req']:
            return None, 'Укажите город'
        return raw, None

    if key == 'team':
        if not raw and f['req']:
            return None, 'Укажите клуб или команду'
        return raw, None

    if key == 'phone':
        if not raw:
            return (None, 'Укажите контактный номер') if f['req'] else ('', None)
        if not re.fullmatch(r'[0-9+\-() ]{5,25}', raw):
            return None, 'Контактный номер указан неверно'
        return raw, None

    if key == 'category':
        if not raw:
            return (None, 'Выберите категорию') if f['req'] else ('', None)
        try:
            cat_id = int(raw)
        except ValueError:
            return None, 'Выберите категорию из списка'
        row = conn.execute('SELECT * FROM categories WHERE id = ? AND competition_id = ?',
                           (cat_id, comp['id'])).fetchone()
        if not row:
            return None, 'Выберите категорию из списка'
        gender = (draft.get('gender') or '').upper() or None
        year = _parse_year(draft.get('year')) if draft.get('year') else None
        if not _category_matches(row, gender, year):
            return None, 'Эта категория не подходит по полу или году рождения'
        return str(cat_id), None

    return raw, None


def _reg_allowed_categories(conn, comp, draft):
    """Категории, подходящие под уже введённые пол и год."""
    gender = (draft.get('gender') or '').upper() or None
    year = _parse_year(draft.get('year')) if draft.get('year') else None
    rows = conn.execute('SELECT * FROM categories WHERE competition_id = ? ORDER BY id',
                        (comp['id'],)).fetchall()
    return [c for c in rows if _category_matches(c, gender, year)]


def _reg_create_participant(conn, comp, cfg, values):
    """Создаёт участника из данных мастера. Возвращает (number, category_name, errors)."""
    errors = []
    f = cfg['fields']

    fio = (values.get('fio') or '').strip()
    if f['fio']['on'] and len(fio) < 3:
        errors.append('Укажите фамилию и имя')

    gender = None
    if f['gender']['on']:
        g = (values.get('gender') or '').upper()
        if g in ('M', 'F'):
            gender = g
        elif f['gender']['req']:
            errors.append('Укажите пол')

    year_val = None
    if f['year']['on']:
        raw_year = (values.get('year') or '').strip()
        year_val = _parse_year(raw_year) if raw_year else None
        if f['year']['req'] and not raw_year:
            errors.append('Укажите год рождения')
        elif raw_year and year_val is None:
            errors.append('Год рождения указан неверно')

    city = (values.get('city') or '').strip()
    if f['city']['on'] and f['city']['req'] and not city:
        errors.append('Укажите город')

    team = (values.get('team') or '').strip()
    if f['team']['on'] and f['team']['req'] and not team:
        errors.append('Укажите клуб или команду')

    phone = (values.get('phone') or '').strip()
    if f['phone']['on']:
        if f['phone']['req'] and not phone:
            errors.append('Укажите контактный номер')
        elif phone and not re.fullmatch(r'[0-9+\-() ]{5,25}', phone):
            errors.append('Контактный номер указан неверно')

    category_id = None
    raw_cat = (values.get('category') or '').strip()
    if f['category']['on']:
        if raw_cat:
            try:
                cat_id = int(raw_cat)
            except ValueError:
                cat_id = None
            row = conn.execute('SELECT * FROM categories WHERE id = ? AND competition_id = ?',
                               (cat_id, comp['id'])).fetchone() if cat_id else None
            if not row:
                errors.append('Выберите категорию из списка')
            elif not _category_matches(row, gender, year_val):
                errors.append('Эта категория не подходит по полу или году рождения')
            else:
                category_id = cat_id
        elif f['category']['req']:
            errors.append('Выберите категорию')
    if category_id is None:
        category_id = _auto_category(conn, comp['id'], gender, year_val)

    if not errors:
        def _norm(s):
            return re.sub(r'\s+', ' ', (s or '').strip().lower().replace('ё', 'е'))

        for cand in conn.execute(
                'SELECT id, number, full_name FROM participants WHERE competition_id = ? '
                'AND COALESCE(birth_year, 0) = COALESCE(?, 0)',
                (comp['id'], year_val)).fetchall():
            if _norm(cand['full_name']) == _norm(fio):
                who = f'№{cand["number"]}' if cand['number'] else f'ID {cand["id"]}'
                errors.append(f'Участник с такими данными уже зарегистрирован ({who})')
                break

    if errors:
        return None, '', errors

    with conn:
        cur = conn.execute(
            'INSERT INTO participants (competition_id, number, full_name, birth_year, category_id, '
            'gender, city, team, phone, consent_at, reg_source) '
            'VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (comp['id'], fio, year_val, category_id, gender,
             city or None, team or None, phone or None,
             datetime.now().isoformat(timespec='seconds'), 'public'))
        pid = cur.lastrowid

    cat_name = ''
    if category_id:
        row = conn.execute('SELECT name FROM categories WHERE id = ?', (category_id,)).fetchone()
        cat_name = row['name'] if row else ''
    _save_registration(conn, comp, values, pid, cat_name)
    return pid, cat_name, []


@app.route('/reg/<slug>/form', methods=['GET', 'POST'])
def reg_form(slug):
    """Старая ссылка на форму — начинаем мастер с первого шага."""
    _reg_clear_draft(slug)
    return redirect(url_for('reg_step', slug=slug, step=1))


@app.route('/reg/<slug>/step/<int:step>', methods=['GET', 'POST'])
def reg_step(slug, step):
    """Пошаговая регистрация: одно поле на странице, можно вернуться назад."""
    entry, conn, comp = _public_reg_context(slug)
    if not entry:
        return _reg_closed(slug, 'Ссылка недействительна: соревнование не найдено.', 404)
    try:
        cfg = _reg_config(conn)
        ok, why = _reg_status(cfg)
        if ok and _is_read_only(slug):
            ok, why = False, 'Регистрация недоступна: соревнование в режиме «только чтение».'
        if not ok:
            return _reg_closed(slug, why)

        steps = _reg_steps(cfg)
        total = len(steps)
        step = max(1, min(step, total))
        key = steps[step - 1]
        draft = _reg_draft(slug)
        errors = []

        if request.method == 'POST':
            if key == 'consent':
                if not request.form.get('consent'):
                    errors.append('Нужно дать согласие на обработку персональных данных')
                if not errors:
                    if not _reg_rate_ok(request.remote_addr or '?'):
                        return _reg_closed(slug, 'Слишком много попыток. Попробуйте через несколько минут.', 429)
                    number, cat_name, errs = _reg_create_participant(conn, comp, cfg, draft)
                    if errs:
                        errors.extend(errs)
                    else:
                        _reg_clear_draft(slug)
                        _app_logger.info('Public registration: comp=%s id=%s name=%s cat=%s',
                                         slug, number, draft.get('fio'), cat_name)
                        return redirect(url_for('reg_done', slug=slug, n=number, cat=cat_name))
            else:
                value, error = _reg_step_validate(conn, comp, cfg, key, request.form.get(key), draft)
                if error:
                    errors.append(error)
                else:
                    draft[key] = value
                    # подстановка из общего списка: пол/год/город/клуб приходят скрытыми полями
                    for extra in ('gender', 'year', 'city', 'team'):
                        if extra == key or not cfg['fields'][extra]['on']:
                            continue
                        if extra not in request.form:
                            continue
                        extra_val, extra_err = _reg_step_validate(
                            conn, comp, cfg, extra, request.form.get(extra), draft)
                        if not extra_err and extra_val:
                            draft[extra] = extra_val
                    _reg_save_draft(slug, draft)
                    # идём к первому незаполненному шагу (подставленные данные не переспрашиваем)
                    target = len(steps)
                    for n in range(step + 1, len(steps) + 1):
                        k = steps[n - 1]
                        if k == 'consent' or not str(draft.get(k) or '').strip():
                            target = n
                            break
                    return redirect(url_for('reg_step', slug=slug, step=target))
            if key == 'consent':
                draft = _reg_draft(slug)

        options = []
        if key == 'category':
            options = _reg_allowed_categories(conn, comp, draft)

        labels = {
            'fio': ('ФИО', 'Как в документах: фамилия, имя и отчество'),
            'gender': ('Пол', 'Нужен для распределения по категориям'),
            'year': ('Год рождения', 'Например, 2012'),
            'city': ('Город', 'Где вы живёте'),
            'team': ('Клуб / команда', 'Если без клуба — напишите «Лично»'),
            'category': ('Категория', 'Показываем только подходящие вам'),
            'phone': ('Контактный номер', 'Для связи организаторов с вами'),
            'consent': ('Согласие', 'Остался последний шаг'),
        }
        field = None
        if key != 'consent':
            field = {
                'key': key,
                'label': labels[key][0],
                'hint': labels[key][1],
                'required': cfg['fields'][key]['req'],
                'value': draft.get(key, ''),
            }

        summary = []
        if key == 'consent':
            for k in REG_WIZARD_ORDER:
                if not cfg['fields'][k]['on']:
                    continue
                v = (draft.get(k) or '').strip()
                if k == 'gender':
                    v = 'Мужской' if v == 'M' else ('Женский' if v == 'F' else '')
                elif k == 'category' and v:
                    row = conn.execute('SELECT name FROM categories WHERE id = ?', (int(v),)).fetchone()
                    v = row['name'] if row else v
                if v:
                    summary.append((labels[k][0], v))

        return render_template('public_reg_step.html', comp=comp, cfg=cfg, slug=slug,
                               step=step, total=total, key=key, field=field,
                               options=options, errors=errors, draft=draft,
                               summary=summary, consent_text=cfg['consent'])
    finally:
        conn.close()


@app.route('/reg/<slug>/done')
def reg_done(slug):
    entry = _registry_find(slug)
    if not entry:
        return _reg_closed(slug, 'Ссылка недействительна: соревнование не найдено.', 404)
    number = request.args.get('n', type=int)
    cat = (request.args.get('cat') or '').strip()
    return render_template('public_reg_done.html', slug=slug, pid=number,
                           name=entry.get('name'), category=cat)


# ======================= МАРШРУТЫ =======================
@app.route('/competitions')
def competitions_list():
    """Настройки единственного соревнования."""
    conn = get_db()
    try:
        comp = _get_competition(conn)
        cid = comp['id']
        stats = {
            'categories': conn.execute('SELECT COUNT(*) FROM categories WHERE competition_id = ?', (cid,)).fetchone()[0],
            'participants': conn.execute('SELECT COUNT(*) FROM participants WHERE competition_id = ?', (cid,)).fetchone()[0],
            'tagged': conn.execute(
                "SELECT COUNT(*) FROM participants p WHERE p.competition_id = ? AND p.number IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM bib_tags bt WHERE bt.competition_id = p.competition_id "
                "            AND bt.number = p.number)", (cid,)).fetchone()[0],
            'heats': conn.execute('SELECT COUNT(*) FROM heats WHERE competition_id = ?', (cid,)).fetchone()[0],
        }
        settings = {k: get_cached_setting(k, '') for k in PROTOCOL_DEFAULTS}
        settings['itt_start_interval'] = get_cached_setting('itt_start_interval', '30')
        settings['itt_countdown_sound'] = get_cached_setting('itt_countdown_sound', '1')
        return render_template('competitions.html', competition=comp, stats=stats,
                               settings=settings, race_format=_comp_format(comp),
                               formats=RACE_FORMATS)
    finally: conn.close()


@app.route('/competition/format', methods=['POST'])
def competition_format_save():
    """Формат соревнования (один на все старты) и параметры разделки."""
    conn = get_db()
    try:
        comp = _get_competition(conn)
        fmt = (request.form.get('format') or 'mass').strip()
        if fmt not in RACE_FORMATS:
            fmt = 'mass'
        interval = request.form.get('itt_start_interval', type=int)
        if interval is None:
            try:
                interval = int(float(get_cached_setting('itt_start_interval', '30') or 30))
            except (TypeError, ValueError):
                interval = 30
        interval = max(5, min(600, interval))
        sound = '1' if request.form.get('itt_countdown_sound') else '0'
        with conn:
            conn.execute('UPDATE competitions SET format = ? WHERE id = ?', (fmt, comp['id']))
            conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                         ('itt_start_interval', str(interval)))
            conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                         ('itt_countdown_sound', sound))
        _reload_settings_cache_from_db(conn)
        _invalidate_competition_meta()
        _app_logger.info('Competition format set: %s interval=%s sound=%s', fmt, interval, sound)
        flash(f'Формат стартов: «{RACE_FORMATS[fmt]}»' + (f', интервал {interval} с' if fmt == 'itt' else ''), 'success')
    finally:
        conn.close()
    return redirect(url_for('competitions_list'))

@app.route('/competitions/<int:cid>/edit', methods=['POST'])
def competition_edit(cid):
    name = request.form.get('name', '').strip()
    if not name: flash('Название пустое', 'error'); return redirect(url_for('competitions_list'))
    conn = get_db()
    try:
        with conn: conn.execute('UPDATE competitions SET name = ? WHERE id = ?', (name, cid))
        _invalidate_competition_meta()
        flash('Название сохранено', 'success'); return redirect(url_for('competitions_list'))
    finally: conn.close()

@app.route('/competitions/<int:cid>/links')
def competition_links(cid):
    """Все публичные адреса соревнования и короткие ссылки стартов."""
    conn = get_db()
    try:
        comp = _get_competition(conn)
        if not comp or comp['id'] != cid:
            return 'Соревнование не найдено', 404
        entry = _registry_find(session.get('comp')) or {}
        counts = _heat_participant_counts(conn, cid)
        heats = []
        for h in conn.execute('SELECT * FROM heats WHERE competition_id = ? ORDER BY heat_number', (cid,)).fetchall():
            c = counts.get(h['id'], {'total': 0, 'active': 0})
            token = h['startlist_token'] if 'startlist_token' in h.keys() else None
            heats.append({
                'id': h['id'],
                'number': h['heat_number'],
                'name': h['name'] or '',
                'status': h['status'],
                'total': c['active'],
                'token': token,
                'short_url': (_base_url() + url_for('startlist_public', token=token)) if token else '',
            })
        return render_template('competition_links.html', competition=comp,
                               slug=entry.get('slug') or '', archived=bool(entry.get('archived')),
                               heats=heats, registration=get_cached_setting('reg_enabled', '0') == '1',
                               base_url=_base_url())
    finally:
        conn.close()


@app.route('/competitions/<int:cid>/categories', methods=['GET', 'POST'])
def categories_list(cid):
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp: return 'Не найдено', 404
        form = {'name': '', 'laps_total': '1', 'gender': [], 'year_from': '', 'year_to': ''}
        form_error = None
        error_field = None
        if request.method == 'POST':
            form = {
                'name': (request.form.get('name') or '').strip(),
                'laps_total': (request.form.get('laps_total') or '').strip() or '1',
                'gender': request.form.getlist('gender'),
                'year_from': (request.form.get('year_from') or '').strip(),
                'year_to': (request.form.get('year_to') or '').strip(),
            }
            name = form['name']
            try:
                laps = int(form['laps_total'])
            except ValueError:
                laps = 0
            gender = _join_genders(form['gender'])
            year_from = int(form['year_from']) if form['year_from'].isdigit() else None
            year_to = int(form['year_to']) if form['year_to'].isdigit() else None
            if not name:
                form_error, error_field = 'Введите название категории', 'name'
            elif laps < 1:
                form_error, error_field = 'Кругов должно быть не меньше 1', 'laps_total'
            elif year_from and year_to and year_from > year_to:
                form_error, error_field = 'Год «с» не может быть больше года «по»', 'year_to'
            else:
                try:
                    with conn:
                        cur = conn.execute('INSERT INTO categories (competition_id, name, laps_total, gender, year_from, year_to) '
                                           'VALUES (?, ?, ?, ?, ?, ?)', (cid, name, laps, gender, year_from, year_to))
                        # порядок сортировки по умолчанию = ID категории
                        conn.execute('UPDATE categories SET sort_order = id WHERE id = ?', (cur.lastrowid,))
                    flash('Добавлено', 'success')
                    return redirect(url_for('categories_list', cid=cid))
                except sqlite3.IntegrityError:
                    form_error, error_field = f'«{name}» уже есть', 'name'
            # ошибка: страница рендерится сразу, введённые значения сохраняются
        categories = conn.execute(
            'SELECT * FROM categories WHERE competition_id = ? '
            'ORDER BY COALESCE(sort_order, id), id', (cid,)).fetchall()
        return render_template('categories.html', competition=comp, categories=categories,
                               overlaps=_category_overlaps(categories),
                               form=form, form_error=form_error, error_field=error_field,
                               submitted={}, row_errors={})
    finally: conn.close()

CATEGORY_HEADER_ALIASES = {
    'name': ['название', 'категория', 'группа', 'name', 'category'],
    'laps': ['кругов', 'круги', 'круг', 'количество кругов', 'кол во кругов', 'laps', 'laps total'],
    'gender': ['пол', 'gender', 'sex'],
    'year_from': ['год с', 'год от', 'с года', 'год рождения с', 'year from'],
    'year_to': ['год по', 'по год', 'до года', 'год рождения по', 'year to'],
}


def _parse_gender_cell(raw):
    """«М», «мужской», «M» → 'M'; «Ж»/«F» → 'F'; пусто/«оба» → None (без ограничения)."""
    s = _norm_header(raw)
    if not s:
        return None
    if s in ('м', 'm', 'муж', 'мужской', 'мужчины', 'male', 'юноши', 'мальчики'):
        return 'M'
    if s in ('ж', 'f', 'жен', 'женский', 'женщины', 'female', 'девушки', 'девочки'):
        return 'F'
    return None


@app.route('/competitions/<int:cid>/categories/template.xlsx')
def categories_template(cid):
    """Шаблон Excel для импорта категорий."""
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp: return 'Не найдено', 404
    finally:
        conn.close()
    wb = Workbook()
    ws = wb.active
    ws.title = 'Категории'
    ws.append(['Название', 'Кругов', 'Пол', 'Год с', 'Год по'])
    ws.append(['Мужчины 18+', 5, 'М', 1900, 2008])
    ws.append(['Женщины 18+', 4, 'Ж', 1900, 2008])
    ws.append(['Мальчики 8 лет и младше', 2, 'М', 2018, 2026])
    ws.append(['Юноши 12–14', 3, 'М', 2012, 2014])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for i, w in enumerate([32, 10, 8, 10, 10], start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = 'A2'
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='kategorii-shablon.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/competitions/<int:cid>/categories/import', methods=['POST'])
def categories_import(cid):
    """Импорт категорий из Excel: проверка и предпросмотр."""
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp: return 'Не найдено', 404
        upload = request.files.get('file')
        if not upload or not upload.filename:
            flash('Выберите файл Excel (.xlsx)', 'error')
            return redirect(url_for('categories_list', cid=cid))
        try:
            wb = load_workbook(io.BytesIO(upload.read()), read_only=True, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
        except Exception as ex:
            flash(f'Не удалось прочитать файл: {ex}', 'error')
            return redirect(url_for('categories_list', cid=cid))
        if not rows:
            flash('Файл пуст', 'error')
            return redirect(url_for('categories_list', cid=cid))

        header = [_norm_header(h) for h in rows[0]]
        cols = {k: _find_header_col(header, aliases) for k, aliases in CATEGORY_HEADER_ALIASES.items()}
        if cols['name'] is None:
            flash('В файле нет колонки «Название»', 'error')
            return redirect(url_for('categories_list', cid=cid))

        existing = {}
        for c in conn.execute('SELECT name FROM categories WHERE competition_id = ?', (cid,)).fetchall():
            existing[_norm_name(c['name'])] = c['name']

        def cell(idx):
            return rows_i[idx] if idx is not None and idx < len(rows_i) else None

        parsed, seen = [], {}
        ok = 0
        for i, rows_i in enumerate(rows[1:], start=2):
            if rows_i is None:
                continue
            name = _cell_text(cell(cols['name']))
            if not name:
                continue
            laps_raw = cell(cols['laps'])
            try:
                laps = int(float(str(laps_raw).replace(',', '.'))) if laps_raw not in (None, '') else 1
            except (TypeError, ValueError):
                laps = 0
            gender = _parse_gender_cell(cell(cols['gender']))
            y1 = _parse_year(cell(cols['year_from']))
            y2 = _parse_year(cell(cols['year_to']))

            status, message = 'new', ''
            key = _norm_name(name)
            if key in existing:
                status, message = 'dup', f'Уже есть: «{existing[key]}»'
            elif key in seen:
                status, message = 'dup', f'Повтор строки {seen[key]}'
            elif laps < 1:
                status, message = 'error', 'Кругов должно быть не меньше 1'
            elif y1 and y2 and y1 > y2:
                status, message = 'error', 'Год «с» больше года «по»'
            else:
                seen[key] = i
            if status == 'new':
                ok += 1
            parsed.append({'row': i, 'name': name, 'laps': laps or 1, 'gender': gender,
                           'year_from': y1, 'year_to': y2, 'status': status, 'message': message})

        if not parsed:
            flash('В файле нет строк с категориями', 'error')
            return redirect(url_for('categories_list', cid=cid))
        return render_template('categories_import.html', competition=comp, rows=parsed, ok=ok)
    finally:
        conn.close()


@app.route('/competitions/<int:cid>/categories/import/confirm', methods=['POST'])
def categories_import_confirm(cid):
    """Записывает проверенные категории из предпросмотра."""
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp: return 'Не найдено', 404
        indices = {int(m.group(1)) for k in request.form if (m := re.match(r'row_(\d+)_name$', k))}
        added, skipped = 0, 0
        with conn:
            for i in sorted(indices):
                prefix = f'row_{i}_'
                name = (request.form.get(prefix + 'name') or '').strip()
                if not name:
                    continue
                try:
                    laps = int(request.form.get(prefix + 'laps', type=int) or 1)
                except (TypeError, ValueError):
                    laps = 1
                gender = (request.form.get(prefix + 'gender') or '').strip() or None
                y1 = request.form.get(prefix + 'year_from', type=int)
                y2 = request.form.get(prefix + 'year_to', type=int)
                try:
                    cur = conn.execute('INSERT INTO categories (competition_id, name, laps_total, gender, year_from, year_to) '
                                       'VALUES (?, ?, ?, ?, ?, ?)', (cid, name, max(1, laps), gender, y1, y2))
                    conn.execute('UPDATE categories SET sort_order = id WHERE id = ?', (cur.lastrowid,))
                    added += 1
                except sqlite3.IntegrityError:
                    skipped += 1
        flash(f'Импорт категорий: добавлено {added}' + (f', пропущено {skipped}' if skipped else ''), 'success')
        return redirect(url_for('categories_list', cid=cid))
    finally:
        conn.close()



@app.route('/competitions/<int:cid>/categories/save_all', methods=['POST'])
def categories_save_all(cid):
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp: return 'Не найдено', 404
        ids = sorted({int(m.group(1)) for k in request.form if (m := re.match(r'^cat_(\d+)_name$', k))})
        updated, errors = 0, []
        submitted, row_errors = {}, {}
        with conn:
            for cat_id in ids:
                cat = conn.execute('SELECT * FROM categories WHERE id = ? AND competition_id = ?', (cat_id, cid)).fetchone()
                if not cat: continue
                raw = {
                    'name': (request.form.get(f'cat_{cat_id}_name') or '').strip(),
                    'sort_order': (request.form.get(f'cat_{cat_id}_sort') or '').strip(),
                    'laps': (request.form.get(f'cat_{cat_id}_laps') or '').strip(),
                    'lap_length': (request.form.get(f'cat_{cat_id}_lap_length') or '').strip(),
                    'hd': (request.form.get(f'cat_{cat_id}_hd') or '').strip(),
                    'ms': (request.form.get(f'cat_{cat_id}_ms') or '').strip(),
                    'ts': (request.form.get(f'cat_{cat_id}_ts') or '').strip(),
                    'gender': _join_genders(request.form.getlist(f'cat_{cat_id}_gender')),
                    'year_from': (request.form.get(f'cat_{cat_id}_year_from') or '').strip(),
                    'year_to': (request.form.get(f'cat_{cat_id}_year_to') or '').strip(),
                }
                submitted[cat_id] = raw
                name = raw['name']
                # порядок сортировки: пусто → как ID категории
                sort_order = int(raw['sort_order']) if raw['sort_order'].lstrip('-').isdigit() else cat['id']
                laps = int(raw['laps']) if raw['laps'].lstrip('-').isdigit() else None
                lap_length = raw['lap_length'] or None
                hd = raw['hd'] or None
                ms = raw['ms'] or None
                ts = raw['ts'] or None
                gender = raw['gender']
                year_from = int(raw['year_from']) if raw['year_from'].isdigit() else None
                year_to = int(raw['year_to']) if raw['year_to'].isdigit() else None
                if (name == cat['name'] and sort_order == (cat['sort_order'] if cat['sort_order'] is not None else cat['id'])
                        and laps == cat['laps_total'] and lap_length == cat['lap_length']
                        and hd == cat['hd'] and ms == cat['ms'] and ts == cat['ts']
                        and gender == cat['gender'] and year_from == cat['year_from']
                        and year_to == cat['year_to']): continue

                def _fail(msg, field):
                    errors.append(msg)
                    row_errors[cat_id] = (msg, field)

                if not name:
                    _fail(f'«{cat["name"]}»: пустое название', 'name'); continue
                if laps is None or laps < 1:
                    _fail(f'«{name}»: кругов должно быть не меньше 1', 'laps'); continue
                if year_from and year_to and year_from > year_to:
                    _fail(f'«{name}»: год «с» больше года «по»', 'year_to'); continue
                try:
                    conn.execute('UPDATE categories SET name = ?, laps_total = ?, lap_length = ?, '
                                 'hd = ?, ms = ?, ts = ?, gender = ?, year_from = ?, year_to = ?, '
                                 'sort_order = ? WHERE id = ?',
                                 (name, laps, lap_length, hd, ms, ts, gender, year_from, year_to,
                                  sort_order, cat_id))
                    updated += 1
                except sqlite3.IntegrityError:
                    _fail(f'«{name}» уже есть', 'name')
        if errors:
            # не теряем введённое: показываем страницу с подсвеченными полями
            categories = conn.execute('SELECT * FROM categories WHERE competition_id = ? ORDER BY COALESCE(sort_order, id), id', (cid,)).fetchall()
            for e in errors[:8]: flash(e, 'error')
            if len(errors) > 8: flash(f'… и ещё {len(errors) - 8} ошибок', 'error')
            return render_template('categories.html', competition=comp, categories=categories,
                                   overlaps=_category_overlaps(categories),
                                   form={'name': '', 'laps_total': '1', 'gender': [], 'year_from': '', 'year_to': ''},
                                   form_error=None, error_field=None,
                                   submitted=submitted, row_errors=row_errors)
        if updated: flash(f'Сохранено: {updated}', 'success')
        if not updated and not errors: flash('Изменений нет', 'success')
        return redirect(url_for('categories_list', cid=cid))
    finally: conn.close()

@app.route('/competitions/<int:cid>/categories/reorder', methods=['POST'])
def categories_reorder(cid):
    """Автосохранение порядка категорий после перетаскивания строк (AJAX)."""
    conn = get_db()
    try:
        comp = conn.execute('SELECT id FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp:
            return jsonify({'ok': False, 'message': 'Соревнование не найдено'}), 404
        raw = request.form.get('order')
        if raw is None:
            payload = request.get_json(silent=True) or {}
            raw = payload.get('order')
        if isinstance(raw, (list, tuple)):
            ids = [int(x) for x in raw if str(x).strip().isdigit()]
        else:
            ids = [int(x) for x in re.split(r'[,\s]+', str(raw or '')) if x.strip().isdigit()]
        if not ids:
            return jsonify({'ok': False, 'message': 'Пустой порядок'}), 400
        placeholders = ','.join('?' * len(ids))
        found = {r['id'] for r in conn.execute(
            f'SELECT id FROM categories WHERE competition_id = ? AND id IN ({placeholders})',
            [cid] + ids).fetchall()}
        if len(found) != len(set(ids)):
            return jsonify({'ok': False, 'message': 'Категория не найдена в этом соревновании'}), 400
        with conn:
            for pos, cat_id in enumerate(ids, start=1):
                conn.execute('UPDATE categories SET sort_order = ? WHERE id = ? AND competition_id = ?',
                             (pos, cat_id, cid))
        return jsonify({'ok': True, 'saved': len(ids)})
    finally:
        conn.close()

@app.route('/categories/<int:catid>/delete', methods=['POST'])
def category_delete(catid):
    conn = get_db()
    try:
        cat = conn.execute('SELECT * FROM categories WHERE id = ?', (catid,)).fetchone()
        if not cat: return 'Не найдено', 404
        cid = cat['competition_id']
        if conn.execute('SELECT COUNT(*) FROM participants WHERE category_id = ?', (catid,)).fetchone()[0] > 0:
            flash('В категории есть участники', 'error'); return redirect(url_for('categories_list', cid=cid))
        with conn:
            conn.execute('DELETE FROM heat_categories WHERE category_id = ?', (catid,))
            conn.execute('DELETE FROM categories WHERE id = ?', (catid,))
        flash('Удалено', 'success'); return redirect(url_for('categories_list', cid=cid))
    finally: conn.close()

PARTICIPANT_SORTS = {
    'category': 'c.name, p.number',
    'category_desc': 'c.name DESC, p.number',
    'number': 'p.number',
    'number_desc': 'p.number DESC',
    'name': 'p.full_name COLLATE NOCASE',
    'name_desc': 'p.full_name COLLATE NOCASE DESC',
    'year': 'p.birth_year IS NULL, p.birth_year, p.number',
    'year_desc': 'p.birth_year IS NULL, p.birth_year DESC, p.number',
    'gender': 'p.gender, p.number',
    'gender_desc': 'p.gender DESC, p.number',
    'city': "p.city IS NULL OR p.city = '', p.city COLLATE NOCASE, p.number",
    'city_desc': "p.city IS NULL OR p.city = '', p.city COLLATE NOCASE DESC, p.number",
    'team': "p.team IS NULL OR p.team = '', p.team COLLATE NOCASE, p.number",
    'team_desc': "p.team IS NULL OR p.team = '', p.team COLLATE NOCASE DESC, p.number",
}

def _participants_redirect(cid, back, q=''):
    url = url_for('participants_list', cid=cid)
    parts = [p for p in (back or '').split('&') if p and not p.startswith('q=')]
    if q:
        from urllib.parse import quote
        parts.append('q=' + quote(q))
    return redirect(url + ('?' + '&'.join(parts) if parts else ''))

@app.route('/competitions/<int:cid>')
def participants_list(cid):
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp: return 'Не найдено', 404
        categories = conn.execute('SELECT * FROM categories WHERE competition_id = ? ORDER BY name', (cid,)).fetchall()

        q = request.args.get('q', '').strip()
        cat = request.args.get('cat', '').strip()
        city = request.args.get('city', '').strip()
        team = request.args.get('team', '').strip()
        gender = request.args.get('gender', '').strip()
        year_op = request.args.get('year_op', '').strip()
        year1_raw = request.args.get('year1', '').strip()
        year2_raw = request.args.get('year2', '').strip()
        order = request.args.get('order', 'category').strip()
        if order not in PARTICIPANT_SORTS: order = 'category'

        where, params = ['p.competition_id = ?'], [cid]
        if cat == '0':
            where.append('p.category_id IS NULL')
        elif cat.isdigit():
            where.append('p.category_id = ?'); params.append(int(cat))
        if city: where.append('p.city LIKE ?'); params.append(f'%{city}%')
        if team: where.append('p.team LIKE ?'); params.append(f'%{team}%')
        if gender in ('M', 'F'): where.append('p.gender = ?'); params.append(gender)

        y1, y2 = _parse_year(year1_raw), _parse_year(year2_raw)
        if y1 is not None:
            if year_op == 'gt': where.append('p.birth_year > ?'); params.append(y1)
            elif year_op == 'lt': where.append('p.birth_year < ?'); params.append(y1)
            elif year_op == 'between':
                if y2 is not None:
                    where.append('p.birth_year BETWEEN ? AND ?'); params += [min(y1, y2), max(y1, y2)]
                else:
                    where.append('p.birth_year >= ?'); params.append(y1)
            else: where.append('p.birth_year = ?'); params.append(y1)
        elif year_op == 'between' and y2 is not None:
            where.append('p.birth_year <= ?'); params.append(y2)

        sql = ('SELECT p.*, c.name AS category_name, c.laps_total AS category_laps '
               'FROM participants p LEFT JOIN categories c ON c.id = p.category_id '
               f'WHERE {" AND ".join(where)} ORDER BY {PARTICIPANT_SORTS[order]}')
        parts = conn.execute(sql, params).fetchall()
        total_all = _scalar(conn, 'SELECT COUNT(*) FROM participants WHERE competition_id = ?', cid)
        has_none_category = bool(_scalar(conn, 'SELECT COUNT(*) FROM participants WHERE competition_id = ? AND category_id IS NULL', cid))
        base_params = {k: v for k, v in (
            ('cat', cat), ('city', city), ('team', team), ('gender', gender),
            ('year_op', year_op), ('year1', year1_raw), ('year2', year2_raw)) if v}
        sort_urls = {}
        for col in ('number', 'name', 'year', 'category', 'gender', 'city', 'team'):
            def make(key, _bp=base_params):
                return url_for('participants_list', cid=cid, **dict(_bp, order=key))
            state = 'asc' if order == col else ('desc' if order == col + '_desc' else None)
            sort_urls[col] = {'asc': make(col), 'desc': make(col + '_desc'), 'state': state}

        filters = {'q': q, 'cat': cat, 'city': city, 'team': team, 'gender': gender,
                   'year_op': year_op, 'year1': year1_raw, 'year2': year2_raw, 'order': order}
        return render_template('participants.html', competition=comp, participants=parts,
                               categories=categories, q=q, total=total_all, shown=len(parts),
                               filters=filters, sort_urls=sort_urls,
                               has_none_category=has_none_category,
                               tags_by_number=_tags_by_number(conn, cid),
                               qs=request.query_string.decode('utf-8', 'ignore'),
                               next_default=_next_reg_number(conn, cid))
    finally: conn.close()

@app.route('/competitions/<int:cid>/numbers', methods=['GET', 'POST'])
def numbers_page(cid):
    """Быстрая выдача стартовых номеров (участник подошёл к судье на старте)."""
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp: return 'Не найдено', 404

        if request.method == 'POST':
            pid = request.form.get('pid', type=int)
            number, err = _issue_number(conn, cid, pid, request.form.get('number'))
            if err:
                flash(err, 'error')
            else:
                person = conn.execute('SELECT full_name FROM participants WHERE id = ?', (pid,)).fetchone()
                flash(f'Номер {number} выдан: {person["full_name"]}', 'success')
            return redirect(url_for('numbers_page', cid=cid, q=request.form.get('q', '')))

        q = request.args.get('q', '').strip()
        where, params = ['p.competition_id = ?'], [cid]
        if q:
            where.append('(p.full_name LIKE ? OR CAST(p.id AS TEXT) = ? OR CAST(p.number AS TEXT) = ?)')
            params += [f'%{q}%', q, q]
        rows = conn.execute(
            'SELECT p.*, c.name AS category_name FROM participants p '
            'LEFT JOIN categories c ON c.id = p.category_id '
            f'WHERE {" AND ".join(where)} '
            'ORDER BY (p.number IS NOT NULL), p.number, p.full_name', params).fetchall()
        without = conn.execute(
            'SELECT COUNT(*) FROM participants WHERE competition_id = ? AND number IS NULL',
            (cid,)).fetchone()[0]
        return render_template('numbers.html', competition=comp, rows=rows, q=q,
                               without=without, next_number=_next_number(conn, cid))
    finally:
        conn.close()


TAGS_HEADER_ALIASES = {
    'number': ['номер', 'номер на старте', 'стартовый номер', 'bib', 'number'],
    'tag1': ['метка', 'метка 1', 'основная метка', 'основная', 'tag', 'tag1', 'epc'],
    'tag2': ['метка 2', 'резервная метка', 'резервная', 'tag2', 'epc 2'],
}


@app.route('/competitions/<int:cid>/tags/template.xlsx')
def bib_tags_template(cid):
    """Шаблон Excel для импорта меток на номера."""
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp:
            return 'Не найдено', 404
    finally:
        conn.close()
    wb = Workbook()
    ws = wb.active
    ws.title = 'Метки'
    ws.append(['Номер', 'Метка 1', 'Метка 2'])
    ws.append([1, 'E28011700000020F5A4B0001', ''])
    ws.append([2, 'E28011700000020F5A4B0002', 'E28011700000020F5A4B0012'])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for i, w in enumerate([10, 34, 34], start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = 'A2'
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='metki-shablon.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/competitions/<int:cid>/tags/import', methods=['POST'])
def bib_tags_import(cid):
    """Импорт меток из Excel: проверка и предпросмотр."""
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp:
            return 'Не найдено', 404
        upload = request.files.get('file')
        if not upload or not upload.filename:
            flash('Выберите файл Excel (.xlsx)', 'error')
            return redirect(url_for('bib_tags_page', cid=cid))
        try:
            wb = load_workbook(io.BytesIO(upload.read()), read_only=True, data_only=True)
            rows = list(wb.active.iter_rows(values_only=True))
        except Exception as ex:
            flash(f'Не удалось прочитать файл: {ex}', 'error')
            return redirect(url_for('bib_tags_page', cid=cid))
        if not rows:
            flash('Файл пуст', 'error')
            return redirect(url_for('bib_tags_page', cid=cid))

        header = [_norm_header(h) for h in rows[0]]
        cols = {k: _find_header_col(header, aliases) for k, aliases in TAGS_HEADER_ALIASES.items()}
        if cols['number'] is None or (cols['tag1'] is None and cols['tag2'] is None):
            flash('В файле нужны колонки «Номер» и «Метка»', 'error')
            return redirect(url_for('bib_tags_page', cid=cid))

        existing = {}
        for r in conn.execute('SELECT number, tag FROM bib_tags WHERE competition_id = ?', (cid,)).fetchall():
            existing[r['tag'].upper()] = r['number']
        have_numbers = {r['number'] for r in conn.execute(
            'SELECT number FROM bib_tags WHERE competition_id = ?', (cid,)).fetchall()}

        def cell(idx, row):
            return row[idx] if idx is not None and idx < len(row) else None

        parsed, seen_tags, ok = [], {}, 0
        for i, row in enumerate(rows[1:], start=2):
            if row is None:
                continue
            raw_number = cell(cols['number'], row)
            if raw_number in (None, ''):
                continue
            try:
                number = int(float(str(raw_number).replace(',', '.')))
            except (TypeError, ValueError):
                parsed.append({'row': i, 'number': None, 'tags': [], 'status': 'error',
                               'message': 'Номер не число'})
                continue
            if number < 1:
                parsed.append({'row': i, 'number': number, 'tags': [], 'status': 'error',
                               'message': 'Номер должен быть больше нуля'})
                continue

            tags = []
            for key in ('tag1', 'tag2'):
                t = re.sub(r'\s+', '', _cell_text(cell(cols[key], row))).upper()
                if t:
                    tags.append(t)
            status, message = ('replace' if number in have_numbers else 'new'), ''
            if not tags:
                status, message = 'error', 'Не указана ни одна метка'
            elif len(tags) != len(set(tags)):
                status, message = 'error', 'Метка указана дважды в строке'
            else:
                for t in tags:
                    if t in existing and existing[t] != number:
                        status, message = 'error', f'Метка {t} уже на номере {existing[t]}'
                        break
                    if t in seen_tags and seen_tags[t] != number:
                        status, message = 'error', f'Метка {t} встречается в строке {seen_tags[t]}'
                        break
                    if t in seen_tags and seen_tags[t] == number:
                        status, message = 'error', f'Метка {t} уже была у номера {number} выше'
                        break
                if status != 'error':
                    for t in tags:
                        seen_tags[t] = number
            if status != 'error':
                ok += 1
            parsed.append({'row': i, 'number': number, 'tags': tags, 'status': status, 'message': message})

        if not parsed:
            flash('В файле нет строк с метками', 'error')
            return redirect(url_for('bib_tags_page', cid=cid))
        return render_template('bib_tags_import.html', competition=comp, rows=parsed, ok=ok)
    finally:
        conn.close()


@app.route('/competitions/<int:cid>/tags/import/confirm', methods=['POST'])
def bib_tags_import_confirm(cid):
    """Записывает проверенные метки из предпросмотра (заменяет метки номера)."""
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp:
            return 'Не найдено', 404
        numbers = {int(m.group(1)) for k in request.form if (m := re.match(r'row_(\d+)_number$', k))}
        saved, errors = 0, []
        for i in sorted(numbers):
            number = request.form.get(f'row_{i}_number', type=int)
            if not number or number < 1:
                continue
            tags = [request.form.get(f'row_{i}_tag1') or '', request.form.get(f'row_{i}_tag2') or '']
            ok, err = _save_bib_tags(conn, cid, number, tags)
            if ok:
                saved += 1
            else:
                errors.append(f'№{number}: {err}')
        flash(f'Импорт меток: сохранено {saved}' + (f', ошибок {len(errors)}' if errors else ''), 'success' if not errors else 'error')
        for e in errors[:8]:
            flash(e, 'error')
        return redirect(url_for('bib_tags_page', cid=cid))
    finally:
        conn.close()


@app.route('/competitions/<int:cid>/tags', methods=['GET', 'POST'])
def bib_tags_page(cid):
    """Метки на номерах: номер ↔ RFID-метка (основная и резервная)."""
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp:
            return 'Не найдено', 404

        if request.method == 'POST':
            action = (request.form.get('action') or 'save').strip()
            number = request.form.get('number', type=int)
            if not number or number < 1:
                flash('Укажите номер', 'error')
                return redirect(url_for('bib_tags_page', cid=cid))
            if action == 'delete':
                with conn:
                    conn.execute('DELETE FROM bib_tags WHERE competition_id = ? AND number = ?', (cid, number))
                flash(f'Метки с номера {number} сняты', 'success')
            else:
                ok, err = _save_bib_tags(conn, cid, number,
                                         [request.form.get('tag1') or '', request.form.get('tag2') or ''])
                if not ok:
                    flash(err, 'error')
                elif not (request.form.get('tag1') or '').strip() and not (request.form.get('tag2') or '').strip():
                    flash(f'Метки номера {number} сняты', 'success')
                else:
                    flash(f'Метки номера {number} сохранены', 'success')
            return redirect(url_for('bib_tags_page', cid=cid))

        rows = conn.execute(
            'SELECT bt.number, bt.tag, bt.slot, '
            '       (SELECT p.full_name FROM participants p '
            '         WHERE p.competition_id = bt.competition_id AND p.number = bt.number) AS full_name, '
            '       (SELECT p.id FROM participants p '
            '         WHERE p.competition_id = bt.competition_id AND p.number = bt.number) AS pid '
            'FROM bib_tags bt WHERE bt.competition_id = ? ORDER BY bt.number, bt.slot', (cid,)).fetchall()
        grouped, order = {}, []
        for r in rows:
            if r['number'] not in grouped:
                grouped[r['number']] = {'number': r['number'], 'full_name': r['full_name'],
                                        'pid': r['pid'], 'tags': []}
                order.append(r['number'])
            grouped[r['number']]['tags'].append(r['tag'])

        without = conn.execute(
            'SELECT p.number, p.full_name FROM participants p '
            'WHERE p.competition_id = ? AND p.number IS NOT NULL '
            '  AND NOT EXISTS (SELECT 1 FROM bib_tags bt '
            '                  WHERE bt.competition_id = p.competition_id AND bt.number = p.number) '
            'ORDER BY p.number', (cid,)).fetchall()

        return render_template('bib_tags.html', competition=comp,
                               groups=[grouped[n] for n in order], without=without)
    finally:
        conn.close()


@app.route('/competitions/<int:cid>/participants/bulk', methods=['POST'])
def participants_bulk(cid):
    """Массовые операции над выбранными участниками."""
    pids = request.form.getlist('pid', type=int)
    action = request.form.get('bulk', '')
    back = request.form.get('back', '')
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp: return 'Не найдено', 404
        if not pids:
            flash('Не выбран ни один участник', 'error')
            return _participants_redirect(cid, back)
        ph = ','.join('?' * len(pids))

        if action == 'category':
            raw = (request.form.get('bulk_category') or '').strip()
            cat_id = int(raw) if raw else None
            if cat_id is not None and not conn.execute(
                    'SELECT id FROM categories WHERE id = ? AND competition_id = ?', (cat_id, cid)).fetchone():
                flash('Категория не найдена', 'error'); return _participants_redirect(cid, back)
            with conn:
                conn.execute(f'UPDATE participants SET category_id = ? WHERE competition_id = ? AND id IN ({ph})',
                             [cat_id, cid] + pids)
            flash(f'Группа назначена участникам: {len(pids)}', 'success')
        elif action == 'city':
            value = (request.form.get('bulk_city') or '').strip() or None
            with conn:
                conn.execute(f'UPDATE participants SET city = ? WHERE competition_id = ? AND id IN ({ph})',
                             [value, cid] + pids)
            flash(f'Город задан участникам: {len(pids)}', 'success')
        elif action == 'team':
            value = (request.form.get('bulk_team') or '').strip() or None
            with conn:
                conn.execute(f'UPDATE participants SET team = ? WHERE competition_id = ? AND id IN ({ph})',
                             [value, cid] + pids)
            flash(f'Команда задана участникам: {len(pids)}', 'success')
        elif action == 'delete':
            with conn:
                conn.execute(f'DELETE FROM participants WHERE competition_id = ? AND id IN ({ph})',
                             [cid] + pids)
            flash(f'Удалено участников: {len(pids)}', 'success')
        else:
            flash('Неизвестное действие', 'error')
        return _participants_redirect(cid, back)
    finally: conn.close()

@app.route('/competitions/<int:cid>/participants/add', methods=['POST'])
def participant_add(cid):
    number, full_name, birth_year, category_id, gender = [request.form.get(x, '').strip() for x in ['number', 'full_name', 'birth_year', 'category_id', 'gender']]
    def err(msg): flash(msg, 'error'); return redirect(url_for('participants_list', cid=cid))
    if not full_name: return err('ФИО обязательно')
    if gender not in ('M', 'F'): return err('Выберите пол')

    cat_int = int(category_id) if category_id else None

    conn = get_db()
    try:
        if cat_int is not None and not conn.execute('SELECT id FROM categories WHERE id = ? AND competition_id = ?', (cat_int, cid)).fetchone(): return err('Категория не найдена')
        if cat_int is None:
            cat_int = _auto_category(conn, cid, gender, _parse_year(birth_year))

        try: number_int = int(number) if number else None
        except: return err('Номер должен быть числом')
        if number_int is not None and number_int < 1: return err('Номер должен быть больше нуля')
        try:
            with conn:
                cur = conn.execute('INSERT INTO participants (competition_id, number, full_name, birth_year, category_id, gender) VALUES (?, ?, ?, ?, ?, ?)', (cid, number_int, full_name, _parse_year(birth_year), cat_int, gender))
                pid_new = cur.lastrowid
            flash(f'Добавлен: {("№" + str(number_int)) if number_int else "ID " + str(pid_new)}', 'success')
        except sqlite3.IntegrityError:
            flash(f'Номер {number_int} занят', 'error')
        return redirect(url_for('participants_list', cid=cid))
    finally: conn.close()


@app.route('/competitions/<int:cid>/participants/save_all', methods=['POST'])
def participants_save_all(cid):
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp: return 'Не найдено', 404
        q = request.form.get('q', '').strip(); pids = {int(m.group(1)) for k in request.form if (m := re.match(r'^(?:number|full_name|birth_year|category_id|gender|city|team)_(\d+)$', k))}
        back = request.form.get('back', '')
        def redirect_back(): return _participants_redirect(cid, back, q)
        if not pids: flash('Нет данных', 'error'); return redirect_back()
        valid_cats = {r['id'] for r in conn.execute('SELECT id FROM categories WHERE competition_id = ?', (cid,)).fetchall()}
        updated, errors = 0, []
        with conn:
            for pid in sorted(pids):
                p = conn.execute('SELECT * FROM participants WHERE id = ? AND competition_id = ?', (pid, cid)).fetchone()
                if not p: continue
                number_str = (request.form.get(f'number_{pid}') or '').strip(); full_name = (request.form.get(f'full_name_{pid}') or '').strip()
                birth_year_str = (request.form.get(f'birth_year_{pid}') or '').strip(); category_id = request.form.get(f'category_id_{pid}', type=int)
                gender = (request.form.get(f'gender_{pid}') or '').strip()
                city = (request.form.get(f'city_{pid}') or '').strip() or None
                team = (request.form.get(f'team_{pid}') or '').strip() or None

                try: number_int = int(number_str) if number_str else None
                except: errors.append(f'{_plabel(p)}: неверный номер'); continue
                year_int = _parse_year(birth_year_str)
                if birth_year_str and year_int is None:
                    errors.append(f'{_plabel(p)}: неверный год'); continue
                if (number_int == p['number'] and full_name == p['full_name'] and year_int == p['birth_year']
                        and category_id == p['category_id'] and gender == p['gender']
                        and city == p['city'] and team == p['team']): continue
                if number_int is not None and number_int < 1:
                    errors.append(f'{_plabel(p)}: номер должен быть больше нуля'); continue
                if not full_name: errors.append(f'{_plabel(p)}: ФИО обязательно'); continue
                if category_id is not None and category_id not in valid_cats: errors.append(f'{_plabel(p)}: категория не найдена'); continue
                if gender not in ('M', 'F'): errors.append(f'{_plabel(p)}: не выбран пол'); continue

                try:
                    conn.execute('UPDATE participants SET number = ?, full_name = ?, birth_year = ?, category_id = ?, gender = ?, city = ?, team = ? WHERE id = ?', (number_int, full_name, year_int, category_id, gender, city, team, pid))
                    updated += 1
                except sqlite3.IntegrityError:
                    errors.append(f'{_plabel(p)}: номер {number_int} занят')
        if updated: flash(f'Сохранено: {updated}', 'success')
        if errors:
            for e in errors[:8]: flash(e, 'error')
            if len(errors) > 8: flash(f'… и ещё {len(errors) - 8} ошибок', 'error')
        if not updated and not errors: flash('Изменений нет', 'success')
        return redirect_back()
    finally: conn.close()

@app.route('/participants/<int:pid>/delete', methods=['POST'])
def participant_delete(pid):
    conn = get_db()
    try:
        p = conn.execute('SELECT competition_id FROM participants WHERE id = ?', (pid,)).fetchone()
        if p:
            cid = p['competition_id']
            with conn:
                conn.execute('DELETE FROM auto_laps WHERE race_id IN (SELECT id FROM races WHERE participant_id = ?)', (pid,))
                conn.execute('DELETE FROM laps WHERE race_id IN (SELECT id FROM races WHERE participant_id = ?)', (pid,))
                conn.execute('DELETE FROM races WHERE participant_id = ?', (pid,))
                conn.execute('DELETE FROM heat_participants WHERE participant_id = ?', (pid,))
                conn.execute('DELETE FROM participants WHERE id = ?', (pid,))
            flash('Удалён', 'success'); return redirect(url_for('participants_list', cid=cid))
        return redirect(url_for('competitions_list'))
    finally: conn.close()

@app.route('/competitions/<int:cid>/participants/template.xlsx')
def participants_template(cid):
    wb = Workbook(); ws = wb.active; ws.title = 'Участники'
    ws.append(['ФИО', 'Год рождения', 'Категория', 'Пол', 'RFID-метка', 'Город', 'Команда'])
    ws.append(['Иванов Иван Иванович', 1990, 'Мужчины 18+', 'М', '', 'Белгород', 'СШ 4'])
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='participants_template.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.route('/competitions/<int:cid>/participants/import', methods=['GET', 'POST'])
def participants_import(cid):
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp: return 'Не найдено', 404
        categories = conn.execute('SELECT * FROM categories WHERE competition_id = ? ORDER BY name', (cid,)).fetchall()
        categories_by_name = {c['name'].strip().lower(): c for c in categories}
        if request.method == 'GET':
            return render_template('participants_import.html', competition=comp, categories=categories, parsed=None, step='upload')
        f = request.files.get('file')
        if not f or not f.filename:
            flash('Выберите файл', 'error'); return redirect(url_for('participants_import', cid=cid))
        try:
            wb = load_workbook(f, data_only=True, read_only=True); ws = wb.active; rows = list(ws.iter_rows(values_only=True))
        except Exception as e:
            flash(f'Не удалось прочитать файл: {e}', 'error'); return redirect(url_for('participants_import', cid=cid))
        if not rows: flash('Файл пуст', 'error'); return redirect(url_for('participants_import', cid=cid))
        header = [_norm_header(h) for h in rows[0]]
        col_fio = _find_header_col(header, HEADER_ALIASES['fio'])
        col_year = _find_header_col(header, HEADER_ALIASES['year'])
        col_cat = _find_header_col(header, HEADER_ALIASES['category'])
        col_gender = _find_header_col(header, HEADER_ALIASES['gender'])
        col_rfid = _find_header_col(header, HEADER_ALIASES['rfid'])
        col_city = _find_header_col(header, HEADER_ALIASES['city'])
        col_team = _find_header_col(header, HEADER_ALIASES['team'])
        col_number = _find_header_col(header, HEADER_ALIASES['number'])
        if col_fio is None or col_gender is None:
            flash('В файле нет обязательных колонок (ФИО, Пол)', 'error'); return redirect(url_for('participants_import', cid=cid))
        existing_keys = {(_norm_name(ep['full_name']), ep['birth_year']) for ep in conn.execute('SELECT full_name, birth_year FROM participants WHERE competition_id = ?', (cid,)).fetchall()}
        existing_tags = {r['tag'].upper() for r in conn.execute('SELECT tag FROM bib_tags WHERE competition_id = ?', (cid,)).fetchall() if r['tag']}
        parsed, seen_in_file = [], {}
        for i, row in enumerate(rows[1:], start=2):
            if row is None: continue
            def cell(idx): return row[idx] if idx is not None and idx < len(row) else None
            fio = cell(col_fio)
            if fio is None or str(fio).strip() == '': continue
            fio = str(fio).strip()
            year = _parse_year(cell(col_year))
            cat_name = _cell_text(cell(col_cat))
            cat = categories_by_name.get(cat_name.lower()) if cat_name else None
            gender = _normalize_gender(cell(col_gender))
            rfid_tag = re.sub(r'\s+', '', _cell_text(cell(col_rfid))).upper()
            city = _cell_text(cell(col_city)) or None
            team = _cell_text(cell(col_team)) or None
            # номер не обязателен: если в файле его нет — выдадим на старте
            row_number = None
            raw_number = cell(col_number) if col_number is not None else None
            if raw_number not in (None, ''):
                try:
                    row_number = int(float(str(raw_number).strip().replace(',', '.')))
                except (TypeError, ValueError):
                    row_number = None
                if row_number is not None and row_number < 1:
                    row_number = None

            tag_conflict = ''
            if rfid_tag and rfid_tag in existing_tags:
                tag_conflict = 'existing'

            key = (_norm_name(fio), year)
            dup_kind, dup_msg = None, ''
            if key in existing_keys: dup_kind, dup_msg = 'existing', 'Уже есть в стартовом листе'
            elif key in seen_in_file: dup_kind, dup_msg = 'in_file', f'Дубликат строки {seen_in_file[key]}'
            else: seen_in_file[key] = i

            if rfid_tag and rfid_tag not in existing_tags:
                existing_tags.add(rfid_tag)

            parsed.append({'row': i, 'number': row_number, 'fio': fio, 'year': year, 'category_name': cat_name, 'category_id': cat['id'] if cat else None, 'gender': gender, 'rfid_tag': rfid_tag or None, 'city': city, 'team': team, 'dup_kind': dup_kind, 'dup_msg': dup_msg, 'tag_conflict': tag_conflict})
        if not parsed: flash('В файле нет строк с данными', 'error'); return redirect(url_for('participants_import', cid=cid))
        missing_cats = {p['category_name'] for p in parsed if p['category_name'] and not p['category_id']}
        return render_template('participants_import.html', competition=comp, categories=categories, parsed=parsed, step='preview', missing_categories=sorted(list(missing_cats)))
    finally: conn.close()

@app.route('/competitions/<int:cid>/participants/import/confirm', methods=['POST'])
def participants_import_confirm(cid):
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp: return 'Не найдено', 404
        indices = {int(m.group(1)) for k in request.form if (m := re.match(r'row_(\d+)_fio$', k))}
        existing_keys = {(_norm_name(ep['full_name']), ep['birth_year']) for ep in conn.execute('SELECT full_name, birth_year FROM participants WHERE competition_id = ?', (cid,)).fetchall()}
        existing_tags = {r['tag'].upper() for r in conn.execute('SELECT tag FROM bib_tags WHERE competition_id = ?', (cid,)).fetchall() if r['tag']}

        create_missing = request.form.get('create_missing_categories') == '1'
        newly_created_cats = {}
        created, skipped, dup_skipped, tag_skipped, errors = 0, 0, 0, 0, []

        with conn:
            for i in sorted(indices):
                prefix = f'row_{i}_'
                fio = (request.form.get(prefix + 'fio') or '').strip()
                number_str = (request.form.get(prefix + 'number') or '').strip()
                year_str = (request.form.get(prefix + 'year') or '').strip()
                category_id = request.form.get(prefix + 'category_id', type=int)
                gender = (request.form.get(prefix + 'gender') or '').strip()
                rfid_tag = (request.form.get(prefix + 'rfid_tag') or '').strip().upper() or None
                city = (request.form.get(prefix + 'city') or '').strip() or None
                team = (request.form.get(prefix + 'team') or '').strip() or None
                cat_name_form = (request.form.get(prefix + 'category_name') or '').strip()

                if not fio: skipped += 1; continue
                number_int = None
                if number_str:
                    try: number_int = int(float(number_str.replace(',', '.')))
                    except: errors.append(f'Стр. {i+1}: неверный номер'); skipped += 1; continue
                    if number_int < 1:
                        errors.append(f'Стр. {i+1}: номер должен быть больше нуля'); skipped += 1; continue
                if gender not in ('M', 'F'): errors.append(f'Стр. {i+1}: не выбран пол'); skipped += 1; continue
                # группа не обязательна: без названия категории участник импортируется «без группы»
                if not category_id and cat_name_form:
                    if create_missing:
                        cat_key = cat_name_form.lower()
                        if cat_key in newly_created_cats:
                            category_id = newly_created_cats[cat_key]
                        else:
                            c_db = conn.execute('SELECT id FROM categories WHERE competition_id = ? AND lower(name) = ?', (cid, cat_key)).fetchone()
                            if c_db:
                                category_id = c_db['id']
                            else:
                                try:
                                    category_id = conn.execute('INSERT INTO categories (competition_id, name, laps_total) VALUES (?, ?, ?)', (cid, cat_name_form, 1)).lastrowid
                                    conn.execute('UPDATE categories SET sort_order = id WHERE id = ?', (category_id,))
                                except sqlite3.IntegrityError:
                                    c_db = conn.execute('SELECT id FROM categories WHERE competition_id = ? AND name = ?', (cid, cat_name_form)).fetchone()
                                    if c_db: category_id = c_db['id']
                                    else: errors.append(f'Стр. {i+1}: ошибка создания категории'); skipped += 1; continue
                            newly_created_cats[cat_key] = category_id
                    else:
                        errors.append(f'Стр. {i+1}: категория «{cat_name_form}» не найдена'); skipped += 1; continue
                year_val = _parse_year(year_str)
                if year_str and year_val is None:
                    errors.append(f'Стр. {i+1}: неверный год'); skipped += 1; continue

                # категория из файла в приоритете; иначе подбираем по условиям (пол + год)
                if category_id is None:
                    category_id = _auto_category(conn, cid, gender, year_val)

                if rfid_tag and rfid_tag in existing_tags:
                    errors.append(f'Стр. {i+1}: метка {rfid_tag} уже занята')
                    tag_skipped += 1
                    continue

                key = (_norm_name(fio), year_val)
                if key in existing_keys: dup_skipped += 1; continue

                try:
                    conn.execute('INSERT INTO participants (competition_id, number, full_name, birth_year, category_id, gender, city, team) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (cid, number_int, fio, year_val, category_id, gender, city, team))
                    existing_keys.add(key)
                    if rfid_tag:
                        existing_tags.add(rfid_tag)
                        if number_int:
                            # метка из файла привязывается к номеру (второй слот — если номер уже с меткой)
                            current = [r['tag'] for r in conn.execute(
                                'SELECT tag FROM bib_tags WHERE competition_id = ? AND number = ? ORDER BY slot',
                                (cid, number_int)).fetchall()]
                            ok_tag, tag_err = _save_bib_tags(conn, cid, number_int, (current + [rfid_tag])[:MAX_TAGS_PER_NUMBER])
                            if not ok_tag:
                                errors.append(f'Стр. {i+1}: метка не сохранена — {tag_err}')
                    created += 1
                except sqlite3.IntegrityError:
                    errors.append(f'Стр. {i+1}: номер {number_int} занят')
                    skipped += 1

        if created: flash(f'Импортировано: {created}', 'success')
        if dup_skipped: flash(f'Пропущено дубликатов ФИО: {dup_skipped}', 'error')
        if tag_skipped: flash(f'Пропущено из-за дубликатов меток: {tag_skipped}', 'error')
        if skipped: flash(f'Пропущено строк: {skipped}', 'error')
        for e in errors[:8]: flash(e, 'error')
        if len(errors) > 8: flash(f'… и ещё {len(errors) - 8} ошибок', 'error')
        return redirect(url_for('participants_list', cid=cid))
    finally: conn.close()

@app.route('/races')
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
            if session.get('last_cid') != selected_cid: session.pop('last_action', None); session['last_cid'] = selected_cid
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
                                'year': p['birth_year'], 'gender': p['gender'],
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

def _build_races_heats(conn, cid):
    """Старты со всеми заездами — для страницы «Старт» и её автообновления."""
    heats_data = []
    for h in conn.execute('SELECT * FROM heats WHERE competition_id = ? ORDER BY heat_number', (cid,)).fetchall():
        hd = dict(h)
        heat_cats = [dict(c) for c in conn.execute('SELECT c.* FROM categories c JOIN heat_categories hc ON hc.category_id = c.id WHERE hc.heat_id = ? ORDER BY c.name', (h['id'],)).fetchall()]
        has_uncategorized = conn.execute(
            'SELECT 1 FROM heat_participants hp JOIN participants p ON p.id = hp.participant_id '
            'WHERE hp.heat_id = ? AND p.category_id IS NULL LIMIT 1', (h['id'],)).fetchone()
        if has_uncategorized:
            heat_cats.append({'id': 0, 'name': 'Без категории', 'laps_total': 1})
        hd['categories'] = heat_cats
        hd['scheduled_participants'] = conn.execute(
            "SELECT p.id, p.number, p.full_name, COALESCE(c.name, 'Без категории') AS category_name, hp.status "
            'FROM participants p JOIN heat_participants hp ON hp.participant_id = p.id '
            'LEFT JOIN categories c ON c.id = p.category_id '
            'WHERE hp.heat_id = ? ORDER BY hp.status, p.number', (h['id'],)).fetchall()
        race_rows = conn.execute(
            "SELECT r.*, p.number, p.full_name, p.birth_year, p.gender, "
            "COALESCE(c.name, 'Без категории') AS category_name "
            'FROM races r JOIN participants p ON p.id = r.participant_id '
            'LEFT JOIN categories c ON c.id = p.category_id WHERE r.heat_id = ? ORDER BY p.number', (h['id'],)).fetchall()

        laps_map = _load_laps_by_race(conn, [r['id'] for r in race_rows])
        hd['races'] = [prepare_race(r, conn, laps_map.get(r['id'], []), include_auto=True) for r in race_rows]

        if h['status'] == 'finished':
            multi_cat = len(hd['categories']) > 1
            hd['races'].sort(key=lambda r: ((r['category_name'] or '',) if multi_cat else ()) + (1 if r['status'] == 'dnf' else (2 if r['status'] == 'dns' else 0), -len(r['laps']), r.get('sort_time', float('inf')), r['number']))
        hd['races_total'] = sum(1 for r in hd['races'] if r['status'] != 'dns')
        hd['races_finished'] = sum(1 for r in hd['races'] if r['status'] in ('ok', 'dnf', 'lapped'))
        if h['started_at']:
            st = datetime.fromisoformat(h['started_at']); hd['started_display'] = st.strftime('%H:%M:%S'); hd['started_dt_local'] = st.strftime('%Y-%m-%dT%H:%M:%S')
        else: hd['started_display'] = None; hd['started_dt_local'] = None
        hd['running_list'] = [{'id': r['id'], 'number': r['number'], 'name': r['full_name'],
                               'laps': len(r['laps']), 'total': r['laps_total']}
                              for r in hd['races'] if r['status'] == 'running']

        hd['is_itt'] = _heat_format(h) == 'itt'
        hd['start_order'] = (hd.get('start_order') or 'number')
        hd['start_at_local'] = (hd.get('start_at') or '')[:16]
        hd['start_at_display'] = _fmt_start_at(hd.get('start_at'))
        hd['interval'] = _itt_interval()
        if hd['is_itt']:
            hd['start_rows'] = _heat_start_rows(conn, h)
            hd['order_manual'] = any(r['manual'] for r in hd['start_rows'])
        heats_data.append(hd)
    return heats_data

@app.route('/races/data')
def races_data():
    """Автообновление списка стартов на странице «Старт»."""
    conn = get_db()
    try:
        comp = _get_competition(conn)
        cid = request.args.get('cid', type=int) or comp['id']
        return render_template('_heats_list.html', heats=_build_races_heats(conn, cid), competition=comp)
    finally:
        conn.close()

@app.route('/heats/<int:hid>/rename', methods=['POST'])
def heat_rename(hid):
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if not h: return 'Не найден', 404
        with conn: conn.execute('UPDATE heats SET name = ? WHERE id = ?', (request.form.get('name', '').strip() or None, hid))
        flash('Название обновлено', 'success'); return redirect(url_for('races_page', cid=h['competition_id']))
    finally: conn.close()

@app.route('/races/create_heat', methods=['POST'])
def heat_create():
    cid = request.form.get('competition_id', type=int); participant_ids = request.form.getlist('participant_ids', type=int)
    finish_on_leader = 1 if request.form.get('finish_on_leader') else 0; auto_enabled = 1 if request.form.get('auto_enabled') else 0
    min_lap_time = request.form.get('min_lap_time', type=float)
    if min_lap_time is None or min_lap_time < 0: min_lap_time = 5.0
    start_order = (request.form.get('start_order') or 'number').strip()
    if start_order not in ('number', 'category'):
        start_order = 'number'
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
            hid = conn.execute('INSERT INTO heats (competition_id, heat_number, status, finish_on_leader, min_lap_time, auto_enabled, format, start_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (cid, new_num, 'pending', finish_on_leader, min_lap_time, auto_enabled, heat_fmt, start_order)).lastrowid
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
        if start_now:
            heat_row = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
            if _start_heat(conn, heat_row) is None:
                flash(f'Старт №{new_num} создан, но запускать некого', 'error')
            else:
                flash(f'Старт №{new_num} создан и запущен: активных {active_count}, DNS {dns_count}', 'success')
        else:
            flash(f'Старт №{new_num}: активных {active_count}, DNS {dns_count}', 'success')
        return redirect(url_for('races_page', cid=cid))
    finally: conn.close()

def _heat_format(heat):
    """Формат старта: 'mass' или 'itt' (у старых стартов поля нет — считаем массовым)."""
    if heat is None:
        return 'mass'
    try:
        value = heat['format']
    except (KeyError, IndexError):
        return 'mass'
    return value if value in RACE_FORMATS else 'mass'


def _start_heat(conn, h):
    """Создаёт заезды старта и переводит его в running.

    Возвращает число активных участников или None, если запускать некого.
    """
    hid, cid = h['id'], h['competition_id']
    participants = conn.execute(
        'SELECT hp.participant_id, hp.status, COALESCE(c.laps_total, 1) AS laps_total FROM heat_participants hp '
        'JOIN participants p ON p.id = hp.participant_id '
        'LEFT JOIN categories c ON c.id = p.category_id '
        'WHERE hp.heat_id = ? AND p.number IS NOT NULL '
        'AND p.id NOT IN (SELECT participant_id FROM races WHERE competition_id = ?)',
        (hid, cid)).fetchall()
    if not participants: return None
    now_iso = datetime.now().isoformat(); active_count = 0
    is_itt = _heat_format(h) == 'itt'
    with conn:
        for p in participants:
            race_status = 'running' if p['status'] == 'active' else 'dns'
            if race_status == 'running': active_count += 1
            # разделка: заезд создаётся в момент старта участника, здесь — только DNS
            if is_itt and race_status == 'running':
                continue
            laps = 1 if is_itt else p['laps_total']
            conn.execute('INSERT INTO races (participant_id, competition_id, heat_id, laps_total, planned_laps, start_time, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
                         (p['participant_id'], cid, hid, laps, laps, now_iso, race_status))
        if active_count == 0:
            conn.execute("UPDATE heats SET status = 'finished', started_at = ?, finished_at = ? WHERE id = ?", (now_iso, now_iso, hid))
        else:
            conn.execute("UPDATE heats SET status = 'running', started_at = ? WHERE id = ?", (now_iso, hid))
    return active_count

@app.route('/heats/<int:hid>/start', methods=['POST'])
def heat_start(hid):
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if not h or h['status'] != 'pending': flash('Старт уже запущен', 'error'); return redirect(url_for('races_page', cid=h['competition_id'] if h else None))
        if _start_heat(conn, h) is None: flash('Нет участников', 'error'); return redirect(url_for('races_page', cid=h['competition_id']))
        flash(f'Старт №{h["heat_number"]} запущен', 'success'); return redirect(url_for('races_page', cid=h['competition_id']))
    finally: conn.close()

@app.route('/heats/<int:hid>/force_finish', methods=['POST'])
def heat_force_finish(hid):
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if h and h['status'] == 'running':
            dnf_ids = request.form.getlist('dnf_race_ids', type=int)
            with conn: _force_finish_heat(conn, hid, dnf_ids)
            msg = f'Старт №{h["heat_number"]} завершён'
            if dnf_ids: msg += f', DNF: {len(dnf_ids)}'
            flash(msg, 'success')
        return redirect(url_for('races_page', cid=h['competition_id']))
    finally: conn.close()

@app.route('/heats/<int:hid>/force_finish_and_next', methods=['POST'])
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

@app.route('/heats/<int:hid>/mark_dnf', methods=['POST'])
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

@app.route('/heats/<int:hid>/delete', methods=['POST'])
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

@app.route('/heats/<int:hid>/edit_start', methods=['POST'])
def heat_edit_start(hid):
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if h:
            try: dt = datetime.fromisoformat(request.form.get('start_time', '').strip())
            except: flash('Неверный формат', 'error'); return redirect(url_for('races_page', cid=h['competition_id']))
            with conn:
                conn.execute('UPDATE heats SET started_at = ? WHERE id = ?', (dt.isoformat(), hid))
                conn.execute("UPDATE races SET start_time = ? WHERE heat_id = ? AND status != 'dns'", (dt.isoformat(), hid))
            flash('Время старта обновлено', 'success')
        return redirect(url_for('races_page', cid=h['competition_id']))
    finally: conn.close()



@app.route('/heats/<int:hid>/order', methods=['POST'])
def heat_order_save(hid):
    """Порядок ожидающих участников разделки (перетаскивание в карточке старта)."""
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if not h:
            return jsonify({'ok': False, 'message': 'Старт не найден'}), 404
        if _heat_format(h) != 'itt':
            return jsonify({'ok': False, 'message': 'Порядок задаётся только в разделке'}), 400
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


@app.route('/heats/<int:hid>/reset_order', methods=['POST'])
def heat_order_reset(hid):
    """Сброс ручного порядка: по номеру или по группе."""
    conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if not h:
            return jsonify({'ok': False, 'message': 'Старт не найден'}), 404
        if _heat_format(h) != 'itt':
            return jsonify({'ok': False, 'message': 'Порядок задаётся только в разделке'}), 400
        mode = (request.form.get('mode') or 'number').strip()
        if mode not in ('number', 'category'):
            mode = 'number'
        with conn:
            conn.execute('UPDATE heat_participants SET sort_order = NULL WHERE heat_id = ?', (hid,))
            conn.execute('UPDATE heats SET start_order = ? WHERE id = ?', (mode, hid))
        return jsonify({'ok': True, 'mode': mode})
    finally:
        conn.close()


@app.route('/heats/<int:hid>/start_at', methods=['POST'])
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


@app.route('/heats/<int:hid>/startlist')
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


@app.route('/heats/<int:hid>/startlist.pdf')
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


@app.route('/heats/<int:hid>/startlist.xlsx')
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
        ws.append([f'Стартовый протокол · старт №{data["heat"]["number"]}'])
        ws['A2'].font = Font(bold=True, size=11)
        meta = []
        if data['heat']['format'] == 'itt':
            meta.append(f'раздельный старт, интервал {data["heat"]["interval"]} с')
            if data['heat']['start_at']:
                meta.append(f'первый старт {data["heat"]["start_at"]}')
        else:
            meta.append('групповой старт')
        meta.append(f'участников {data["total"]}')
        ws.append([' · '.join(meta)])
        ws.append([])
        headers = ['№ п/п', 'Номер', 'ФИО', 'Год', 'Группа', 'Город', 'Команда', 'Время старта', 'Прим.']
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
                r['city'],
                r['team'],
                r['actual_display'] or r['planned_display'] or '',
                note,
            ])
        widths = [7, 8, 32, 7, 20, 16, 18, 13, 14]
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


@app.route('/heats/<int:hid>/startlist_link', methods=['POST'])
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


@app.route('/sl/<token>')
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


@app.route('/laps/<int:lid>/edit', methods=['POST'])
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

@app.route('/laps/<int:lid>/delete', methods=['POST'])
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

@app.route('/races/<int:rid>/edit_finish', methods=['POST'])
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


@app.route('/results')
def results_page():
    conn = get_db()
    try:
        comp = _get_competition(conn)
        return _results_response(conn, comp['id'],
                                 request.args.get('target', '').strip(),
                                 request.args.get('include_auto', '1') == '1')
    finally:
        conn.close()


@app.route('/results/details')
def results_details():
    cid = request.args.get('cid', type=int); target = request.args.get('target', '').strip()
    include_auto = request.args.get('include_auto', '1') == '1'
    if not cid or not target: return '', 400
    conn = get_db()
    try:
        _, _, _, details, _, _, _ = _prepare_results_data(conn, cid, target, include_auto)
        return render_template('_results_details.html', details=details)
    finally: conn.close()

@app.route('/results/export')
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
                headers = ["Место", "№", "ФИО", "Год рожд.", "Пол", "Старт", "Финиш", "Время", "Отставание"]
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

@app.route('/auto')
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

@app.route('/auto/device')
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

@app.route('/auto/log')
def auto_log():
    with _auto_log_lock:
        return render_template('_auto_log.html', auto_log=list(_auto_log))


@app.route('/auto/detail_rows')
def auto_detail_rows():
    hid = request.args.get('hid', type=int)
    if not hid: return '', 400
    conn = get_db()
    try:
        return render_template('_auto_details.html', rows=_get_auto_rows(conn, hid))
    finally: conn.close()

@app.route('/auto/lap_compare')
def auto_lap_compare():
    """Подробное сравнение по кругам — подгружается при раскрытии блока."""
    hid = request.args.get('hid', type=int)
    if not hid: return '', 400
    conn = get_db()
    try:
        return render_template('_auto_compare.html', rows=_get_auto_rows(conn, hid))
    finally: conn.close()

def _save_setting(conn, key, val): set_cached_setting(conn, key, val)

def _auto_redirect():
    """Возврат туда, откуда пришли: на страницу устройства или на общую."""
    cid = request.form.get('cid', type=int)
    if request.form.get('back') == 'device':
        return redirect(url_for('auto_device_page', cid=cid) if cid else url_for('auto_device_page'))
    return redirect(url_for('auto_page', cid=cid) if cid else url_for('auto_page'))

@app.route('/auto/swrfid/settings', methods=['POST'])
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

@app.route('/auto/swrfid/toggle', methods=['POST'])
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

@app.route('/auto/heat_toggle/<int:hid>', methods=['POST'])
def auto_heat_toggle(hid):
    cid = request.form.get('cid', type=int); conn = get_db()
    try:
        h = conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()
        if h:
            with conn: conn.execute('UPDATE heats SET auto_enabled = ? WHERE id = ?', (0 if h['auto_enabled'] else 1, hid))
            flash(f'Автохронометраж старта №{h["heat_number"]} переключён', 'success')
    finally: conn.close()
    return redirect(url_for('auto_page', cid=cid, hid=hid) if cid else url_for('auto_page'))

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

@app.route('/auto/export')
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

@app.route('/manual')
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


@app.route('/manual/start/data')
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


@app.route('/manual/start/action', methods=['POST'])
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
                    "VALUES (?, ?, ?, 1, 1, ?, 'dns')",
                    (nxt['participant_id'], cid, hid, now_iso))
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
                "VALUES (?, ?, ?, 1, 1, ?, 'running')",
                (nxt['participant_id'], cid, hid, now_iso))
            _log_timing(conn, 'manual', 'start', True, competition_id=cid, heat_id=hid,
                        participant_id=nxt['participant_id'], number=nxt['number'],
                        full_name=nxt['full_name'], user_login=judge, detail='Старт (разделка)')
        return jsonify({'ok': True, 'message': f'Старт: №{nxt["number"]} {nxt["full_name"]}',
                        'number': nxt['number'], 'time': now.strftime('%H:%M:%S')})
    finally:
        conn.close()


@app.route('/manual/active')
def manual_active():
    cid = request.args.get('cid', type=int)
    if not cid: return '', 400
    conn = get_db()
    try:
        competition, active_races = _get_manual_data(conn, cid)
        if not competition: return '', 404
        return render_template('_manual_active.html', competition=competition, active_races=active_races, last=session.get('manual_last'), log=session.get('manual_log', []))
    finally: conn.close()

@app.route('/manual/action', methods=['POST'])
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

@app.route('/manual/undo', methods=['POST'])
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

@app.route('/app')
def app_page():
    """Страница скачивания Android-приложения."""
    return render_template('app_page.html')

@app.route('/app.apk')
def app_download():
    """Скачивание Android-приложения."""
    path = os.path.join(app.static_folder, 'chrono.apk')
    if not os.path.exists(path):
        return 'Приложение ещё не собрано', 404
    return send_from_directory(app.static_folder, 'chrono.apk', as_attachment=True,
                               download_name='Хронометраж.apk',
                               mimetype='application/vnd.android.package-archive')

@app.route('/logs')
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

@app.route('/competition/weather/fetch', methods=['POST'])
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

@app.route('/protocol')
def protocol_page():
    conn = get_db()
    try:
        comp = _get_competition(conn)
        return render_template('protocol.html', competition=comp, **_build_protocol(conn, comp['id']))
    finally: conn.close()

@app.route('/competition/protocol-settings', methods=['POST'])
def protocol_settings_save():
    conn = get_db()
    try:
        with conn:
            _save_proto_settings(conn)
        flash('Данные для протокола сохранены', 'success')
        return redirect(url_for('competitions_list'))
    finally: conn.close()

@app.route('/protocol.pdf')
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

def _render_protocol_pdf(comp, data):
    """Собирает PDF протокола по образцу официального документа."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, KeepTogether
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    regular, bold = 'Helvetica', 'Helvetica-Bold'
    app_dir = os.path.dirname(os.path.abspath(__file__))
    font_pairs = [
        (r'C:\Windows\Fonts\arial.ttf', r'C:\Windows\Fonts\arialbd.ttf'),
        ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
         '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
        ('/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
         '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf'),
        (os.path.join(app_dir, 'static', 'fonts', 'DejaVuSans.ttf'),
         os.path.join(app_dir, 'static', 'fonts', 'DejaVuSans-Bold.ttf')),
    ]
    for reg_path, bold_path in font_pairs:
        if not os.path.exists(reg_path):
            continue
        try:
            pdfmetrics.registerFont(TTFont('ProtoFont', reg_path))
            regular = 'ProtoFont'
            if os.path.exists(bold_path):
                pdfmetrics.registerFont(TTFont('ProtoFontBd', bold_path))
                bold = 'ProtoFontBd'
            break
        except Exception:
            _app_logger.exception('Protocol PDF: не удалось загрузить шрифт %s', reg_path)
    if regular == 'Helvetica':
        _app_logger.warning('Protocol PDF: шрифт с кириллицей не найден — текст может не отображаться')

    def esc(v):
        return (str(v or '')).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    def st(size, font=regular, align=TA_LEFT, after=0, before=0, leading=None):
        return ParagraphStyle(f's{size}{font}{align}{after}', fontName=font, fontSize=size,
                              leading=leading or size * 1.3, alignment=align,
                              spaceAfter=after, spaceBefore=before)

    s = data['settings']
    totals = data['totals']
    cell = st(8, leading=9.5)
    cell_c = st(8, align=TA_CENTER, leading=9.5)
    cell_b = st(8, font=bold, leading=9.5)
    head_c = st(8, font=bold, align=TA_CENTER, leading=9.5)

    W = A4[0] - 24 * mm  # доступная ширина
    is_itt = data.get('format') == 'itt'
    # ширины подобраны по метрикам DejaVu 8pt: время «16:51:53» ≈ 14,1 мм с отступами,
    # «Отставание» ≈ 18,8 мм, «1:05:21,21» ≈ 16,8 мм — слова не должны рваться
    col_w = [14, 9, 52, 15, 19, 22, 19, 23, 13]  # мм: масса
    if is_itt:
        col_w = [13, 9, 30, 15, 18, 18, 15, 15, 19, 21, 13]  # + Старт, Финиш
    # масштабируем под доступную ширину
    total_mm = sum(col_w)
    col_w = [w * W / total_mm for w in col_w]

    def race_table(rows):
        names = ['Место', '№', 'ФИО', 'Год<br/>рожд.', 'Город', 'Команда']
        if is_itt:
            names += ['Старт', 'Финиш']
        names += ['Общее<br/>время', 'Отставание', 'V.ср<br/>км/ч']
        header = [Paragraph(h, head_c) for h in names]
        body = []
        for r in rows:
            race = r.get('race', r)  # абсолют передаёт {'race': ...}
            cells = [
                Paragraph(str(race.get('place', '')), cell_c),
                Paragraph(str(race.get('number', '')), cell_c),
                Paragraph(esc(race.get('full_name')), cell),
                Paragraph(str(race.get('birth_year') or ''), cell_c),
                Paragraph(esc(race.get('city')), cell),
                Paragraph(esc(race.get('team')), cell),
            ]
            if is_itt:
                cells += [
                    Paragraph(str(race.get('start_display') or ''), cell_c),
                    Paragraph(str(race.get('finish_display') or ''), cell_c),
                ]
            cells += [
                Paragraph(str(race.get('time_display') or 'н/ф'), cell_c),
                Paragraph(str(race.get('gap_display') or ''), cell_c),
                Paragraph(str(race.get('speed_display') or ''), cell_c),
            ]
            body.append(cells)
        t = Table([header] + body, colWidths=col_w, repeatRows=1)
        t.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BACKGROUND', (0, 0), (-1, 0), colors.Color(0.94, 0.94, 0.94)),
            ('LEFTPADDING', (0, 0), (-1, -1), 2),
            ('RIGHTPADDING', (0, 0), (-1, -1), 2),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ]))
        return t

    def kv_rows(pairs, width, bold_labels=False):
        data = [[Paragraph(esc(lbl), st(8, font=(bold if bold_labels else regular))),
                 Paragraph(esc(val), st(8, align=TA_RIGHT))] for lbl, val in pairs]
        t = Table(data, colWidths=[width * 0.60, width * 0.40])
        t.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 2), ('RIGHTPADDING', (0, 0), (-1, -1), 2),
            ('TOPPADDING', (0, 0), (-1, -1), 1), ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
        ]))
        return t

    def head_block(track_name='', track_reg='', hd='', ms='', ts='', lap_length='', laps='', route=''):
        place = Paragraph(f'<b>Место проведения:</b> {esc(s["proto_venue"] or "—")}', st(8))
        dt = Paragraph(f'Дата гонки: {esc(data["date_display"] or "—")}      '
                       f'Время старта: {esc(data["start_display"] or "—")}', st(8, align=TA_RIGHT))
        top = Table([[place, dt]], colWidths=[W * 0.6, W * 0.4])
        top.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
                                 ('LEFTPADDING', (0, 0), (-1, -1), 0),
                                 ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                                 ('TOPPADDING', (0, 0), (-1, -1), 0),
                                 ('BOTTOMPADDING', (0, 0), (-1, -1), 0)]))

        inner_w = W / 2 - 8
        jury = kv_rows([
            ('Главный судья:', s['proto_chief_judge'] or ''),
            ('Судья на старте:', s['proto_start_judge'] or ''),
            ('Судья на финише:', s['proto_finish_judge'] or ''),
            ('Главный секретарь:', s['proto_chief_secretary'] or ''),
        ], inner_w)
        track = esc(track_name or '') + (f' / {esc(track_reg)}' if track_reg else '')
        tech = kv_rows([
            ('НАЗВАНИЕ ТРАССЫ / РЕГ. НОМЕР:', track),
            ('МАКСИМАЛЬНЫЙ ПЕРЕПАД (HD):', hd or ''),
            ('МАКСИМАЛЬНЫЙ ПОДЪЕМ (МС):', ms or ''),
            ('СУММА ПЕРЕПАДОВ (ТС):', ts or ''),
            ('ДЛИНА КРУГА, км:', lap_length or ''),
            ('КРУГОВ:', str(laps) if laps not in (None, '') else ''),
            ('ПРОТЯЖЕННОСТЬ МАРШРУТА, км:', route or ''),
        ], inner_w, bold_labels=True)

        head2 = Table([[Paragraph('ИНФОРМАЦИЯ О ЖЮРИ И ГСК СОРЕВНОВАНИЙ:', head_c),
                        Paragraph('ТЕХНИЧЕСКИЕ ДАННЫЕ ТРАССЫ:', head_c)],
                       [jury, tech]], colWidths=[W / 2, W / 2])
        head2.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ('BACKGROUND', (0, 0), (-1, 0), colors.Color(0.9, 0.9, 0.9)),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 3), ('RIGHTPADDING', (0, 0), (-1, -1), 3),
            ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ]))
        return [top, head2, Spacer(1, 4)]

    story = []

    # ---- титул ----
    for line in (s['proto_title'] or '').splitlines():
        if line.strip():
            story.append(Paragraph(esc(line.strip()), st(11, font=bold, align=TA_CENTER, after=1)))
    story.append(Paragraph('ОФИЦИАЛЬНЫЙ ПРОТОКОЛ РЕЗУЛЬТАТОВ', st(13, font=bold, align=TA_CENTER, before=6, after=6)))

    def stats_footer(b):
        """Таблица «Погодные условия / Статистика гонки» под результатами категории."""
        from reportlab.pdfbase.pdfmetrics import stringWidth
        head = ['ПОГОДНЫЕ УСЛОВИЯ', '', '', '', '', 'СТАТИСТИКА ГОНКИ', '', '', '', '', '']
        sub = ['Погода в начале', 'Погода в конце', 'Т в начале', 'Т в конце', 'Влажность',
               'Заяв.', 'Старт.', 'Не старт.', 'Финиш.', 'Не финиш.', 'ДСК']
        vals = [s['proto_weather_start'] or '', s['proto_weather_end'] or '',
                s['proto_temp_start'] or '', s['proto_temp_end'] or '', s['proto_humidity'] or '',
                str(b['stats']['zayav']), str(b['stats']['start']), str(b['stats']['dns']),
                str(b['stats']['finish']), str(b['stats']['dnf']), '0']
        # ширины по фактической длине текста — без разрывов слов
        widths = [max(stringWidth(sub[i], bold, 7.5), stringWidth(str(vals[i]), regular, 8)) + 7
                  for i in range(11)]
        total = sum(widths)
        widths = [w * W / total for w in widths]
        sub_hdr = st(7.5, font=bold, align=TA_CENTER)
        data = [[Paragraph(esc(x), head_c) for x in head],
                [Paragraph(esc(x), sub_hdr) for x in sub],
                [Paragraph(esc(x), cell_c) for x in vals]]
        t = Table(data, colWidths=widths)
        t.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ('SPAN', (0, 0), (4, 0)), ('SPAN', (5, 0), (10, 0)),
            ('BACKGROUND', (0, 0), (-1, 1), colors.Color(0.9, 0.9, 0.9)),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 2), ('RIGHTPADDING', (0, 0), (-1, -1), 2),
            ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ]))
        return t

    def sign_block():
        data = [
            [Paragraph('СУДЬЯ НА СТАРТЕ', head_c), Paragraph('ГЛАВНЫЙ СУДЬЯ', head_c),
             Paragraph('ГЛАВНЫЙ СЕКРЕТАРЬ', head_c)],
            [Paragraph(esc(s['proto_start_judge'] or ''), st(9, align=TA_RIGHT)),
             Paragraph(esc(s['proto_chief_judge'] or ''), st(9, align=TA_RIGHT)),
             Paragraph(esc(s['proto_chief_secretary'] or ''), st(9, align=TA_RIGHT))],
        ]
        t = Table(data, colWidths=[W * 0.26, W * 0.48, W * 0.26], rowHeights=[None, 60])
        t.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ('BACKGROUND', (0, 0), (-1, 0), colors.Color(0.9, 0.9, 0.9)),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 3), ('RIGHTPADDING', (0, 0), (-1, -1), 3),
            ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        return t

    # ---- категории: каждая с новой страницы, своя шапка и свой подвал ----
    blocks = [b for b in data['blocks'] if b['races']]
    for i, b in enumerate(blocks):
        if i:
            story.append(PageBreak())
        cat = b['category']
        story.append(KeepTogether(
            head_block(s['proto_track_name'], s['proto_track_reg'],
                       cat['hd'], cat['ms'], cat['ts'],
                       cat['lap_length'], cat['laps_total'], b['route_display'])
            + [Paragraph(esc(cat['name']), st(11, font=bold, before=2, after=3))]))
        story.append(race_table(b['races']))
        story.append(Spacer(1, 4))
        story.append(stats_footer(b))
        story.append(Spacer(1, 8))
        story.append(sign_block())

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm,
                      topMargin=12 * mm, bottomMargin=14 * mm,
                      title='Официальный протокол результатов').build(story)
    return buf.getvalue()

@app.route('/summary')
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

@app.route('/health')
def health_page():
    return redirect(url_for('summary_page'))

def _build_heat_view(conn, h):
    """Готовит заезды старта: разбивка по категориям, отставание внутри группы, счётчики.

    Используется и страницей «Сейчас идёт», и публичным табло.
    """
    hd = dict(h)
    races = conn.execute('SELECT r.*, p.number, p.full_name, p.category_id, c.name AS category_name '
                         'FROM races r JOIN participants p ON p.id = r.participant_id '
                         'LEFT JOIN categories c ON c.id = p.category_id '
                         'WHERE r.heat_id = ? ORDER BY p.number', (h['id'],)).fetchall()

    race_ids = [r['id'] for r in races]
    laps_map = _load_laps_by_race(conn, race_ids)
    auto_map = _load_auto_laps_by_race(conn, race_ids)
    min_lap = h['min_lap_time'] or 0
    prepared = []
    for r in races:
        if r['status'] == 'dns': continue
        pr = prepare_race(r, conn, laps_map.get(r['id'], []), auto_map.get(r['id'], []), min_lap, include_auto=True)
        pr['laps_count'] = len(pr['laps'])
        prepared.append(pr)

    # группируем по категориям: отставание считаем внутри своей группы
    groups = {}
    for r in prepared:
        key = r.get('category_id') or 0
        if key not in groups:
            groups[key] = {'id': key, 'name': r.get('category_name') or 'Без категории', 'races': []}
        groups[key]['races'].append(r)

    for g in groups.values():
        _assign_category_gaps(g['races'])
        counts = _count_races(g['races'])
        g.update(total=counts['total'], running_count=counts['running'],
                 finished_count=counts['ok'], dnf_count=counts['dnf'], lapped_count=counts['lapped'])

    hd['races'] = prepared
    counts = _count_races(prepared)
    hd.update(total=counts['total'], running_count=counts['running'],
              finished_count=counts['ok'], dnf_count=counts['dnf'], lapped_count=counts['lapped'])
    hd['categories'] = sorted(groups.values(), key=lambda x: (x['name'] or '').lower())
    hd['running_list'] = [{'id': r['id'], 'number': r['number'], 'name': r['full_name'],
                           'laps': len(r['laps']), 'total': r['laps_total']}
                          for r in prepared if r['status'] == 'running']
    return hd

def _build_dashboard_heats(conn, cid):
    """Идущие старты с подготовленными заездами и счётчиками для дашборда."""
    return [_build_heat_view(conn, h)
            for h in conn.execute("SELECT * FROM heats WHERE competition_id = ? AND status = 'running' "
                                  "ORDER BY heat_number", (cid,)).fetchall()]

@app.route('/dashboard')
def dashboard_page():
    conn = get_db()
    try:
        competition = _get_competition(conn)
        cid = competition['id']
        heats_data = _build_dashboard_heats(conn, cid)
        return render_template('dashboard.html', competition=competition, heats=heats_data)
    finally: conn.close()

@app.route('/dashboard/data')
def dashboard_data():
    cid = request.args.get('cid', type=int)
    if not cid: return '', 400
    conn = get_db()
    try:
        if not conn.execute('SELECT id FROM competitions WHERE id = ?', (cid,)).fetchone(): return '', 404
        return render_template('_dashboard_heats.html', heats=_build_dashboard_heats(conn, cid))
    finally: conn.close()

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


@app.route('/finish-screen')
def finish_screen():
    conn = get_db()
    try:
        return _finish_screen_response(conn)
    finally: conn.close()


@app.route('/finish-screen/data')
def finish_screen_data():
    conn = get_db()
    try:
        cid = request.args.get('cid', type=int)
        if not cid:
            cid = _get_competition(conn)['id']
        return _finish_screen_data_response(conn, cid)
    finally: conn.close()


# ======================== ПУБЛИЧНЫЙ ХАБ ========================

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


@app.route('/p/<slug>')
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


@app.route('/p/<slug>/results')
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


@app.route('/p/<slug>/live')
def public_live(slug):
    entry = _public_competition(slug)
    if not entry:
        return 'Соревнование не найдено', 404
    conn = _connect(_comp_db_path(entry))
    try:
        return _finish_screen_response(conn, url_for('public_live_data', slug=slug))
    finally:
        conn.close()


@app.route('/p/<slug>/live/data')
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


@app.route('/p/<slug>/sl/<int:hid>')
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

@app.route('/sw.js')
def service_worker():
    response = send_from_directory('static', 'sw.js', mimetype='application/javascript')
    response.headers['Service-Worker-Allowed'] = '/'
    return response

if __name__ == '__main__':
    init_db()
    _ensure_registry()
    _migrate_competitions()
    _people_auto_refresh()
    start_swrfid_listener()

    CERT_PATH = os.path.join(CERT_DIR, 'shumilova.cedor.ru-crt.pem')
    KEY_PATH = os.path.join(CERT_DIR, 'shumilova.cedor.ru-key.pem')

    # Порты можно задать переменными окружения (за прокси TLS терминируется снаружи)
    HTTP_PORT = int(os.environ.get('CHRONO_HTTP_PORT', '5000'))
    HTTPS_PORT = int(os.environ.get('CHRONO_HTTPS_PORT', '5001'))
    HTTPS_ENABLED = os.environ.get('CHRONO_HTTPS', '1') == '1'

    print('=' * 60, flush=True)
    print(f'Сервер запускается на HTTP:  http://0.0.0.0:{HTTP_PORT}', flush=True)

    ssl_ctx = None
    if HTTPS_ENABLED and os.path.exists(CERT_PATH) and os.path.exists(KEY_PATH):
        ssl_ctx = (CERT_PATH, KEY_PATH)
        print(f'Сервер запускается на HTTPS: https://0.0.0.0:{HTTPS_PORT}', flush=True)
        print('🔒 Поддержка HTTPS (SSL) включена', flush=True)
    elif not HTTPS_ENABLED:
        print('HTTPS внутри приложения отключён (CHRONO_HTTPS=0)', flush=True)
    else:
        print('⚠ ВНИМАНИЕ: Файлы сертификатов не найдены. HTTPS недоступен.', flush=True)
    print('=' * 60, flush=True)

    from werkzeug.serving import make_server

    app.debug = True

    servers = []
    
    # HTTP Server
    http_server = make_server('0.0.0.0', HTTP_PORT, app, threaded=True)
    servers.append(http_server)
    
    # HTTPS Server
    if ssl_ctx:
        https_server = make_server('0.0.0.0', HTTPS_PORT, app, threaded=True, ssl_context=ssl_ctx)
        servers.append(https_server)

    server_threads = []
    for srv in servers:
        t = threading.Thread(target=srv.serve_forever)
        t.daemon = True
        t.start()
        server_threads.append(t)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nОстановка серверов...", flush=True)
        for srv in servers:
            srv.shutdown()