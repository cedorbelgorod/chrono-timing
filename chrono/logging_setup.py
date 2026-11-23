# -*- coding: utf-8 -*-
"""Логгеры в файлы, журнал запросов и обработчик необработанных ошибок."""
import logging, os
from logging.handlers import RotatingFileHandler
from chrono.db import (LOG_DIR)

def _make_file_logger(name, filename, fmt='%(asctime)s %(message)s', level=logging.INFO, max_bytes=5*1024*1024, backup_count=3):
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        try:
            h = RotatingFileHandler(filename, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8')
            h.setFormatter(logging.Formatter(fmt))
            logger.addHandler(h)
        except Exception:
            pass
    return logger

_access_logger = _make_file_logger('access', os.path.join(LOG_DIR, 'access.log'), fmt='%(message)s')

_app_logger = _make_file_logger('chrono', os.path.join(LOG_DIR, 'app.log'), fmt='%(asctime)s %(levelname)s %(message)s')

_swrfid_logger = _make_file_logger('swrfid', os.path.join(LOG_DIR, 'swrfid.log'), fmt='%(asctime)s %(message)s')

_NOISY_PATHS = {'/manual/active', '/auto/detail_rows', '/auto/log', '/results/details', '/finish-screen/data'}


from flask import request
from datetime import datetime

def log_request_hook(response):
    if request.path in _NOISY_PATHS or (response.status_code == 404 and request.path == '/favicon.ico'):
        return response
    try:
        _access_logger.info(f'{datetime.now().isoformat()} {request.method} {request.path} -> {response.status_code}')
    except Exception:
        pass
    return response


import traceback
from werkzeug.exceptions import HTTPException

def handle_exception_hook(e):
    if isinstance(e, HTTPException):
        return e
    tb = traceback.format_exc()
    try:
        with open(os.path.join(LOG_DIR, 'error_trace.log'), 'a', encoding='utf-8') as f:
            f.write(f'\n===== {datetime.now().isoformat()} {request.method} {request.path} =====\n')
            f.write(tb)
            f.write('\n')
    except Exception:
        pass
    _app_logger.error('Unhandled exception on %s %s', request.method, request.path, exc_info=e)
    return ('Внутренняя ошибка сервера. Подробности в файле log/error_trace.log', 500)
