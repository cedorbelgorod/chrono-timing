# -*- coding: utf-8 -*-
"""Регрессии рефакторинга: навигация и пропущенные импорты."""
from tests.support import ChronoTestCase

MENU_ITEMS = ('Категории', 'Участники', 'Выдать номера', 'Метки', 'Саморегистрация',
              'Ссылки', 'Журнал заявок', 'Настройки', 'Приложение Android')


class NavRegressionTest(ChronoTestCase):
    def test_competition_menu_has_all_items(self):
        """Регрессия: меню «Соревнование» не должно схлопываться до двух пунктов."""
        self.login_admin()
        html = self.client.get('/competitions').get_data(as_text=True)
        # вырезаем только меню, чтобы не ловить совпадения в теле страницы
        menu = html[html.find('Соревнование <span class="caret">'):]
        menu = menu[:menu.find('</nav>')]
        for label in MENU_ITEMS:
            self.assertIn(label, menu, label)

    def test_auto_device_page_renders(self):
        """Регрессия: /auto/device использует SWRFID_AVAILABLE — без NameError."""
        self.login_admin()
        self.assertEqual(self.client.get('/auto/device').status_code, 200)


class MissingImportsRegressionTest(ChronoTestCase):
    def test_no_missing_module_names(self):
        """Каждая функция в chrono/** ссылается только на доступные имена."""
        from tools.audit_names import find_missing_names
        self.assertEqual(find_missing_names(), {}, 'Пропущенные импорты после рефакторинга')
