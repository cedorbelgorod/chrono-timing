# -*- coding: utf-8 -*-
"""Запуск HTTP/HTTPS-сервера (вызывается из app.py)."""
import os
import threading
import time

from chrono.db import CERT_DIR, _ensure_registry, _migrate_competitions, init_db
from chrono.people import _people_auto_refresh
from chrono.ratings import _ratings_auto_refresh
from chrono.rfid import start_swrfid_listener


def run(flask_app):
    init_db()
    _ensure_registry()
    _migrate_competitions()
    _people_auto_refresh()
    _ratings_auto_refresh()
    start_swrfid_listener()

    CERT_PATH = os.path.join(CERT_DIR, 'shumilova.cedor.ru-crt.pem')
    KEY_PATH = os.path.join(CERT_DIR, 'shumilova.cedor.ru-key.pem')

    # Порты можно задать переменными окружения (за прокси TLS терминируется снаружи)
    HTTP_PORT = int(os.environ.get('CHRONO_HTTP_PORT', '5000'))
    HTTPS_PORT = int(os.environ.get('CHRONO_HTTPS_PORT', '5001'))
    HTTPS_ENABLED = os.environ.get('CHRONO_HTTPS', '1') == '1'

    print('=' * 60, flush=True)
    print(f'Сервер запускается на HTTP:  http://0.0.0.0:{HTTP_PORT}', flush=True)

    ssl_ctx = None
    if HTTPS_ENABLED and os.path.exists(CERT_PATH) and os.path.exists(KEY_PATH):
        ssl_ctx = (CERT_PATH, KEY_PATH)
        print(f'Сервер запускается на HTTPS: https://0.0.0.0:{HTTPS_PORT}', flush=True)
        print('🔒 Поддержка HTTPS (SSL) включена', flush=True)
    elif not HTTPS_ENABLED:
        print('HTTPS внутри приложения отключён (CHRONO_HTTPS=0)', flush=True)
    else:
        print('⚠ ВНИМАНИЕ: Файлы сертификатов не найдены. HTTPS недоступен.', flush=True)
    print('=' * 60, flush=True)

    from werkzeug.serving import make_server

    flask_app.debug = True

    servers = []

    # HTTP Server
    http_server = make_server('0.0.0.0', HTTP_PORT, flask_app, threaded=True)
    servers.append(http_server)

    # HTTPS Server
    if ssl_ctx:
        https_server = make_server('0.0.0.0', HTTPS_PORT, flask_app, threaded=True, ssl_context=ssl_ctx)
        servers.append(https_server)

    server_threads = []
    for srv in servers:
        t = threading.Thread(target=srv.serve_forever)
        t.daemon = True
        t.start()
        server_threads.append(t)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nОстановка серверов...", flush=True)
        for srv in servers:
            srv.shutdown()
