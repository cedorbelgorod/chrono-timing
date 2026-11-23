# -*- coding: utf-8 -*-
"""Стартовый протокол: страница, PDF, Excel и публичная короткая ссылка."""
import io

from openpyxl import load_workbook

from tests.support import ChronoTestCase


class StartlistTest(ChronoTestCase):
    def _prepare(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        self.board('begin')
        self.board('start')
        return hid, p1, p2

    def test_page_has_laps_column(self):
        hid, p1, p2 = self._prepare()
        resp = self.client.get(f'/heats/{hid}/startlist')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('>Круги<', html)
        self.assertIn('>2<', html)
        self.assertIn('>3<', html)

    def test_pdf(self):
        hid, p1, p2 = self._prepare()
        resp = self.client.get(f'/heats/{hid}/startlist.pdf')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.startswith(b'%PDF-'))

    def test_xlsx_has_laps_column(self):
        hid, p1, p2 = self._prepare()
        resp = self.client.get(f'/heats/{hid}/startlist.xlsx')
        self.assertEqual(resp.status_code, 200)
        wb = load_workbook(io.BytesIO(resp.data))
        rows = list(wb.active.iter_rows(values_only=True))
        header = next(r for r in rows if r and r[0] == '№ п/п')
        self.assertIn('Круги', header)

    def test_short_link_create_and_rotate(self):
        hid, p1, p2 = self._prepare()
        first = self.client.post(f'/heats/{hid}/startlist_link').get_json()
        self.assertTrue(first['ok'])
        token = first['token']
        anon = self.public_client()
        self.assertEqual(anon.get(f'/sl/{token}').status_code, 200)

        rotated = self.client.post(f'/heats/{hid}/startlist_link', data={'rotate': '1'}).get_json()
        self.assertNotEqual(rotated['token'], token)
        self.assertEqual(anon.get(f'/sl/{token}').status_code, 404)
        self.assertEqual(anon.get(f"/sl/{rotated['token']}").status_code, 200)

    def test_public_startlist_shows_laps(self):
        hid, p1, p2 = self._prepare()
        token = self.client.post(f'/heats/{hid}/startlist_link').get_json()['token']
        html = self.public_client().get(f'/sl/{token}').get_data(as_text=True)
        self.assertIn('Круги', html)
        self.assertIn('Второй Участник', html)

    def test_same_link_is_reused(self):
        hid, p1, p2 = self._prepare()
        first = self.client.post(f'/heats/{hid}/startlist_link').get_json()
        second = self.client.post(f'/heats/{hid}/startlist_link').get_json()
        self.assertEqual(first['token'], second['token'])
