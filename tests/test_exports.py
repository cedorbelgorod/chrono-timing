# -*- coding: utf-8 -*-
"""Экспорт категорий и участников в Excel и совместимость с импортом."""
import io

from openpyxl import load_workbook

from tests.support import ChronoTestCase


class ExportXlsxTest(ChronoTestCase):
    def test_categories_export(self):
        self.login_admin()
        cid = self.comp['id']
        cat = self.add_category('Юноши 12-14', laps=3)
        with self.conn:
            self.conn.execute(
                "UPDATE categories SET gender = 'M', age_range = '12-14' WHERE id = ?", (cat,))
        self.add_category('Все', laps=1)
        resp = self.client.get(f'/competitions/{cid}/categories/export.xlsx')
        self.assertEqual(resp.status_code, 200)
        ws = load_workbook(io.BytesIO(resp.data)).active
        rows = list(ws.iter_rows(values_only=True))
        self.assertEqual(rows[0], ('Название', 'Кругов', 'Пол', 'Возраст'))
        self.assertIn(('Юноши 12-14', 3, 'М', '12-14'), rows)
        self.assertIn(('Все', 1, 'МЖ', None), rows)

    def test_participants_export(self):
        self.login_admin()
        cid = self.comp['id']
        cat = self.add_category('Юноши', laps=2)
        with self.conn:
            self.conn.execute(
                "INSERT INTO participants (competition_id, number, full_name, birth_year, gender, "
                "category_id, city, team) VALUES (?, 7, 'Лопин Антон', 1980, 'M', ?, 'Белгород', 'Лично')",
                (cid, cat))
            self.conn.execute(
                "INSERT INTO participants (competition_id, number, full_name, gender) "
                "VALUES (?, NULL, 'Без номера', 'F')", (cid,))
        resp = self.client.get(f'/competitions/{cid}/participants/export.xlsx')
        self.assertEqual(resp.status_code, 200)
        ws = load_workbook(io.BytesIO(resp.data)).active
        rows = list(ws.iter_rows(values_only=True))
        self.assertEqual(rows[0],
                         ('Номер', 'ФИО', 'Год рождения', 'Пол', 'Категория', 'Город', 'Команда'))
        self.assertIn((7, 'Лопин Антон', 1980, 'М', 'Юноши', 'Белгород', 'Лично'), rows)
        self.assertIn((None, 'Без номера', None, 'Ж', None, None, None), rows)

    def test_categories_export_import_roundtrip(self):
        self.login_admin()
        cid = self.comp['id']
        cat = self.add_category('Юноши 12-14', laps=3)
        with self.conn:
            self.conn.execute(
                "UPDATE categories SET gender = 'M', age_range = '12-14' WHERE id = ?", (cat,))
        data = self.client.get(f'/competitions/{cid}/categories/export.xlsx').data
        # тот же файл проходит проверку импорта (совместимость формата)
        resp = self.client.post(
            f'/competitions/{cid}/categories/import',
            data={'file': (io.BytesIO(data), 'kategorii.xlsx')},
            content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('Юноши 12-14', html)

    def test_participants_export_import_preview(self):
        self.login_admin()
        cid = self.comp['id']
        cat = self.add_category('Юноши', laps=2)
        with self.conn:
            self.conn.execute(
                "INSERT INTO participants (competition_id, number, full_name, birth_year, gender, "
                "category_id, city, team) VALUES (?, 7, 'Лопин Антон', 1980, 'M', ?, 'Белгород', 'Лично')",
                (cid, cat))
        data = self.client.get(f'/competitions/{cid}/participants/export.xlsx').data
        # тот же файл проходит проверку импорта (совместимость формата)
        resp = self.client.post(
            f'/competitions/{cid}/participants/import',
            data={'file': (io.BytesIO(data), 'uchastniki.xlsx')},
            content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('Лопин Антон', html)
        self.assertIn('Юноши', html)
