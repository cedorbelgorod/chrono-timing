# -*- coding: utf-8 -*-
"""Маршруты саморегистрации участников."""

import io
import json

from flask import flash, redirect, render_template, request, send_file, session, url_for
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from chrono.db import (_get_competition, _registry_find, get_db)
from chrono.settings import (set_cached_setting)
from chrono.utils import (_fmt_date, _qr_svg)
from chrono.auth import (_is_read_only)
from chrono.catalog import (_auto_category, _cat_genders)
from chrono.logging_setup import (_app_logger)
from chrono.registration_helpers import (
    DEFAULT_CONSENT, REG_FIELDS, REG_WIZARD_ORDER, _public_participants, _public_reg_context,
    _reg_allowed_categories, _reg_clear_draft, _reg_closed, _reg_config, _reg_create_participant,
    _reg_draft, _reg_rate_ok, _reg_save_draft, _reg_status, _reg_step_validate, _reg_steps,
)


def _setting(conn, key, default=''):
    """Значение настройки из базы соревнования (без кэша по сессии)."""
    row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    return row['value'] if row and row['value'] is not None else default


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


def reg_list(slug):
    """Публичный список зарегистрированных участников (без телефонов)."""
    entry, conn, comp = _public_reg_context(slug)
    if not entry:
        return _reg_closed(slug, 'Ссылка недействительна: соревнование не найдено.', 404)
    try:
        rows = _public_participants(conn, comp['id'])
        return render_template('public_reg_list.html', comp=comp, rows=rows, slug=slug,
                               total=len(rows),
                               start_date=_fmt_date(_setting(conn, 'start_date')))
    finally:
        conn.close()


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
        return render_template('public_reg.html', comp=comp, cfg=cfg, ok=ok, why=why, slug=slug,
                               start_date=_fmt_date(_setting(conn, 'start_date')))
    finally:
        conn.close()


def reg_form(slug):
    """Старая ссылка на форму — начинаем мастер с первого шага."""
    _reg_clear_draft(slug)
    return redirect(url_for('reg_step', slug=slug, step=1))


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
                               summary=summary, consent_text=cfg['consent'],
                               start_date=_fmt_date(_setting(conn, 'start_date')))
    finally:
        conn.close()


def reg_done(slug):
    entry = _registry_find(slug)
    if not entry:
        return _reg_closed(slug, 'Ссылка недействительна: соревнование не найдено.', 404)
    number = request.args.get('n', type=int)
    cat = (request.args.get('cat') or '').strip()
    return render_template('public_reg_done.html', slug=slug, pid=number,
                           name=entry.get('name'), category=cat)


RULES = [
    ('/registration', 'registration_page', registration_page, ['GET']),
    ('/registrations', 'registrations_page', registrations_page, ['GET']),
    ('/registrations.xlsx', 'registrations_xlsx', registrations_xlsx, ['GET']),
    ('/registration/save', 'registration_save', registration_save, ['POST']),
    ('/registration/autocat', 'registration_autocat', registration_autocat, ['POST']),
    ('/reg/<slug>/list', 'reg_list', reg_list, ['GET']),
    ('/reg/<slug>/list.xlsx', 'reg_list_xlsx', reg_list_xlsx, ['GET']),
    ('/reg/<slug>', 'reg_intro', reg_intro, ['GET']),
    ('/reg/<slug>/form', 'reg_form', reg_form, ['GET', 'POST']),
    ('/reg/<slug>/step/<int:step>', 'reg_step', reg_step, ['GET', 'POST']),
    ('/reg/<slug>/done', 'reg_done', reg_done, ['GET']),
]
