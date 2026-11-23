# -*- coding: utf-8 -*-
"""Экспорт: Excel результатов и стартового протокола, PDF и ширины колонок."""
import io
import os
import unittest

from openpyxl import load_workbook

from tests.support import ChronoTestCase

DEJAVU = ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
          '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf')

# ширины колонок PDF (мм до масштабирования под A4) — должны совпадать с app.py
ITT_WIDTHS = [12, 8, 26, 12, 18, 17, 12, 15, 15, 19, 21, 11]
ITT_HEADS = ['Место', '№', 'ФИО', 'Год рожд.', 'Город', 'Команда', 'Круги',
             'Старт', 'Финиш', 'Общее время', 'Отставание', 'V.ср км/ч']
ITT_VALUES = ['1', '21', 'Пятёрочкин', '1990', 'Белгород', 'МБУДО СШ №4', '5',
              '16:51:53', '16:57:14', '1:05:21,21', '+00:41,95', '24,5']

STARTLIST_WIDTHS = [11, 13, 36, 11, 26, 12, 18, 20, 19, 20]
STARTLIST_HEADS = ['№ п/п', 'Номер', 'ФИО', 'Год', 'Группа', 'Круги', 'Город',
                   'Команда', 'Время старта', 'Прим.']
STARTLIST_VALUES = ['1', '21', 'Пятёрочкин', '1990', 'Пять кругов', '5', 'Белгород',
                    'МБУДО СШ №4', '16:51:53', 'на дистанции']


class ExportTest(ChronoTestCase):
    def test_results_xlsx_has_laps(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        self.board('begin')
        self.board('start')
        self.lap(1)
        self.lap(1)
        resp = self.client.get(f'/results/export?cid={self.comp["id"]}&target=heat:{hid}')
        self.assertEqual(resp.status_code, 200)
        wb = load_workbook(io.BytesIO(resp.data))
        header = next(wb.active.iter_rows(values_only=True))
        self.assertIn('Круги', header)

    def test_protocol_pdf(self):
        self.itt_heat(laps=(2, 3))
        self.board('begin')
        self.board('start')
        self.lap(1)
        self.lap(1)
        resp = self.client.get('/protocol.pdf')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.startswith(b'%PDF-'))

    @unittest.skipUnless(os.path.exists(DEJAVU[0]) and os.path.exists(DEJAVU[1]),
                         'DejaVu не установлен — аудит ширин выполняется в контейнере')
    def test_pdf_columns_fit(self):
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        pdfmetrics.registerFont(TTFont('TestReg', DEJAVU[0]))
        pdfmetrics.registerFont(TTFont('TestBold', DEJAVU[1]))
        width = A4[0] - 24 * mm

        def audit(widths, heads, values, label):
            cols = [w * width / sum(widths) for w in widths]
            problems = []
            for head, col in zip(heads, cols):
                for word in head.split():
                    need = pdfmetrics.stringWidth(word, 'TestBold', 8)
                    if need > col - 4:
                        problems.append((label, 'шапка', word, round(need / mm, 1), round(col / mm, 1)))
            for value, col in zip(values, cols):
                for word in str(value).split():
                    need = pdfmetrics.stringWidth(word, 'TestReg', 8)
                    if need > col - 4:
                        problems.append((label, 'данные', word, round(need / mm, 1), round(col / mm, 1)))
            return problems

        problems = audit(ITT_WIDTHS, ITT_HEADS, ITT_VALUES, 'протокол')
        problems += audit(STARTLIST_WIDTHS, STARTLIST_HEADS, STARTLIST_VALUES, 'стартовый')
        self.assertEqual(problems, [], problems)
