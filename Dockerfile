FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

# часовой пояс (нужен для корректного времени отметок)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# зависимости — отдельным слоем, чтобы правки кода не пересобирали пакеты
COPY requirements-docker.txt /app/requirements-docker.txt
RUN pip install --no-cache-dir -r /app/requirements-docker.txt

# код приложения
COPY . /app

# база, логи и сертификаты живут на томе /data
VOLUME ["/data"]

EXPOSE 5000 5001

CMD ["python", "app.py"]
