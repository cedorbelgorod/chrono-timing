# -*- coding: utf-8 -*-
"""Инфраструктура тестов «Хронометража».

Данные изолированы: тесты работают в папке .tests-data рядом с проектом
и не трогают рабочие chrono.db / comps / log.

Запуск (любой вариант):
    python -m pytest tests -q
    python -m unittest discover -s tests -t .
"""
import atexit
import os
import shutil
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMPROOT = os.path.join(BASE, '.tests-data')

shutil.rmtree(TMPROOT, ignore_errors=True)
os.makedirs(TMPROOT, exist_ok=True)
atexit.register(shutil.rmtree, TMPROOT, True)

# пути задаются до импорта приложения: оно читает их при загрузке модуля
os.environ['CHRONO_DB'] = os.path.join(TMPROOT, 'chrono.db')
os.environ['CHRONO_LOG_DIR'] = os.path.join(TMPROOT, 'log')
os.environ['CHRONO_CERT_DIR'] = os.path.join(TMPROOT, 'cert')
os.environ['CHRONO_REGISTRY'] = os.path.join(TMPROOT, 'competitions.json')
os.environ['CHRONO_SECRET_KEY'] = 'test-secret-key'
os.environ['CHRONO_SESSION_DAYS'] = '1'

if BASE not in sys.path:
    sys.path.insert(0, BASE)

import app as chrono  # noqa: E402


def _reset_data():
    """Чистые база, реестр и кэш настроек перед каждым тестом."""
    for name in os.listdir(TMPROOT):
        path = os.path.join(TMPROOT, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                pass
    os.makedirs(chrono.LOG_DIR, exist_ok=True)
    with chrono._settings_lock:
        chrono._settings_cache.clear()
    chrono.init_db()
    chrono._ensure_registry()


class ChronoTestCase(unittest.TestCase):
    """База: изолированные данные, клиент и помощники."""

    def setUp(self):
        _reset_data()
        chrono.app.config.update(TESTING=True)
        self.client = chrono.app.test_client()
        self.comp = self._current_comp()
        self.conn = chrono._connect(self.comp['path'])

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass

    # ---------- окружение ----------
    def _current_comp(self):
        entry = chrono._registry_load()['competitions'][0]
        path = chrono._comp_db_path(entry)
        conn = chrono._connect(path)
        try:
            cid = conn.execute('SELECT id FROM competitions ORDER BY id LIMIT 1').fetchone()['id']
        finally:
            conn.close()
        return {'id': cid, 'slug': entry['slug'], 'path': path, 'entry': entry}

    def login_admin(self):
        self.client.get('/c/' + self.comp['slug'])
        resp = self.client.post('/login', data={'username': 'admin', 'password': 'admin'})
        self.assertIn(resp.status_code, (200, 302))

    def public_client(self):
        """Отдельный клиент без сессии (как анонимный посетитель)."""
        return chrono.app.test_client()

    # ---------- данные ----------
    def set_format(self, fmt):
        with self.conn:
            self.conn.execute('UPDATE competitions SET format = ? WHERE id = ?', (fmt, self.comp['id']))

    def add_category(self, name, laps=1, sort_order=0):
        with self.conn:
            cur = self.conn.execute(
                'INSERT INTO categories (competition_id, name, laps_total, sort_order) VALUES (?, ?, ?, ?)',
                (self.comp['id'], name, laps, sort_order))
        return cur.lastrowid

    def add_participant(self, number, name, category_id=None):
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO participants (competition_id, number, full_name, gender, category_id) "
                "VALUES (?, ?, ?, 'M', ?)",
                (self.comp['id'], number, name, category_id))
        return cur.lastrowid

    def add_heat(self, number=1, fmt='itt', min_lap=0, auto=False, finish_on_leader=0,
                 start_order='number'):
        with self.conn:
            cur = self.conn.execute(
                'INSERT INTO heats (competition_id, heat_number, status, min_lap_time, format, start_order, '
                'auto_enabled, finish_on_leader) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (self.comp['id'], number, 'pending', min_lap, fmt, start_order,
                 1 if auto else 0, 1 if finish_on_leader else 0))
        return cur.lastrowid

    def add_to_heat(self, hid, pids):
        with self.conn:
            for pid in pids:
                self.conn.execute(
                    "INSERT INTO heat_participants (heat_id, participant_id, status) VALUES (?, ?, 'active')",
                    (hid, pid))

    def heat(self, hid):
        return self.conn.execute('SELECT * FROM heats WHERE id = ?', (hid,)).fetchone()

    def race_row(self, pid):
        return self.conn.execute(
            'SELECT * FROM races WHERE participant_id = ? ORDER BY id DESC LIMIT 1', (pid,)).fetchone()

    def add_tag(self, number, tag):
        with self.conn:
            self.conn.execute(
                'INSERT INTO bib_tags (competition_id, number, tag, slot) VALUES (?, ?, ?, 1)',
                (self.comp['id'], number, tag))

    # ---------- действия ----------
    def board(self, action):
        return self.client.post('/manual/start/action',
                                data={'competition_id': str(self.comp['id']), 'action': action})

    def board_state(self):
        return self.client.get('/manual/start/data?cid=%s' % self.comp['id']).get_json()

    def lap(self, number):
        return self.client.post('/manual/action',
                                data={'competition_id': str(self.comp['id']),
                                      'number': str(number), 'json': '1'}).get_json()

    def request_ctx(self):
        return chrono.app.test_request_context('/')

    # ---------- сценарии ----------
    def itt_heat(self, laps=(2, 3), auto=False, tags=()):
        """Разделка: категории с разными кругами и два участника."""
        self.login_admin()
        self.set_format('itt')
        cat_a = self.add_category('Категория А', laps[0], 1)
        cat_b = self.add_category('Категория Б', laps[1], 2)
        p1 = self.add_participant(1, 'Первый Участник', cat_a)
        p2 = self.add_participant(2, 'Второй Участник', cat_b)
        hid = self.add_heat(1, 'itt', auto=auto)
        self.add_to_heat(hid, [p1, p2])
        for num, tag in tags:
            self.add_tag(num, tag)
        return hid, p1, p2
