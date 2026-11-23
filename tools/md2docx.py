# -*- coding: utf-8 -*-
"""Собирает DOCX-руководство из docs/РУКОВОДСТВО.md.

Запуск:  python tools/md2docx.py [выходной_файл]
По умолчанию пишет «docs/Хронометраж — руководство (v3).docx».
"""
import os
import re
import sys

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt, RGBColor

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(BASE_DIR, 'docs')
SRC = os.path.join(DOCS, 'РУКОВОДСТВО.md')
DEFAULT_OUT = os.path.join(DOCS, 'Хронометраж — руководство (v3).docx')


def add_inline(par, text):
    """**жирный**, `код` и обычный текст в одном абзаце."""
    for chunk in re.split(r'(\*\*[^*]+\*\*|`[^`]+`)', text):
        if not chunk:
            continue
        if chunk.startswith('**') and chunk.endswith('**'):
            par.add_run(chunk[2:-2]).bold = True
        elif chunk.startswith('`') and chunk.endswith('`'):
            run = par.add_run(chunk[1:-1])
            run.font.name = 'Consolas'
            run.font.size = Pt(10)
        else:
            par.add_run(chunk)


def build(md_path, out_path):
    lines = open(md_path, encoding='utf-8').read().splitlines()
    doc = Document()
    doc.styles['Normal'].font.name = 'Calibri'
    doc.styles['Normal'].font.size = Pt(11)
    for section in doc.sections:
        section.left_margin = Cm(2.2)
        section.right_margin = Cm(2.0)
        section.top_margin = Cm(2.0)
        section.bottom_margin = Cm(2.0)

    i, in_code = 0, False
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith('```'):
            in_code = not in_code
            i += 1
            continue
        if in_code:
            p = doc.add_paragraph()
            run = p.add_run(line)
            run.font.name = 'Consolas'
            run.font.size = Pt(9.5)
            p.paragraph_format.left_indent = Cm(0.6)
            p.paragraph_format.space_after = Pt(0)
            i += 1
            continue
        if not stripped:
            i += 1
            continue
        if stripped == '---':
            doc.add_paragraph()
            i += 1
            continue

        if stripped.startswith('|') and i + 1 < len(lines) and re.match(r'^\|[\s:|-]+\|$', lines[i + 1].strip()):
            rows = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                row = lines[i].strip()
                if not re.match(r'^\|[\s:|-]+\|$', row):
                    rows.append([c.strip() for c in row.strip('|').split('|')])
                i += 1
            table = doc.add_table(rows=len(rows), cols=len(rows[0]))
            table.style = 'Light Grid Accent 1'
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            for r, cells in enumerate(rows):
                for c, cell in enumerate(cells):
                    if c >= len(rows[0]):
                        continue
                    par = table.cell(r, c).paragraphs[0]
                    add_inline(par, cell)
                    for run in par.runs:
                        run.font.size = Pt(10)
                        if r == 0:
                            run.bold = True
            doc.add_paragraph()
            continue

        m = re.match(r'^(#{1,4})\s+(.*)$', stripped)
        if m:
            level, title = len(m.group(1)), m.group(2)
            if level == 1:
                p = doc.add_heading('', level=0)
                run = p.add_run(title)
                run.font.size = Pt(24)
                run.font.color.rgb = RGBColor(0x1D, 0x4E, 0xD8)
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            else:
                p = doc.add_heading('', level=min(level - 1, 3))
                run = p.add_run(title)
                run.font.size = Pt({2: 16, 3: 13, 4: 12}.get(level, 12))
            i += 1
            continue

        m = re.match(r'^(\d+)\.\s+(.*)$', stripped)
        if m:
            add_inline(doc.add_paragraph(style='List Number'), m.group(2))
            i += 1
            continue

        m = re.match(r'^[-*]\s+(.*)$', stripped)
        if m:
            add_inline(doc.add_paragraph(style='List Bullet'), m.group(1))
            i += 1
            continue

        p = doc.add_paragraph()
        add_inline(p, stripped)
        p.paragraph_format.space_after = Pt(6)
        i += 1

    doc.save(out_path)
    return out_path


if __name__ == '__main__':
    out = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    path = build(SRC, out)
    print('сохранено:', path, os.path.getsize(path), 'байт')
