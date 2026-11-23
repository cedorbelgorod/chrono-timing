# -*- coding: utf-8 -*-
"""Страница «Ссылки» и короткие ссылки на старты."""
from tests.support import ChronoTestCase, chrono


class LinksPageTest(ChronoTestCase):
    def test_links_page_shows_all_addresses(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        resp = self.client.get(f"/competitions/{self.comp['id']}/links")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        for part in (f"/p/{self.comp['slug']}", '/reg/', 'Результаты', 'Табло',
                     'Стартовые протоколы', 'Создать ссылку'):
            self.assertIn(part, html)

    def test_short_link_appears_on_page(self):
        hid, p1, p2 = self.itt_heat(laps=(2, 3))
        token = self.client.post(f'/heats/{hid}/startlist_link').get_json()['token']
        html = self.client.get(f"/competitions/{self.comp['id']}/links").get_data(as_text=True)
        self.assertIn(f'/sl/{token}', html)

    def test_archived_competition_shows_warning(self):
        self.itt_heat(laps=(2, 3))
        self.login_admin()
        reg = chrono._registry_load()
        reg['competitions'][0]['archived'] = True
        chrono._registry_save(reg)
        resp = self.client.get(f"/competitions/{self.comp['id']}/links")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('в архиве', resp.get_data(as_text=True))
