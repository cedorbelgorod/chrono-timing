#!/bin/bash
# Прогон тестов в образе chrono:latest (Python 3.12 — как в проде).
# Использование: bash tools/run_tests.sh [путь-к-исходникам]
set -e
SRC="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
echo "Исходники: $SRC"
docker run --rm -v "$SRC":/src -w /src chrono:latest \
    python -m unittest discover -s tests -t . -v
