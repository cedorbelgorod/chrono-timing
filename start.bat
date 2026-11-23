@echo off
cd /d "%~dp0"
setlocal

set SWRFID_URL=https://github.com/jasoisjaso/swrfid/archive/refs/heads/main.zip

if not exist ".venv\Scripts\python.exe" (
    where python >nul 2>&1
    if errorlevel 1 (
        echo Python не найден в PATH.
        echo Установите Python 3.9 или новее и включите "Add Python to PATH".
        pause
        exit /b 1
    )
    echo Создаю виртуальное окружение...
    python -m venv .venv
)

echo Активирую окружение...
call .venv\Scripts\activate.bat

rem Базовые пакеты проверяем по импорту: если всё на месте, pip не запускается.
python -c "import flask, openpyxl, serial" >nul 2>&1
if errorlevel 1 (
    echo Устанавливаю зависимости...
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Полная установка не удалась, ставлю базовые пакеты.
        python -m pip install Flask openpyxl pyserial keyboard hid
    )
)

rem QR-коды для публичной страницы результатов.
python -c "import qrcode" >nul 2>&1
if errorlevel 1 (
    echo Устанавливаю qrcode...
    python -m pip install "qrcode>=8.0"
)

rem Протокол в PDF.
python -c "import reportlab" >nul 2>&1
if errorlevel 1 (
    echo Устанавливаю reportlab...
    python -m pip install "reportlab>=4.0"
)

rem Библиотека считывателя ставится только если её нет в окружении.
python -c "import swrfid" >nul 2>&1
if errorlevel 1 (
    echo Устанавливаю библиотеку считывателя swrfid...
    python -m pip install "swrfid @ %SWRFID_URL%"
    python -c "import swrfid" >nul 2>&1
    if errorlevel 1 (
        echo.
        echo ВНИМАНИЕ: swrfid установить не удалось.
        echo Автохронометраж по RFID работать не будет.
        echo Проверьте доступ в интернет и установите вручную:
        echo     .venv\Scripts\pip install "swrfid @ %SWRFID_URL%"
        echo.
    )
)

echo Запускаю приложение...
python app.py

pause
endlocal
