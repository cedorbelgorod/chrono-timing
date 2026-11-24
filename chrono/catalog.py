# -*- coding: utf-8 -*-
"""Участники, категории, номера и метки: подбор категории, выдача номеров, разбор Excel."""
from datetime import datetime
import re
from flask import redirect, url_for, session

from chrono.utils import _age_range_hint, _age_range_years, _parse_age_range

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

def _to_float(raw):
    """Число из строки с запятой-разделителем («2,3» → 2.3); None, если не число."""
    try:
        return float(str(raw or '').replace(',', '.'))
    except (TypeError, ValueError):
        return None

def _scalar(conn, sql, *args):
    """Первый столбец первой строки запроса (None, если пусто)."""
    row = conn.execute(sql, args).fetchone()
    return row[0] if row else None

def _norm_name(s):
    return ' '.join(str(s).lower().replace('ё', 'е').split()) if s else ''

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

def _apply_age_range(conn, cid, start_year):
    """Пересчитывает годы рождения категорий из строки возраста и года старта.

    Возраст участника считается на 31 декабря года старта: age = год − год рождения.
    """
    updated = 0
    for cat in conn.execute(
            'SELECT id, age_range, year_from, year_to FROM categories WHERE competition_id = ?',
            (cid,)).fetchall():
        age_min, age_max, _err = _parse_age_range(cat['age_range'])
        year_from, year_to = _age_range_years(start_year, age_min, age_max)
        if (year_from, year_to) != (cat['year_from'], cat['year_to']):
            conn.execute('UPDATE categories SET year_from = ?, year_to = ? WHERE id = ?',
                         (year_from, year_to, cat['id']))
            updated += 1
    return updated

def _category_age_hint(cat, start_year):
    """Подсказка «2016–2017 г.р.» для категории (по строке возраста)."""
    age_min, age_max, _err = _parse_age_range(cat['age_range'])
    return _age_range_hint(start_year, age_min, age_max)

def _category_mismatch(cat, gender, birth_year):
    """Участник не попадает в условия категории (пол + возраст)."""
    return not _category_matches(cat, gender, birth_year)

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

def _category_participants(conn, cat_id):
    """Участники категории. cat_id = 0 — участники без группы (category_id IS NULL)."""
    if cat_id:
        return conn.execute('SELECT id, number, full_name, birth_year, gender FROM participants WHERE category_id = ? ORDER BY number', (cat_id,)).fetchall()
    return conn.execute('SELECT id, number, full_name, birth_year, gender FROM participants WHERE category_id IS NULL ORDER BY number').fetchall()

CATEGORY_HEADER_ALIASES = {
    'name': ['название', 'категория', 'группа', 'name', 'category'],
    'laps': ['кругов', 'круги', 'круг', 'количество кругов', 'кол во кругов', 'laps', 'laps total'],
    'gender': ['пол', 'gender', 'sex'],
    'age': ['возраст', 'возраст участника', 'возраст от до', 'age', 'ages'],
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

TAGS_HEADER_ALIASES = {
    'number': ['номер', 'номер на старте', 'стартовый номер', 'bib', 'number'],
    'tag1': ['метка', 'метка 1', 'основная метка', 'основная', 'tag', 'tag1', 'epc'],
    'tag2': ['метка 2', 'резервная метка', 'резервная', 'tag2', 'epc 2'],
}


from chrono.auth import _is_read_only
from chrono.db import (_get_competition, get_db)

def inject_competition_hook():
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
