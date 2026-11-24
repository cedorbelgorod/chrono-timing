# -*- coding: utf-8 -*-
"""Журналы хронометража не должны перетекать между соревнованиями."""
from chrono import rfid
from chrono.db import _db_key
from tests.support import ChronoTestCase

LOG_ITEM = {'ok': True, 'number': 7, 'time': '10:35:03', 'action': 'Круг 1/2'}


class ManualJournalScopeTest(ChronoTestCase):
    def test_log_cleared_for_other_competition(self):
        self.login_admin()
        with self.client.session_transaction() as sess:
            sess['manual_key'] = 'другое-соревнование'
            sess['manual_log'] = [dict(LOG_ITEM)]
            sess['manual_last'] = {'number': 7, 'ok': True}
        html = self.client.get('/manual').get_data(as_text=True)
        self.assertNotIn('Круг 1/2', html)

    def test_log_kept_for_current_competition(self):
        self.login_admin()
        with self.client.session_transaction() as sess:
            sess['manual_key'] = sess.get('comp')
            sess['manual_log'] = [dict(LOG_ITEM)]
        html = self.client.get('/manual').get_data(as_text=True)
        self.assertIn('Круг 1/2', html)


class AutoLogScopeTest(ChronoTestCase):
    def tearDown(self):
        rfid._auto_log.clear()
        super().tearDown()

    def test_auto_log_filtered_by_competition(self):
        self.login_admin()
        key = _db_key(self.conn)
        rfid._auto_log.clear()
        rfid._push_auto_log({'ok': True, 'number': 1, 'message': 'Круг 1/1 (авто)'}, key)
        rfid._push_auto_log({'ok': True, 'number': 2, 'message': 'ЧУЖОЙ ЖУРНАЛ'}, '/tmp/other.db')
        html = self.client.get('/auto').get_data(as_text=True)
        self.assertIn('Круг 1/1 (авто)', html)
        self.assertNotIn('ЧУЖОЙ ЖУРНАЛ', html)

    def test_auto_log_endpoint_filtered(self):
        self.login_admin()
        key = _db_key(self.conn)
        rfid._auto_log.clear()
        rfid._push_auto_log({'ok': True, 'number': 3, 'message': 'Свой круг'}, key)
        rfid._push_auto_log({'ok': True, 'number': 4, 'message': 'Чужой круг'}, '/tmp/other.db')
        html = self.client.get('/auto/log').get_data(as_text=True)
        self.assertIn('Свой круг', html)
        self.assertNotIn('Чужой круг', html)
