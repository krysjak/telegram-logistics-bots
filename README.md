# Telegram Logistics Bot

A feature-complete Telegram bot for **fleet / logistics management** — trip reporting, fuel accounting, vehicle maintenance, mileage tracking, oil-change reminders, and penalty recording. Built with **aiogram 3** and backed entirely by **Google Sheets** as the database. Role-based access for drivers, accountants, maintenance technicians, and admins.

> **Note:** The bot's UI language is **Ukrainian**. Code comments mix Ukrainian and Russian.

---

## ✨ Features by Role

### 👨‍✈️ Driver (`водій`)
- **📋 Trip reports** — full FSM flow: pick vehicle → enter tour number → enter fuel refill → add note → choose date/time → confirm. Written to the "Рейси" (Trips) sheet.
- **🚗 Vehicle management** — search the vehicle database, attach/detach vehicles, manual vehicle entry.
- **🔁 Double tours** — report a tour as "part 1" (0.5×) or "part 2", or a full tour (1.0×). Numbers combined as `4740/4680`.
- **📅 Day off** — register a day off instead of a trip.
- **⛽ Fuel reports** — manual entry of liters, price per liter, and check code. Written to "Пальне" sheet.
- **👤 Profile** — view/edit name, fuel card; attach/detach vehicles.
- **🚨 Report breakdown** — submit breakdown tickets with vehicle, description, and status tracking.

### 🔧 Maintenance Technician (`ТО`)
- **🔧 Record maintenance** — vehicle → mileage → work type → comment → confirm.
- **🛢️ Register oil change** — vehicle → price → liters → confirm. Resets the vehicle's oil-change mileage counter.
- **🛠️ Breakdown tickets** — view active tickets, mark done, add repair cost + comment.
- **💸 Record penalty/withholding** — select driver → amount → note → confirm.

### 💼 Accountant (`бухгалтер`)
- **View reports** — trips, fuel, maintenance, oil changes, and penalties (read-only).

### 🛡️ Admin (`адмін`)
- **🗺️ Create tours** — tour number, distance, type, note.
- **📚 Manage directories** — view vehicles, edit tour distances, edit vehicle numbers.
- **👥 Role management** — assign roles: driver, accountant, technician, admin.
- **🔔 Manual reminders** — instantly remind all drivers who haven't reported today.
- **🛢️ Oil change check** — instantly scan all vehicles and remind those overdue.
- **🧪 Reminder diagnostics** — test/diagnose the cron scheduler.

### ⏰ Automated (Cron via aiocron)
- **Oil-change check** — daily at 13:00 Kyiv time. Notifies drivers whose vehicles hit the 10,000 km threshold (24h de-dup).
- **Missing-report reminders** — hourly 13:00–18:00 Kyiv. After 5 missed reminders, auto-creates a **day-off report**.
- **Daily reset** — midnight Kyiv: clears the reminder tracker.

---

## 📊 Google Sheets Schema

Authentication via **Google Service Account** (`gspread` + `google.oauth2.service_account`). Reads are cached with `cachetools.TTLCache`; writes use `asyncio.to_thread` to stay non-blocking.

| Worksheet | Purpose |
|-----------|---------|
| Водії (Drivers) | User registry: ID, name, phone, fuel card, vehicles, role |
| Рейси (Trips) | Trip reports: driver, time, date, vehicle, fuel, tour, km |
| Тури (Tours) | Tour directory: number, distance, creator, date |
| Пальне (Fuel) | Fuel reports: datetime, driver, liters, price, check code |
| Заміна масла (Oil Change) | Oil change log: date, vehicle, technician, price, liters |
| Пробіг (Mileage) | Mileage tracking per vehicle for oil-change intervals |
| Сервісне обслуговування (Maintenance) | Maintenance log: date, vehicle, mileage, work type |
| Поломки (Breakdowns) | Breakdown tickets: date, driver, vehicle, description, status, cost |
| Утримання (Penalties) | Penalties: date, driver, amount, note |
| Автомобілі (Vehicles) | Vehicle directory: plate, fuel type, consumption, brand |

---

## 🚀 Getting Started

### Prerequisites
- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Google Cloud service account with Google Sheets API enabled

### 1. Google Sheets Setup
1. Create a Google Cloud project and a service account.
2. Download the JSON key file → rename to `credentials.json`.
3. Create a Google Sheet and share it with the service-account email (Editor access).

### 2. Configure Environment
```bash
cp .env.example .env
```

Fill in `.env`:
```env
BOT_TOKEN=your_telegram_bot_token
SECRET_CODE=your_registration_secret_code
GOOGLE_SHEET_NAME=your_google_sheet_name
ADMIN_USER_IDS=123456789,987654321
GOOGLE_CREDS_PATH=credentials.json
```

### 3. Install & Run
```bash
pip install -r roman\ bot/requirements.txt
python roman\ bot/betav2.py
```

### 4. Docker
```bash
cd v1
docker-compose up -d --build
```
> ⚠️ The `docker-compose.yml` does not inject env vars or mount credentials — you'll need to add `env_file: .env` and a volume for `credentials.json`.

---

## 🏗️ Architecture

Single-file monolith (`betav2.py`, ~3,100 lines) with one helper module:

```
roman bot/
├── betav2.py          # Everything: config, Google Sheets DAL, FSM states,
│                      #   keyboards, handlers, cron jobs, entrypoint
├── beta.py            # Older working predecessor (v1)
├── utils.py           # Standalone helpers (not imported by betav2.py)
├── requirements.txt
├── CHANGELOG.md
├── README.md
└── Procfile           # Heroku-style deploy

v1/
├── betav2.py          # Docker deployment copy
├── Dockerfile         # python:3.12-slim-bullseye
├── docker-compose.yml
└── requirements.txt
```

Key patterns: aiogram 3 `Dispatcher` + FSM, async I/O with `asyncio.to_thread` for blocking gspread calls, `TTLCache` for read caching, `aiocron` for scheduling. Long-polling worker (no webhook).

---

## 📦 Dependencies

| Package | Purpose |
|---------|---------|
| `aiogram==3.0.0` | Telegram Bot API framework |
| `gspread` | Google Sheets API client |
| `google-auth` / `google-auth-oauthlib` / `google-auth-httplib2` | Service-account auth |
| `aiocron` | Async cron scheduler (reminders) |
| `cachetools` | TTLCache for sheet-read caching |
| `python-dotenv` | `.env` loading |

> `easyocr` is listed in requirements but **not actively used** — the OCR receipt handler was removed. Fuel reports are manual-entry only.
