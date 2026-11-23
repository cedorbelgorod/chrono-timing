import io
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import logging
import traceback
import secrets
import hmac
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, session, send_file, send_from_directory,
    Response, jsonify, has_request_context
)
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash, check_password_hash
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

try:
    from swrfid import RFIDReader
    SWRFID_AVAILABLE = True
except ImportError:
    SWRFID_AVAILABLE = False

from chrono.utils import (
    _TOKEN_ALPHABET, _TRANSLIT, _fmt_eta_short, _fmt_hms, _fmt_start_at, _new_startlist_token,
    _normalize_gender, _parse_year, _qr_svg, _slugify, _speed_kmh, _unique_slug, format_secs,
    format_secs_short,
)

from chrono.db import (
    BACKUP_DIR, BASE_DIR, CERT_DIR, COMPS_DIR, DATA_DIR, DB_PATH, DEFAULT_COMPETITION_NAME,
    LOG_DIR, PEOPLE_DB, REGISTRY_PATH, _active_db_path, _comp_db_path, _connect,
    _create_competition_db, _create_timing_log, _ensure_registry, _get_competition,
    _migrate_competitions, _registry_find, _registry_load, _registry_norm, _registry_save,
    _rel_db_path, get_db, init_db,
)
from chrono.settings import (
    AUTH_PASSWORD, AUTH_USERNAME, ITT_DEFAULTS, PROTOCOL_DEFAULTS, RACE_FORMATS, RFID_DEFAULTS,
    _comp_format, _heat_format, _itt_interval, _reload_settings_cache_from_db, _settings_cache,
    _settings_lock, get_cached_setting, set_cached_setting,
)
from chrono.pdf import (
    _pdf_font_pair, _render_protocol_pdf, _render_startlist_pdf,
)
from chrono.timing import (
    _assign_category_gaps, _build_heat_view, _build_merged_laps, _build_races_heats,
    _build_results_detail, _comp_format, _count_races, _find_heat_by_token, _force_finish_heat,
    _heat_participant_counts, _heat_start_rows, _heat_startlist_data, _itt_interval,
    _manual_start_queue, _manual_start_state, _merge_laps, _predict_race, _process_race_finish,
    _race_lap_counts, _running_itt_heat, _start_heat, _load_auto_laps_by_race, _load_laps_by_race,
    maybe_finish_heat, prepare_race,
)

from chrono.auth import (
    MANAGE_FILE, MANAGE_PASS, MANAGE_USER, PUBLIC_ENDPOINTS, READONLY_ALLOWED, SESSION_DAYS,
    _apply_auth, _clear_competition, _current_comp, _is_read_only, _manage_creds, _manage_ok,
    _manage_password_ok, _require_admin, _safe_next, _verify_user,
)
from chrono.catalog import (
    CATEGORY_HEADER_ALIASES, HEADER_ALIASES, MAX_TAGS_PER_NUMBER, PARTICIPANT_SORTS,
    TAGS_HEADER_ALIASES, _auto_category, _cat_genders, _cat_has_conditions, _category_matches,
    _category_overlaps, _category_participants, _cats_overlap, _cell_text, _competition_meta,
    _find_header_col, _invalidate_competition_meta, _issue_number, _join_genders, _next_number,
    _norm_header, _norm_name, _parse_duration, _parse_gender_cell, _participant_by_tag,
    _participants_redirect, _plabel, _save_bib_tags, _scalar, _tags_by_number, _to_float,
    _word_matches,
)
from chrono.logging_setup import (
    _NOISY_PATHS, _access_logger, _app_logger, _make_file_logger, _swrfid_logger,
)
from chrono.manual_helpers import (
    AUTO_LOG_SIZE, LOG_SIZE, _get_manual_data, _log_timing, _push_manual_log,
)
from chrono.people import (
    PEOPLE_TTL_HOURS, PEOPLE_URL, _norm_person, _parse_people_csv, _people_auto_refresh,
    _people_db, _people_refresh, _people_search, _people_stats,
)
from chrono.registration_helpers import (
    DEFAULT_CONSENT, REG_FIELDS, REG_NUMBER_BASE, REG_RATE_LIMIT, REG_RATE_WINDOW,
    REG_WIZARD_ORDER, _next_reg_number, _parse_ua, _public_participants, _public_reg_context,
    _reg_allowed_categories, _reg_attempts, _reg_attempts_lock, _reg_clear_draft, _reg_closed,
    _reg_config, _reg_create_participant, _reg_draft, _reg_rate_ok, _reg_save_draft, _reg_status,
    _reg_step_validate, _reg_steps, _save_registration,
)
from chrono.results_helpers import (
    WMO_RU, _auto_redirect, _base_url, _build_dashboard_heats, _build_protocol, _fetch_weather,
    _finish_screen_data_response, _finish_screen_response, _geocode_city, _get_auto_rows,
    _http_json, _hub_data, _prepare_results_data, _proto_time, _public_competition, _public_url,
    _read_log_tail, _results_response, _save_proto_settings, _save_setting,
)
from chrono.rfid import (
    _auto_log, _auto_log_lock, _process_tag_scan, _push_auto_log, _swrfid_listener, _swrfid_status,
    _swrfid_status_lock, _swrfid_stop_event, _swrfid_thread, start_swrfid_listener,
)

from chrono.auth import require_auth_hook  # noqa: E402
from chrono.logging_setup import log_request_hook, handle_exception_hook  # noqa: E402
from chrono.catalog import inject_competition_hook  # noqa: E402

app = Flask(__name__)

# ======================== ПУТИ И ПАПКИ ========================
# Пути можно переопределить переменными окружения (например, в Docker — на смонтированный том)

# Папка данных: здесь лежат базы соревнований, реестр и резервные копии

# Учётка страницы управления соревнованиями (не путать с логинами соревнований)
# Смена логина/пароля управления из интерфейса: файл в каталоге данных.
# Пока файла нет — работают переменные окружения (или значения по умолчанию).


# ======================== РЕЕСТР СОРЕВНОВАНИЙ ========================
# Одно соревнование = одна база SQLite. Список баз хранится в competitions.json.


# Создаём папку для логов, если её нет
os.makedirs(LOG_DIR, exist_ok=True)

# ======================== секретный ключ ========================
_secret = os.environ.get('CHRONO_SECRET_KEY')
if not _secret:
    _secret = secrets.token_hex(32)
    print('⚠  CHRONO_SECRET_KEY не задан — сгенерирован временный.', flush=True)
app.secret_key = _secret


# ---------------------- Аутентификация (форма входа) ----------------------
app.permanent_session_lifetime = timedelta(days=SESSION_DAYS)


app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)

# За обратным прокси (Caddy) — доверяем X-Forwarded-*, чтобы ссылки были https://
if os.environ.get('CHRONO_PROXY') == '1':
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# ---- текущее соревнование и режим «только чтение» ----


# POST-запросы, которые не меняют данные соревнования

@app.before_request
def _require_auth():
    return require_auth_hook()


# ---- выбор соревнования ----


# ======================= УПРАВЛЕНИЕ СОРЕВНОВАНИЯМИ =======================
# Отдельная страница под своим паролем (по умолчанию cedor / cedor).


# ---------------- Диагностика и логи ----------------


logging.getLogger('werkzeug').setLevel(logging.WARNING)
logging.getLogger('werkzeug').propagate = False


@app.after_request
def _log_request(response):
    return log_request_hook(response)

@app.errorhandler(Exception)
def _handle_exception(e):
    return handle_exception_hook(e)

# ===================== RFID-статусы и лог =====================


# Данные для официального протокола (хранятся в settings)

# Формат старта (у соревнования один на все старты)


# Коды погоды WMO → короткое описание


# ---------------------- Журнал событий хронометража ----------------------


# ============================ Утилиты ============================


# ---------------------- Общие помощники (формат, счётчики, публичные ссылки) ----------------------


# ---------------------- Разбор заголовков Excel при импорте ----------------------


# ---- условия категорий: пол и диапазон года рождения ----


@app.context_processor
def _inject_competition():
    return inject_competition_hook()


# ==================== RFID и СКАНИРОВАНИЕ ====================


# ======================= УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ =======================


# ======================= СПИСОК РАНЕЕ РЕГИСТРИРОВАВШИХСЯ =======================
# Общий для всех соревнований кэш участников с bike.cedor.ru: подсказки при вводе ФИО.


# ======================= САМОРЕГИСТРАЦИЯ УЧАСТНИКОВ =======================
# Публичная форма для участников. Настройки хранятся в базе соревнования.


# ======================= МАРШРУТЫ =======================


# ======================== ПУБЛИЧНЫЙ ХАБ ========================


# ======================= РЕГИСТРАЦИЯ МАРШРУТОВ =======================
from chrono.views import (
    auth as _auth_views, manage as _manage_views, users as _users_views,
    registration as _registration_views, catalog as _catalog_views, races as _races_views,
    results as _results_views, auto as _auto_views, manual as _manual_views,
    public as _public_views,
)

for _rule, _endpoint, _view, _methods in _auth_views.RULES:
    app.add_url_rule(_rule, _endpoint, _view, methods=_methods)
for _rule, _endpoint, _view, _methods in _manage_views.RULES:
    app.add_url_rule(_rule, _endpoint, _view, methods=_methods)
for _rule, _endpoint, _view, _methods in _users_views.RULES:
    app.add_url_rule(_rule, _endpoint, _view, methods=_methods)
for _rule, _endpoint, _view, _methods in _registration_views.RULES:
    app.add_url_rule(_rule, _endpoint, _view, methods=_methods)
for _rule, _endpoint, _view, _methods in _catalog_views.RULES:
    app.add_url_rule(_rule, _endpoint, _view, methods=_methods)
for _rule, _endpoint, _view, _methods in _races_views.RULES:
    app.add_url_rule(_rule, _endpoint, _view, methods=_methods)
for _rule, _endpoint, _view, _methods in _results_views.RULES:
    app.add_url_rule(_rule, _endpoint, _view, methods=_methods)
for _rule, _endpoint, _view, _methods in _auto_views.RULES:
    app.add_url_rule(_rule, _endpoint, _view, methods=_methods)
for _rule, _endpoint, _view, _methods in _manual_views.RULES:
    app.add_url_rule(_rule, _endpoint, _view, methods=_methods)
for _rule, _endpoint, _view, _methods in _public_views.RULES:
    app.add_url_rule(_rule, _endpoint, _view, methods=_methods)


if __name__ == '__main__':
    from chrono.server import run
    run(app)
