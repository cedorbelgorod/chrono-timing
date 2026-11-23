# -*- coding: utf-8 -*-
"""Саморегистрация: настройки формы, шаги визарда, правила и создание участника."""
from datetime import datetime
import json, re, threading, time
from flask import render_template, request, session
from chrono.catalog import (_auto_category, _category_matches)
from chrono.db import (_comp_db_path, _connect, _get_competition, _registry_find)
from chrono.utils import (_parse_year)

REG_NUMBER_BASE = 999000

def _next_reg_number(conn, cid):
    return max(REG_NUMBER_BASE, conn.execute('SELECT COALESCE(MAX(number), 0) FROM participants WHERE competition_id = ?', (cid,)).fetchone()[0] + 1)

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

def _public_participants(conn, cid):
    """Список участников для публичной страницы: без телефона и прочих контактов."""
    return conn.execute(
        'SELECT p.number, p.full_name, p.birth_year, p.gender, p.city, p.team, '
        '       c.name AS category_name '
        'FROM participants p LEFT JOIN categories c ON c.id = p.category_id '
        'WHERE p.competition_id = ? '
        'ORDER BY COALESCE(p.number, 0), p.full_name', (cid,)).fetchall()

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
