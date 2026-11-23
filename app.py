# -*- coding: utf-8 -*-
"""Хронометраж — точка входа.

Здесь только сборка Flask-приложения: конфигурация, хуки и регистрация
маршрутов. Логика живёт в пакете `chrono/` (см. README, «Структура проекта»).

Запуск: `python app.py` (сервер — `chrono/server.py`).
"""
import logging
import os
import secrets
from datetime import timedelta

from flask import Flask

from chrono.auth import SESSION_DAYS, require_auth_hook
from chrono.catalog import inject_competition_hook
from chrono.db import LOG_DIR

# каталог логов нужен до настройки логгеров (иначе первые записи теряются)
os.makedirs(LOG_DIR, exist_ok=True)

from chrono.logging_setup import (  # noqa: E402  (после создания каталога логов)
    handle_exception_hook, log_request_hook,
)
from chrono.views import (  # noqa: E402
    auth as auth_views, auto as auto_views, catalog as catalog_views,
    manage as manage_views, manual as manual_views, public as public_views,
    races as races_views, registration as registration_views,
    results as results_views, users as users_views,
)

app = Flask(__name__)

# ---- секретный ключ сессий ----
_secret = os.environ.get('CHRONO_SECRET_KEY')
if not _secret:
    _secret = secrets.token_hex(32)
    print('⚠  CHRONO_SECRET_KEY не задан — сгенерирован временный.', flush=True)
app.secret_key = _secret

# ---- сессии и cookies ----
app.permanent_session_lifetime = timedelta(days=SESSION_DAYS)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)

# ---- за обратным прокси (Caddy) доверяем X-Forwarded-*, чтобы ссылки были https:// ----
if os.environ.get('CHRONO_PROXY') == '1':
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

logging.getLogger('werkzeug').setLevel(logging.WARNING)
logging.getLogger('werkzeug').propagate = False


# ======================= ХУКИ =======================
# Проверка доступа, журнал запросов, обработчик ошибок и контекст шаблонов —
# тела вынесены в chrono.auth / chrono.logging_setup / chrono.catalog.

@app.before_request
def _require_auth():
    return require_auth_hook()


@app.after_request
def _log_request(response):
    return log_request_hook(response)


@app.errorhandler(Exception)
def _handle_exception(e):
    return handle_exception_hook(e)


@app.context_processor
def _inject_competition():
    return inject_competition_hook()


# ======================= МАРШРУТЫ =======================
# Каждый модуль в chrono/views/ отдаёт RULES: (правило, эндпоинт, функция, методы).
# Имена эндпоинтов сохраняем прежними — на них завязаны url_for() в шаблонах.

_VIEW_MODULES = (
    auth_views, manage_views, users_views, registration_views, catalog_views,
    races_views, results_views, auto_views, manual_views, public_views,
)
for _views in _VIEW_MODULES:
    for _rule, _endpoint, _view, _methods in _views.RULES:
        app.add_url_rule(_rule, _endpoint, _view, methods=_methods)


# ======================= СОВМЕСТИМОСТЬ =======================
# Исторические имена модуля `app`: ими пользуются тесты и внешние скрипты.
# Новый код должен импортировать из chrono.* напрямую.
from chrono.db import (  # noqa: E402,F401
    COMPS_DIR, _comp_db_path, _connect, _create_competition_db, _ensure_registry,
    _registry_load, _registry_save, _rel_db_path, init_db,
)
from chrono.rfid import _process_tag_scan  # noqa: E402,F401
from chrono.settings import _settings_cache, _settings_lock  # noqa: E402,F401
from chrono.timing import _build_results_detail, _start_heat  # noqa: E402,F401


if __name__ == '__main__':
    from chrono.server import run
    run(app)
