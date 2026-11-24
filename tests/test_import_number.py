# -*- coding: utf-8 -*-
"""Импорт участников: телефон из формы не должен становиться стартовым номером."""
import io
import re

from openpyxl import Workbook

from tests.support import ChronoTestCase


def _book(headers, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


class ImportNumberColumnTest(ChronoTestCase):
    FORM_HEADERS = [
        'Отметка времени', 'Внимание !', 'Фамилия Имя', 'Год рождения', 'Пол',
        'Населенный пункт', 'Команда',
        'Контактный номер (виден только организаторам)',
        'С порядком допуска на соревнования, ознакомлен.',
        'Я даю согласие на обработку персональных данных',
    ]

    def _preview(self, data):
        resp = self.client.post(
            f'/competitions/{self.comp["id"]}/participants/import',
            data={'file': (io.BytesIO(data), 'form.xlsx')},
            content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def _number_values(self, html):
        return re.findall(r'name="row_\d+_number"\s+value="([^"]*)"', html)

    def test_contact_number_is_not_bib_number(self):
        self.login_admin()
        data = _book(self.FORM_HEADERS, [
            ['15.11.2026', 'Согласен (на)', 'Белоконь Александр', 1998, 'Мужской',
             'Белгород', 'Bikenergy', 89204548368, 'Да', 'Да'],
            ['16.11.2026', 'Согласен (на)', 'Черепнин Евгений', 2013, 'Мужской',
             'Курск', 'СШ Картинг', None, 'Да', 'Да'],
        ])
        html = self._preview(data)
        self.assertEqual(self._number_values(html), ['', ''])
        self.assertNotIn('89204548368', html)

    def test_real_number_column_wins_over_contact(self):
        self.login_admin()
        headers = self.FORM_HEADERS + ['Номер']
        data = _book(headers, [
            ['15.11.2026', 'Согласен (на)', 'Белоконь Александр', 1998, 'Мужской',
             'Белгород', 'Bikenergy', 89204548368, 'Да', 'Да', 12],
        ])
        html = self._preview(data)
        self.assertEqual(self._number_values(html), ['12'])

    def test_implausible_number_ignored(self):
        self.login_admin()
        headers = [h for h in self.FORM_HEADERS
                   if 'Контактный номер' not in h] + ['Номер']
        data = _book(headers, [
            ['15.11.2026', 'Согласен (на)', 'Белоконь Александр', 1998, 'Мужской',
             'Белгород', 'Bikenergy', 'Да', 'Да', 89204548368],
        ])
        html = self._preview(data)
        self.assertEqual(self._number_values(html), [''])
