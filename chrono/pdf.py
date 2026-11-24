# -*- coding: utf-8 -*-
"""PDF: шрифты с кириллицей, стартовый протокол и итоговый протокол."""
import io
import logging
import os

log = logging.getLogger('chrono')


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
    heat_label = (data['heat'].get('name') or '').strip() or f'Старт №{data["heat"]["number"]}'
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm,
                            title=f'Стартовый протокол — {heat_label}')
    story = []
    story.append(Paragraph(esc(data['competition']), st(15, bold, TA_CENTER)))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph('СТАРТОВЫЙ ПРОТОКОЛ', st(12, bold, TA_CENTER)))
    head_bits = [heat_label]
    if data['heat']['name']:
        head_bits.append(data['heat']['name'])
    if data['heat']['format'] == 'itt':
        head_bits.append(f'раздельный старт, интервал {data["heat"]["interval"]} с')
        if data['heat']['start_at']:
            head_bits.append(f'первый старт {data["heat"]["start_at"]}')
    else:
        head_bits.append('групповой старт')
        if data['heat']['start_at']:
            head_bits.append(f'старт {data["heat"]["start_at"]}')
        head_bits.append('№ — порядок вызова')
    story.append(Paragraph(esc(' · '.join(head_bits)), st(9, None, TA_CENTER)))
    story.append(Paragraph(esc(f'Участников: {data["total"]} · стартовали: {data["started"]} · финишировали: {data["finished"]}'),
                           st(8.5, None, TA_CENTER)))
    story.append(Spacer(1, 4 * mm))

    widths_mm = [11, 13, 36, 11, 26, 12, 18, 20, 19, 20]
    scale = 186.0 / sum(widths_mm)
    col_w = [w * scale * mm for w in widths_mm]
    head = ['№<br/>п/п', 'Номер', 'ФИО', 'Год', 'Группа', 'Круги',
            'Город', 'Команда', 'Время<br/>старта', 'Прим.']
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
            Paragraph(str(r.get('laps') or 1), cell_c),
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

def _render_protocol_pdf(comp, data):
    """Собирает PDF протокола по образцу официального документа."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, KeepTogether,
    )
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
            log.exception('Protocol PDF: не удалось загрузить шрифт %s', reg_path)
    if regular == 'Helvetica':
        log.warning('Protocol PDF: шрифт с кириллицей не найден — текст может не отображаться')

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
        # Место, №, ФИО, Год, Город, Команда, Круги, Старт, Финиш, Время, Отставание, V.ср
        col_w = [12, 8, 26, 12, 18, 17, 12, 15, 15, 19, 21, 11]
    # масштабируем под доступную ширину
    total_mm = sum(col_w)
    col_w = [w * W / total_mm for w in col_w]

    def race_table(rows):
        names = ['Место', '№', 'ФИО', 'Год<br/>рожд.', 'Город', 'Команда']
        if is_itt:
            names += ['Круги', 'Старт', 'Финиш']
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
                    Paragraph(str(race.get('laps_display') or ''), cell_c),
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
