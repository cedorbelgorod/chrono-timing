# Хронометраж — запуск

## Windows (как раньше, без Docker)

1. Установить Python 3.10+ (при установке отметить «Add Python to PATH»).
2. Дважды щёлкнуть `start.bat` — он создаст `.venv`, поставит зависимости и запустит сервер.
3. Открыть <http://127.0.0.1:5000>, логин/пароль по умолчанию `admin` / `admin`
   (меняются переменными окружения `CHRONO_USER` / `CHRONO_PASS`).

Все данные лежат рядом с `app.py`: `chrono.db`, `log/`, `cert/`. Резервная копия — это копия `chrono.db`.

## Docker (сервер)

```bash
# сборка
docker build -t chrono:latest .

# запуск (данные — в ./data, порты 5000/5001)
docker run -d --name chrono --restart unless-stopped \
  -p 5000:5000 -p 5001:5001 \
  -v /opt/chrono-data:/data \
  -e CHRONO_SECRET_KEY='ваш-секрет' \
  -e CHRONO_USER=admin -e CHRONO_PASS=admin \
  -e TZ=Europe/Moscow \
  chrono:latest
```

Или через compose (папка `./data` рядом с `docker-compose.yml`):

```bash
docker compose up -d --build
```

### Переменные окружения

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `CHRONO_DB` | путь к базе SQLite | `/app/chrono.db` |
| `CHRONO_LOG_DIR` | папка логов | `/app/log` |
| `CHRONO_CERT_DIR` | папка с сертификатами HTTPS | `/app/cert` |
| `CHRONO_SECRET_KEY` | ключ подписи сессий | случайный при старте |
| `CHRONO_USER`, `CHRONO_PASS` | учётка администратора | `admin` / `admin` |
| `CHRONO_SESSION_DAYS` | срок жизни сессии, дней | `7` |
| `CHRONO_COMPETITION` | название соревнования по умолчанию | `Соревнование` |
| `TZ` | часовой пояс (важно для времени отметок) | `Europe/Moscow` |

### Данные

Всё состояние — в томе `/data`:

- `competitions.json` — реестр соревнований (название, ссылка, режим «только чтение»);
- `chrono.db` — база первого (существовавшего) соревнования;
- `comps/<slug>.db` — базы остальных соревнований (создаются со страницы управления);
- `backups/` — копии баз удалённых соревнований;
- `log/` — журналы и трейсы ошибок;
- `cert/` — сертификаты для HTTPS (`shumilova.cedor.ru-crt.pem` + `-key.pem`).

Резервная копия: `cp /opt/chrono-data/comps/<slug>.db /opt/chrono-data/backup-$(date +%F).db`.
Обновление кода: `docker build -t chrono:latest . && docker rm -f chrono && docker run ...` (данные на томе сохраняются).

### Несколько соревнований

Одно соревнование = одна база SQLite. Логины и пароли у каждого соревнования свои.

- Главная страница `/` — выбор соревнования (видны названия).
- Прямая ссылка на соревнование: `/c/<slug>`, например `/c/domashnyaya-gonka`.
- Управление соревнованиями: `/manage`, учётка по умолчанию `cedor` / `cedor`
  (меняется переменными `CHRONO_MANAGE_USER` / `CHRONO_MANAGE_PASS`).
  Там же: создание пустого соревнования, удаление (с копией в `backups/`),
  включение и выключение режима «только чтение».
- «Только чтение» включается, только если в соревновании нет незавершённых стартов;
  в этом режиме просмотр разрешён, любые изменения блокируются.
