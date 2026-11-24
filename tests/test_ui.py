# -*- coding: utf-8 -*-
"""Единый вид: токены, общие компоненты, терминология."""
from tests.support import ChronoTestCase


class UiTokensTest(ChronoTestCase):
    def test_tokens_present_in_base(self):
        self.login_admin()
        for url in ('/competitions', f'/competitions/{self.comp["id"]}', '/races'):
            html = self.client.get(url).get_data(as_text=True)
            self.assertIn('--primary: #2563eb', html, url)
            self.assertIn('--radius: 8px', html, url)

    def test_common_button_classes(self):
        self.login_admin()
        self.add_category('Категория А', 1, 1)
        with self.conn:
            self.conn.execute(
                "INSERT INTO participants (competition_id, number, full_name, gender) "
                "VALUES (?, 1, 'Тестовый Участник', 'M')", (self.comp['id'],))
        html = self.client.get(f'/competitions/{self.comp["id"]}').get_data(as_text=True)
        self.assertIn('class="btn btn-ghost"', html)


class UiTerminologyTest(ChronoTestCase):
    def test_participants_use_category_wording(self):
        self.login_admin()
        self.add_category('Категория А', 1, 1)
        html = self.client.get(f'/competitions/{self.comp["id"]}').get_data(as_text=True)
        self.assertIn('Назначить категорию', html)
        self.assertIn('— без категории —', html)
        self.assertNotIn('без группы', html)

    def test_heading_without_emoji(self):
        self.login_admin()
        cid = self.comp['id']
        html = self.client.get(f'/competitions/{cid}/tags').get_data(as_text=True)
        self.assertIn('<h2>Метки на номерах', html)
        self.assertNotIn('<h2>📡', html)
