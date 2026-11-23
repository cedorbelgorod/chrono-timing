# -*- coding: utf-8 -*-
"""Чистые помощники: форматирование, шкалы времени, QR, slug, токены."""
from datetime import datetime
import re
import secrets


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

def _speed_kmh(sec, km):
    """Средняя скорость в км/ч строкой с запятой («27,1»); пусто, если нет данных."""
    if not km or not sec:
        return ''
    return f'{km / (sec / 3600):.1f}'.replace('.', ',')

def _fmt_start_at(value):
    """Плановое время старта: ISO → «23.11.2026 12:00» (или пусто)."""
    if not value:
        return ''
    try:
        return datetime.fromisoformat(value).strftime('%d.%m.%Y %H:%M')
    except (TypeError, ValueError):
        return ''

_TOKEN_ALPHABET = 'abcdefghijkmnpqrstuvwxyz23456789'

def _new_startlist_token(conn):
    """Короткий токен публичной ссылки, уникальный в этой базе."""
    for _ in range(30):
        token = ''.join(secrets.choice(_TOKEN_ALPHABET) for _ in range(8))
        row = conn.execute('SELECT 1 FROM heats WHERE startlist_token = ?', (token,)).fetchone()
        if not row:
            return token
    return secrets.token_hex(6)
