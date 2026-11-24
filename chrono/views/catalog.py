# -*- coding: utf-8 -*-
"""Маршруты участников, категорий, номеров и меток."""

import io
import re
import sqlite3
from datetime import datetime

from flask import flash, jsonify, redirect, render_template, request, send_file, session, url_for
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

from chrono.db import (_get_competition, _registry_find, get_db)
from chrono.settings import (
    PROTOCOL_DEFAULTS, RACE_FORMATS, _comp_format, _competition_start_year,
    _reload_settings_cache_from_db, get_cached_setting,
)
from chrono.timing import (_heat_participant_counts)
from chrono.utils import (_age_range_hint, _age_range_years, _format_age_range,
                          _normalize_gender, _parse_age_range, _parse_year)
from chrono.catalog import (
    CATEGORY_HEADER_ALIASES, HEADER_ALIASES, MAX_TAGS_PER_NUMBER, PARTICIPANT_SORTS,
    TAGS_HEADER_ALIASES, _apply_age_range, _auto_category, _category_age_hint,
    _category_mismatch, _category_overlaps, _cell_text, _find_header_col,
    _invalidate_competition_meta, _issue_number, _join_genders, _next_number, _norm_header,
    _norm_name, _parse_gender_cell, _participants_redirect, _plabel, _save_bib_tags, _scalar,
    _tags_by_number,
)
from chrono.logging_setup import (_app_logger)
from chrono.registration_helpers import (_next_reg_number)
from chrono.results_helpers import (_base_url)


def competitions_list():
    """Дашборд подготовки соревнования: настройки, чеклист готовности, ссылки."""
    conn = get_db()
    try:
        comp = _get_competition(conn)
        cid = comp['id']
        participants = conn.execute(
            'SELECT COUNT(*) FROM participants WHERE competition_id = ?', (cid,)).fetchone()[0]
        stats = {
            'categories': conn.execute('SELECT COUNT(*) FROM categories WHERE competition_id = ?', (cid,)).fetchone()[0],
            'participants': participants,
            'numbers': conn.execute(
                'SELECT COUNT(*) FROM participants WHERE competition_id = ? AND number IS NOT NULL',
                (cid,)).fetchone()[0],
            'tagged': conn.execute(
                "SELECT COUNT(*) FROM participants p WHERE p.competition_id = ? AND p.number IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM bib_tags bt WHERE bt.competition_id = p.competition_id "
                "            AND bt.number = p.number)", (cid,)).fetchone()[0],
            'heats': conn.execute('SELECT COUNT(*) FROM heats WHERE competition_id = ?', (cid,)).fetchone()[0],
        }
        settings = {k: get_cached_setting(k, '') for k in PROTOCOL_DEFAULTS}
        settings['itt_start_interval'] = get_cached_setting('itt_start_interval', '30')
        settings['itt_countdown_sound'] = get_cached_setting('itt_countdown_sound', '1')
        settings['start_date'] = get_cached_setting('start_date', '')
        protocol_filled = bool((settings.get('proto_title') or '').strip())
        return render_template('competitions.html', competition=comp, stats=stats,
                               settings=settings, race_format=_comp_format(comp),
                               formats=RACE_FORMATS, protocol_filled=protocol_filled)
    finally: conn.close()


def competition_format_save():
    """Параметры разделки (интервал, звук). Тип старта задаётся при создании."""
    conn = get_db()
    try:
        _get_competition(conn)
        interval = request.form.get('itt_start_interval', type=int)
        if interval is None:
            try:
                interval = int(float(get_cached_setting('itt_start_interval', '30') or 30))
            except (TypeError, ValueError):
                interval = 30
        interval = max(5, min(600, interval))
        sound = '1' if request.form.get('itt_countdown_sound') else '0'
        with conn:
            conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                         ('itt_start_interval', str(interval)))
            conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                         ('itt_countdown_sound', sound))
        _reload_settings_cache_from_db(conn)
        _invalidate_competition_meta()
        _app_logger.info('ITT settings: interval=%s sound=%s', interval, sound)
        flash(f'Параметры разделки сохранены: интервал {interval} с', 'success')
    finally:
        conn.close()
    return redirect(url_for('competitions_list'))


def competition_start_date_save():
    """Плановая дата старта. При смене года — вопрос о пересчёте категорий."""
    conn = get_db()
    try:
        comp = _get_competition(conn)
        cid = comp['id']
        raw = (request.form.get('start_date') or '').strip()
        if raw:
            try:
                normalized = datetime.strptime(raw, '%Y-%m-%d').strftime('%Y-%m-%d')
            except ValueError:
                flash('Дата должна быть в формате ГГГГ-ММ-ДД', 'error')
                return redirect(url_for('competitions_list'))
        else:
            normalized = ''
        old = (get_cached_setting('start_date', '') or '').strip()
        old_year = int(old[:4]) if old[:4].isdigit() else None
        new_year = int(normalized[:4]) if normalized[:4].isdigit() else None
        year_changed = old_year != new_year
        confirm = request.form.get('confirm') == '1'
        participants = conn.execute(
            'SELECT COUNT(*) FROM participants WHERE competition_id = ?', (cid,)).fetchone()[0]
        if year_changed and participants and not confirm:
            return render_template('start_date_confirm.html', competition=comp,
                                   start_date=normalized, old_year=old_year,
                                   new_year=new_year, participants=participants)
        with conn:
            conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                         ('start_date', normalized))
            _reload_settings_cache_from_db(conn)
            recalc = 0
            if new_year:
                _apply_age_range(conn, cid, new_year)
                if confirm:
                    for p in conn.execute(
                            'SELECT id, gender, birth_year, category_id FROM participants '
                            'WHERE competition_id = ?', (cid,)).fetchall():
                        cat = _auto_category(conn, cid, p['gender'], p['birth_year'])
                        if cat and cat != p['category_id']:
                            conn.execute('UPDATE participants SET category_id = ? WHERE id = ?',
                                         (cat, p['id']))
                            recalc += 1
        if confirm and recalc:
            flash(f'Дата старта сохранена, категории пересчитаны: {recalc}', 'success')
        else:
            flash('Дата старта сохранена', 'success')
        return redirect(url_for('competitions_list'))
    finally:
        conn.close()


def competition_edit(cid):
    name = request.form.get('name', '').strip()
    if not name: flash('Название пустое', 'error'); return redirect(url_for('competitions_list'))
    conn = get_db()
    try:
        with conn: conn.execute('UPDATE competitions SET name = ? WHERE id = ?', (name, cid))
        _invalidate_competition_meta()
        flash('Название сохранено', 'success'); return redirect(url_for('competitions_list'))
    finally: conn.close()


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


def categories_list(cid):
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp: return 'Не найдено', 404
        start_year = _competition_start_year()
        form = {'name': '', 'laps_total': '1', 'gender': [], 'age_range': ''}
        form_error = None
        error_field = None
        if request.method == 'POST':
            form = {
                'name': (request.form.get('name') or '').strip(),
                'laps_total': (request.form.get('laps_total') or '').strip() or '1',
                'gender': request.form.getlist('gender'),
                'age_range': (request.form.get('age_range') or '').strip(),
            }
            name = form['name']
            try:
                laps = int(form['laps_total'])
            except ValueError:
                laps = 0
            gender = _join_genders(form['gender'])
            age_min, age_max, age_err = _parse_age_range(form['age_range'])
            if not name:
                form_error, error_field = 'Введите название категории', 'name'
            elif laps < 1:
                form_error, error_field = 'Кругов должно быть не меньше 1', 'laps_total'
            elif age_err:
                form_error, error_field = age_err, 'age_range'
            else:
                year_from, year_to = _age_range_years(start_year, age_min, age_max)
                try:
                    with conn:
                        cur = conn.execute(
                            'INSERT INTO categories (competition_id, name, laps_total, gender, age_range, '
                            'year_from, year_to) VALUES (?, ?, ?, ?, ?, ?, ?)',
                            (cid, name, laps, gender, form['age_range'], year_from, year_to))
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
                               start_year=start_year,
                               age_hints={c['id']: _category_age_hint(c, start_year) for c in categories},
                               form=form, form_error=form_error, error_field=error_field,
                               submitted={}, row_errors={})
    finally: conn.close()


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
    ws.append(['Название', 'Кругов', 'Пол', 'Возраст'])
    ws.append(['Мужчины 18+', 5, 'М', '18-'])
    ws.append(['Женщины 18+', 4, 'Ж', '18-'])
    ws.append(['Мальчики 8 лет и младше', 2, 'М', '-8'])
    ws.append(['Юноши 12–14', 3, 'М', '12-14'])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for i, w in enumerate([32, 10, 8, 14], start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = 'A2'
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='kategorii-shablon.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


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
        start_year = _competition_start_year()

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
            age_raw = _cell_text(cell(cols['age'])) if cols.get('age') is not None else ''
            age_min, age_max, age_err = _parse_age_range(age_raw)
            if not age_raw and cols.get('age') is None:
                # старый формат: годы рождения → возраст
                y1 = _parse_year(cell(cols['year_from']))
                y2 = _parse_year(cell(cols['year_to']))
                if y1 is not None or y2 is not None:
                    age_min = (start_year - y2) if y2 is not None else None
                    age_max = (start_year - y1) if y1 is not None else None
                    if age_min is not None:
                        age_min = max(0, age_min)
                    if age_max is not None:
                        age_max = max(0, age_max)
            age_range = _format_age_range(age_min, age_max)

            status, message = 'new', ''
            key = _norm_name(name)
            if key in existing:
                status, message = 'dup', f'Уже есть: «{existing[key]}»'
            elif key in seen:
                status, message = 'dup', f'Повтор строки {seen[key]}'
            elif laps < 1:
                status, message = 'error', 'Кругов должно быть не меньше 1'
            elif age_err:
                status, message = 'error', age_err
            else:
                seen[key] = i
            if status == 'new':
                ok += 1
            year_from, year_to = _age_range_years(start_year, age_min, age_max)
            parsed.append({'row': i, 'name': name, 'laps': laps or 1, 'gender': gender,
                           'age_range': age_range, 'year_from': year_from, 'year_to': year_to,
                           'status': status, 'message': message})

        if not parsed:
            flash('В файле нет строк с категориями', 'error')
            return redirect(url_for('categories_list', cid=cid))
        return render_template('categories_import.html', competition=comp, rows=parsed, ok=ok)
    finally:
        conn.close()


def categories_import_confirm(cid):
    """Записывает проверенные категории из предпросмотра."""
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp: return 'Не найдено', 404
        start_year = _competition_start_year()
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
                age_range = (request.form.get(prefix + 'age_range') or '').strip()
                age_min, age_max, _age_err = _parse_age_range(age_range)
                year_from, year_to = _age_range_years(start_year, age_min, age_max)
                try:
                    cur = conn.execute(
                        'INSERT INTO categories (competition_id, name, laps_total, gender, age_range, '
                        'year_from, year_to) VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (cid, name, max(1, laps), gender, age_range, year_from, year_to))
                    conn.execute('UPDATE categories SET sort_order = id WHERE id = ?', (cur.lastrowid,))
                    added += 1
                except sqlite3.IntegrityError:
                    skipped += 1
        flash(f'Импорт категорий: добавлено {added}' + (f', пропущено {skipped}' if skipped else ''), 'success')
        return redirect(url_for('categories_list', cid=cid))
    finally:
        conn.close()


def categories_save_all(cid):
    conn = get_db()
    try:
        comp = conn.execute('SELECT * FROM competitions WHERE id = ?', (cid,)).fetchone()
        if not comp: return 'Не найдено', 404
        start_year = _competition_start_year()
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
                    'age_range': (request.form.get(f'cat_{cat_id}_age_range') or '').strip(),
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
                age_min, age_max, age_err = _parse_age_range(raw['age_range'])
                year_from, year_to = _age_range_years(start_year, age_min, age_max)
                if (name == cat['name'] and sort_order == (cat['sort_order'] if cat['sort_order'] is not None else cat['id'])
                        and laps == cat['laps_total'] and lap_length == cat['lap_length']
                        and hd == cat['hd'] and ms == cat['ms'] and ts == cat['ts']
                        and gender == cat['gender'] and raw['age_range'] == (cat['age_range'] or '')
                        and year_from == cat['year_from'] and year_to == cat['year_to']): continue

                def _fail(msg, field):
                    errors.append(msg)
                    row_errors[cat_id] = (msg, field)

                if not name:
                    _fail(f'«{cat["name"]}»: пустое название', 'name'); continue
                if laps is None or laps < 1:
                    _fail(f'«{name}»: кругов должно быть не меньше 1', 'laps'); continue
                if age_err:
                    _fail(f'«{name}»: {age_err.lower()}', 'age_range'); continue
                try:
                    conn.execute('UPDATE categories SET name = ?, laps_total = ?, lap_length = ?, '
                                 'hd = ?, ms = ?, ts = ?, gender = ?, age_range = ?, '
                                 'year_from = ?, year_to = ?, sort_order = ? WHERE id = ?',
                                 (name, laps, lap_length, hd, ms, ts, gender, raw['age_range'],
                                  year_from, year_to, sort_order, cat_id))
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
                                   start_year=start_year,
                                   age_hints={c['id']: _category_age_hint(c, start_year) for c in categories},
                                   form={'name': '', 'laps_total': '1', 'gender': [], 'age_range': ''},
                                   form_error=None, error_field=None,
                                   submitted=submitted, row_errors=row_errors)
        if updated: flash(f'Сохранено: {updated}', 'success')
        if not updated and not errors: flash('Изменений нет', 'success')
        return redirect(url_for('categories_list', cid=cid))
    finally: conn.close()


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
        cats_by_id = {c['id']: c for c in categories}
        mismatch_ids = {p['id'] for p in parts
                        if p['category_id'] in cats_by_id
                        and _category_mismatch(cats_by_id[p['category_id']], p['gender'], p['birth_year'])}
        return render_template('participants.html', competition=comp, participants=parts,
                               categories=categories, q=q, total=total_all, shown=len(parts),
                               filters=filters, sort_urls=sort_urls,
                               has_none_category=has_none_category,
                               mismatch_ids=mismatch_ids,
                               tags_by_number=_tags_by_number(conn, cid),
                               qs=request.query_string.decode('utf-8', 'ignore'),
                               next_default=_next_reg_number(conn, cid))
    finally: conn.close()


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


def participants_template(cid):
    wb = Workbook(); ws = wb.active; ws.title = 'Участники'
    ws.append(['ФИО', 'Год рождения', 'Категория', 'Пол', 'RFID-метка', 'Город', 'Команда'])
    ws.append(['Иванов Иван Иванович', 1990, 'Мужчины 18+', 'М', '', 'Белгород', 'СШ 4'])
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='participants_template.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


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


RULES = [
    ('/competitions', 'competitions_list', competitions_list, ['GET']),
    ('/competition/format', 'competition_format_save', competition_format_save, ['POST']),
    ('/competition/start-date', 'competition_start_date_save', competition_start_date_save, ['POST']),
    ('/competitions/<int:cid>/edit', 'competition_edit', competition_edit, ['POST']),
    ('/competitions/<int:cid>/links', 'competition_links', competition_links, ['GET']),
    ('/competitions/<int:cid>/categories', 'categories_list', categories_list, ['GET', 'POST']),
    ('/competitions/<int:cid>/categories/template.xlsx', 'categories_template', categories_template, ['GET']),
    ('/competitions/<int:cid>/categories/import', 'categories_import', categories_import, ['POST']),
    ('/competitions/<int:cid>/categories/import/confirm', 'categories_import_confirm', categories_import_confirm, ['POST']),
    ('/competitions/<int:cid>/categories/save_all', 'categories_save_all', categories_save_all, ['POST']),
    ('/competitions/<int:cid>/categories/reorder', 'categories_reorder', categories_reorder, ['POST']),
    ('/categories/<int:catid>/delete', 'category_delete', category_delete, ['POST']),
    ('/competitions/<int:cid>', 'participants_list', participants_list, ['GET']),
    ('/competitions/<int:cid>/numbers', 'numbers_page', numbers_page, ['GET', 'POST']),
    ('/competitions/<int:cid>/tags/template.xlsx', 'bib_tags_template', bib_tags_template, ['GET']),
    ('/competitions/<int:cid>/tags/import', 'bib_tags_import', bib_tags_import, ['POST']),
    ('/competitions/<int:cid>/tags/import/confirm', 'bib_tags_import_confirm', bib_tags_import_confirm, ['POST']),
    ('/competitions/<int:cid>/tags', 'bib_tags_page', bib_tags_page, ['GET', 'POST']),
    ('/competitions/<int:cid>/participants/bulk', 'participants_bulk', participants_bulk, ['POST']),
    ('/competitions/<int:cid>/participants/add', 'participant_add', participant_add, ['POST']),
    ('/competitions/<int:cid>/participants/save_all', 'participants_save_all', participants_save_all, ['POST']),
    ('/participants/<int:pid>/delete', 'participant_delete', participant_delete, ['POST']),
    ('/competitions/<int:cid>/participants/template.xlsx', 'participants_template', participants_template, ['GET']),
    ('/competitions/<int:cid>/participants/import', 'participants_import', participants_import, ['GET', 'POST']),
    ('/competitions/<int:cid>/participants/import/confirm', 'participants_import_confirm', participants_import_confirm, ['POST']),
]
