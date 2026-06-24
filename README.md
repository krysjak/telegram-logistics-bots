# Telegram Bots

Telegram-боты для управления поездками/логистикой с OCR и Google Sheets.

## `roman bot/`
Основной бот: управление поездками, логистика, OCR-распознавание, интеграция с Google Sheets.
- `README.md`, `CHANGELOG.md` — документация
- `beta.py` / `betav2.py` — рабочие версии
- `Procfile` — деплой (Heroku-стиль)

## `v1/`
Докеризированная версия бота.
- `Dockerfile`, `docker-compose.yml` — контейнеризация
- `betav2.py` — код
- `requirements.txt` — зависимости

## `bot portfolio/`
`main.py` — портфолио-бот.

**Стек:** Python, aiogram, gspread (Google Sheets), Tesseract OCR, Docker

**Статус:** ✅ Готово
