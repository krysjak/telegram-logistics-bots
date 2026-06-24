import asyncio
import io
import json
import logging
import re
from datetime import datetime, time, timedelta

# Импорт для Google Sheets
import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, ReplyKeyboardMarkup,
                           ReplyKeyboardRemove)

# НОВЫЙ ИМПОРТ: Добавлена библиотека для кэширования
from cachetools import TTLCache

# Импорт для планирования задач - с обработкой ошибки отсутствия модуля
try:
    import aiocron
    AIOCRON_AVAILABLE = True
    logging.info("Модуль aiocron успешно импортирован.")
except ModuleNotFoundError:
    AIOCRON_AVAILABLE = False
    logging.error("Модуль 'aiocron' не найден! Функционал напоминаний будет выключен.")
    logging.error("Для установки выполните: pip install aiocron cachetools")

# ========================================
#         НАЛАШТУВАННЯ ТА КОНСТАНТИ
# ========================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- ВАЖЛИВО! ВАШІ ДАНІ ВСТАВЛЕНО СЮДИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
SECRET_CODE = os.getenv("SECRET_CODE", "YOUR_SECRET_CODE")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "YOUR_SHEET_NAME")

# Google service account credentials — load from JSON file or env
_creds_path = os.getenv("GOOGLE_CREDS_PATH", "credentials.json")
GOOGLE_CREDS_JSON = {}
if os.path.exists(_creds_path):
    with open(_creds_path, "r") as _f:
        GOOGLE_CREDS_JSON = json.load(_f)
else:
    logging.warning("credentials.json not found — Google Sheets integration disabled")

ADMIN_USER_IDS = [int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()]
# --- Кінець секції налаштувань ---

# Назви аркушів в Google Sheets
USERS_WORKSHEET_TITLE = "Водії"
REPORTS_WORKSHEET_TITLE = "Рейси"
FUEL_REPORTS_WORKSHEET_TITLE = "Пальне"
TOURS_WORKSHEET_TITLE = "Тури"
VEHICLES_WORKSHEET_TITLE = "Автомобілі"
MILEAGE_WORKSHEET_TITLE = "Пробіг"
MAINTENANCE_WORKSHEET_TITLE = "Сервісне обслуговування"
CLARIFICATION_WORKSHEET_TITLE = "Потребує з'ясування"
BREAKDOWN_REPORTS_WORKSHEET_TITLE = "Поломки"
# НОВИЙ АРКУШ: для звітів про заміну масла
OIL_CHANGE_REPORTS_WORKSHEET_TITLE = "Заміна масла"


# Заголовки для аркушів
USERS_HEADERS = ["user_id", "first_name", "last_name", "phone_number", "fuel_card", "vehicles_json", "role"]
REPORTS_HEADERS = ["ПІБ Водія", "Час звіту", "Дата звіту", "Номер авто", "Марка авто", "Тип палива", "Норма розходу", "Номер туру", "Табель", "Паливна картка", "КМ", "Фактична заправка"]
FUEL_REPORTS_HEADERS = ["Дата та час", "ПІБ Водія", "Літри", "Ціна за літр", "Код чеку"]
TOURS_HEADERS = ["Номер туру", "Відстань км", "Ким створено", "Дата створення"]  # Базові заголовки
VEHICLES_HEADERS = ["Номер авто", "Тип палива", "Норма л/100км", "Марка авто"]
MILEAGE_HEADERS = ["Номер авто", "Загальний пробіг км", "Пробіг з останньої заміни масла км", "Дата останньої заміни масла"]
MAINTENANCE_HEADERS = ["Дата", "Номер авто", "Пробіг", "Тип робіт", "Коментар", "Виконавець"]
BREAKDOWN_REPORTS_HEADERS = ["Дата", "ПІБ Водія", "Номер авто", "Марка авто", "Опис поломки", "Статус", "Коментар ТО", "Вартість ремонту"]
# НОВІ ЗАГОЛОВКИ: для звітів про заміну масла
OIL_CHANGE_REPORTS_HEADERS = ["Дата", "Номер авто", "Виконавець (ТО)", "Ціна, грн", "Кількість, л"]


# Налаштування для нагадувань (час за Україною UTC+2)
REMINDER_START_HOUR = 11; REMINDER_MAX_COUNT = 5; REMINDER_INTERVAL = 1  # 11 UTC = 13:00 за Україною
# Додаткові налаштування для часового поясу
UKRAINE_UTC_OFFSET = 2  # Україна UTC+2
# Налаштування для обслуговування авто
OIL_CHANGE_KM = 10000
# Словник для відслідковування надісланих нагадувань
REMINDER_TRACKER = {}
# Список марок авто
CAR_BRANDS = ["Benz", "Газель", "FAW"]
# Список ролей
ROLES = ["водій", "бухгалтер", "ТО", "адмін"]

# Перевірка ключових змінних
if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE": logging.critical("ПОМИЛКА: BOT_TOKEN не заповнено!"); exit()
if GOOGLE_SHEET_NAME == "YOUR_GOOGLE_SHEET_NAME_HERE" or GOOGLE_CREDS_JSON.get("project_id") == "your-project-id": logging.critical("ПОМИЛКА: Змінні для Google Sheets не заповнено."); exit()

# Регулярні вирази
VEHICLE_NUMBER_PATTERN = re.compile(r'^\s*([A-Za-zА-Яа-я]{2})\s*(\d{4})\s*([A-Za-zА-Яа-я]{2})\s*$', re.IGNORECASE)

# Ініціалізація
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
# OCR убран

# Глобальний клієнт для Google Sheets та кеш
gsheet_client = None
user_cache = {}

# Кеш для оптимізації швидкості
tours_cache = TTLCache(maxsize=100, ttl=300)  # 5 хвилин
vehicles_cache = TTLCache(maxsize=500, ttl=180)  # 3 хвилини
reports_cache = TTLCache(maxsize=1000, ttl=60)  # 1 хвилина
all_users_cache = TTLCache(maxsize=1, ttl=300)

# ========================================
#       ДОПОМІЖНІ ФУНКЦІЇ (GOOGLE SHEETS)
# ========================================

def initialize_gsheet_client():
    """Ініціалізує клієнт gspread один раз при запуску."""
    global gsheet_client
    if gsheet_client is None:
        try:
            logging.info("Ініціалізація клієнта Google Sheets...")
            scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
            creds = Credentials.from_service_account_info(GOOGLE_CREDS_JSON, scopes=scopes)
            gsheet_client = gspread.authorize(creds)
            logging.info("Клієнт Google Sheets успішно ініціалізовано.")
        except Exception as e:
            logging.critical(f"Не вдалося ініціалізувати клієнта Google Sheets: {e}")
            gsheet_client = None
    return gsheet_client

def is_admin(user_id: int) -> bool:
    """Перевіряє, чи має користувач адміністративні права"""
    return str(user_id) in ADMIN_USER_IDS

def clear_caches(user_id: str = None):
    """Очищує кеш всіх користувачів або конкретного користувача."""
    global all_users_cache, user_cache
    all_users_cache.clear()
    if user_id and user_id in user_cache:
        del user_cache[user_id]
    logging.info(f"Кеші очищено. All users: {len(all_users_cache)}, User {user_id}: {user_id in user_cache if user_id else 'N/A'}")

async def get_gsheet_worksheet(worksheet_title: str, headers: list) -> gspread.Worksheet | None:
    """Отримує аркуш з Google таблиці, створює його з заголовками, якщо він не існує."""
    try:
        client = initialize_gsheet_client()
        if not client: return None
        spreadsheet = await asyncio.to_thread(client.open, GOOGLE_SHEET_NAME)
        try:
            worksheet = await asyncio.to_thread(spreadsheet.worksheet, worksheet_title)
            first_row = await asyncio.to_thread(worksheet.row_values, 1)
            if not first_row:
                # Тільки додаємо заголовки, якщо аркуш порожній
                await asyncio.to_thread(worksheet.append_row, headers)
                await asyncio.to_thread(worksheet.format, 'A1:Z1', {'textFormat': {'bold': True}})
                logging.info(f"Додано заголовки до порожнього аркуша '{worksheet_title}'.")
        except gspread.exceptions.WorksheetNotFound:
            worksheet = await asyncio.to_thread(spreadsheet.add_worksheet, title=worksheet_title, rows="100", cols=len(headers) + 5)
            await asyncio.to_thread(worksheet.append_row, headers)
            await asyncio.to_thread(worksheet.format, 'A1:Z1', {'textFormat': {'bold': True}})
            logging.info(f"Створено новий аркуш '{worksheet_title}' із заголовками.")
        return worksheet
    except Exception as e:
        logging.error(f"Сталася помилка при роботі з Google Sheets: {e}")
        return None

async def get_all_users_from_gsheet() -> dict:
    """Завантажує дані всіх користувачів з кешу або Google Sheets."""
    if "all_users" in all_users_cache:
        logging.info("Отримано дані всіх користувачів з кешу.")
        return all_users_cache["all_users"]

    worksheet = await get_gsheet_worksheet(USERS_WORKSHEET_TITLE, USERS_HEADERS)
    if not worksheet: return {}

    users_data = {}
    records = await asyncio.to_thread(worksheet.get_all_records, expected_headers=USERS_HEADERS)
    for record in records:
        user_id = str(record.get("user_id")) if record.get("user_id") else str(record.get("phone_number"))
        if not user_id: continue

        try: vehicles = json.loads(record.get("vehicles_json", "[]"))
        except json.JSONDecodeError: vehicles = []

        users_data[user_id] = {
            "first_name": record.get("first_name"),
            "last_name": record.get("last_name"),
            "phone_number": record.get("phone_number"),
            "fuel_card": record.get("fuel_card"),
            "vehicles": vehicles,
            "role": record.get("role", "водій")
        }
    all_users_cache["all_users"] = users_data
    return users_data

async def get_user_from_gsheet(user_id: str) -> dict | None:
    """Завантажує дані користувача з кешу або Google Sheets."""
    if user_id in user_cache:
        logging.info(f"Отримано дані користувача {user_id} з кешу.")
        return user_cache[user_id]

    logging.info(f"В кеші немає {user_id}. Завантаження з Google Sheets.")
    worksheet = await get_gsheet_worksheet(USERS_WORKSHEET_TITLE, USERS_HEADERS)
    if not worksheet: return None

    try:
        cell = await asyncio.to_thread(worksheet.find, str(user_id), in_column=1)
        if not cell: return None
        row_data = await asyncio.to_thread(worksheet.row_values, cell.row)

        user_data = {header: row_data[i] if i < len(row_data) else "" for i, header in enumerate(USERS_HEADERS)}

        try:
            user_data["vehicles"] = json.loads(user_data.get("vehicles_json", "[]"))
        except json.JSONDecodeError:
            user_data["vehicles"] = []

        if not user_data.get("role"):
            user_data["role"] = "водій"

        user_cache[user_id] = user_data
        return user_data
    except Exception as e:
        if "Unable to locate" in str(e) or "not found" in str(e).lower():
            # Користувача не знайдено
            return None
        logging.error(f"Помилка при зчитуванні даних користувача {user_id} з GSheet: {e}")
        return None

async def save_user_to_gsheet(user_id: str, user_data: dict):
    """Зберігає дані користувача і оновлює кеш."""
    worksheet = await get_gsheet_worksheet(USERS_WORKSHEET_TITLE, USERS_HEADERS)
    if not worksheet:
        logging.error(f"Не вдалося зберегти дані для користувача {user_id}: аркуш не знайдено.")
        return

    vehicles_json = json.dumps(user_data.get("vehicles", []), ensure_ascii=False)
    row_data = [
        str(user_id),
        str(user_data.get("first_name", "")),
        str(user_data.get("last_name", "")),
        str(user_data.get("phone_number", "")),
        str(user_data.get("fuel_card", "")),
        str(vehicles_json),
        str(user_data.get("role", "водій"))
    ]

    try:
        cell = await asyncio.to_thread(worksheet.find, str(user_id), in_column=1)
        if cell:
            await asyncio.to_thread(worksheet.update, f'A{cell.row}:G{cell.row}', [row_data])
            logging.info(f"Оновлено дані для користувача {user_id} в Google Sheets.")
        else:
            phone_number_to_find = str(user_data.get("phone_number", ""))
            found_match = False
            if phone_number_to_find:
                normalized_phone_to_find = re.sub(r'\D', '', phone_number_to_find)
                records = await asyncio.to_thread(worksheet.get_all_records, expected_headers=USERS_HEADERS)
                for i, record in enumerate(records):
                    db_phone = re.sub(r'\D', '', str(record.get('phone_number', '')))
                    if db_phone == normalized_phone_to_find and not str(record.get('user_id', '')).strip():
                        row_to_update = i + 2
                        await asyncio.to_thread(worksheet.update, f'A{row_to_update}:G{row_to_update}', [row_data])
                        logging.info(f"Оновлено існуючий запис (рядок {row_to_update}) для {user_id}")
                        found_match = True
                        break
            if not found_match:
                await asyncio.to_thread(worksheet.append_row, row_data)
                logging.info(f"Додано нового користувача {user_id}")
        clear_caches(user_id)
    except Exception as e:
        logging.error(f"Помилка при збереженні даних {user_id}: {e}")

async def get_users_by_role(role: str) -> list:
    users_data = await get_all_users_from_gsheet()
    # Нормалізуємо роль для пошуку (прибираємо пробіли та приводимо до нижнього регістру)
    normalized_role = role.strip().lower()
    result = []
    for uid, user in users_data.items():
        user_role = user.get("role", "").strip().lower()
        if user_role == normalized_role:
            result.append(uid)
    return result

async def check_user_by_contact_or_name(phone_number: str = None, first_name: str = None, last_name: str = None) -> dict | None:
    users_data = await get_all_users_from_gsheet()
    if phone_number:
        normalized_phone_telegram = re.sub(r'\D', '', phone_number)
        logging.info(f"Шукаю користувача за номером телефону: {normalized_phone_telegram}")
        for user_id, user_info in users_data.items():
            db_phone = re.sub(r'\D', '', str(user_info.get('phone_number', '')))
            if db_phone == normalized_phone_telegram:
                user_info["user_id"] = user_id
                return user_info
    if first_name and last_name:
        logging.info(f"Шукаю користувача за ПІБ: {first_name} {last_name}")
        for user_id, user_info in users_data.items():
            if (user_info.get('first_name', '').strip().lower() == first_name.strip().lower() and
                    user_info.get('last_name', '').strip().lower() == last_name.strip().lower()):
                user_info["user_id"] = user_id
                return user_info
    return None

async def get_vehicle_data_from_db(vehicle_number: str) -> dict:
    # Перевіряємо кеш
    cache_key = f"vehicle_{vehicle_number}"
    if cache_key in vehicles_cache:
        return vehicles_cache[cache_key]

    try:
        worksheet = await get_gsheet_worksheet(VEHICLES_WORKSHEET_TITLE, VEHICLES_HEADERS)
        if worksheet:
            cell = await asyncio.to_thread(worksheet.find, vehicle_number, in_column=1)
            if cell:
                row_data = await asyncio.to_thread(worksheet.row_values, cell.row)
                result = {"fuel_type": row_data[1] if len(row_data) > 1 else "Не вказано",
                         "consumption_rate": row_data[2] if len(row_data) > 2 else "Не вказано",
                         "vehicle_brand": row_data[3] if len(row_data) > 3 else "Не вказано"}
                # Зберігаємо в кеш
                vehicles_cache[cache_key] = result
                return result
    except Exception as e:
        logging.error(f"Помилка при пошуку даних для {vehicle_number}: {e}")
    return {}

async def get_vehicle_mileage_from_db(vehicle_number: str) -> dict:
    try:
        worksheet = await get_gsheet_worksheet(MILEAGE_WORKSHEET_TITLE, MILEAGE_HEADERS)
        if worksheet:
            cell = await asyncio.to_thread(worksheet.find, vehicle_number, in_column=1)
            if cell:
                row_data = await asyncio.to_thread(worksheet.row_values, cell.row)
                return {"total_mileage": float(str(row_data[1]).replace(',', '.')) if len(row_data) > 1 and row_data[1] else 0,
                        "mileage_since_oil_change": float(str(row_data[2]).replace(',', '.')) if len(row_data) > 2 and row_data[2] else 0,
                        "last_oil_change_date": row_data[3] if len(row_data) > 3 else None}
            else:
                 logging.info(f"Авто {vehicle_number} не знайдено в базі пробігу.")
    except Exception as e:
        logging.error(f"Критична помилка при отриманні даних пробігу для {vehicle_number}: {e}")
    return {"total_mileage": 0, "mileage_since_oil_change": 0, "last_oil_change_date": None}


async def update_vehicle_mileage(vehicle_number: str, additional_km: float):
    if additional_km <= 0: return
    logging.info(f"Спроба оновлення пробігу для {vehicle_number}: додатково {additional_km} км")
    try:
        worksheet = await get_gsheet_worksheet(MILEAGE_WORKSHEET_TITLE, MILEAGE_HEADERS)
        if not worksheet: return
        cell = await asyncio.to_thread(worksheet.find, vehicle_number, in_column=1)
        if not cell:
            row = [vehicle_number, str(additional_km), str(additional_km), ""]
            await asyncio.to_thread(worksheet.append_row, row)
            logging.info(f"Створено новий запис пробігу для авто {vehicle_number}: {additional_km} км")
            return
        row_data = await asyncio.to_thread(worksheet.row_values, cell.row)
        total_mileage = float(str(row_data[1]).replace(',', '.')) if len(row_data) > 1 and row_data[1] else 0
        mileage_since_oil_change = float(str(row_data[2]).replace(',', '.')) if len(row_data) > 2 and row_data[2] else 0
        new_total_mileage = total_mileage + additional_km
        new_mileage_since_oil = mileage_since_oil_change + additional_km

        # Перевірка, чи вже було надіслано повідомлення
        already_notified = mileage_since_oil_change >= OIL_CHANGE_KM

        await asyncio.to_thread(worksheet.update_cell, cell.row, 2, str(new_total_mileage))
        await asyncio.to_thread(worksheet.update_cell, cell.row, 3, str(new_mileage_since_oil))
        logging.info(f"Оновлено пробіг в базі для авто {vehicle_number}: загальний {new_total_mileage:.0f} км")

        # Надіслати повідомлення, тільки якщо пробіг ПЕРЕВИЩИВ поріг, а не був вже за ним
        if new_mileage_since_oil >= OIL_CHANGE_KM and not already_notified:
            asyncio.create_task(notify_oil_change_needed(vehicle_number, new_mileage_since_oil))
    except Exception as e:
        logging.error(f"Критична помилка при оновленні пробігу для {vehicle_number}: {e}")

async def notify_oil_change_needed(vehicle_number: str, mileage_since_oil_change: float):
    """Надсилає повідомлення користувачам з роллю 'ТО' про необхідність заміни масла."""
    recipients_ids = await get_users_by_role("ТО")
    if not recipients_ids:
        logging.warning(f"Не знайдено користувачів з роллю 'ТО' для сповіщення про заміну масла для {vehicle_number}")
        return

    message_text = (f"🛢️ **Увага! Потрібна заміна масла**\n\n"
                    f"Автомобіль: **{vehicle_number}**\n"
                    f"Пробіг після останньої заміни: **{mileage_since_oil_change:.0f} км** "
                    f"(норма: {OIL_CHANGE_KM} км).\n\n"
                    f"Будь ласка, зареєструйте заміну в панелі ТО.")

    for user_id in recipients_ids:
        try:
            await bot.send_message(chat_id=int(user_id), text=message_text, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Не вдалося відправити повідомлення про заміну масла користувачу {user_id}: {e}")


async def append_report_to_gsheet(report_data: dict, worksheet_title: str = REPORTS_WORKSHEET_TITLE):
    worksheet = await get_gsheet_worksheet(worksheet_title, REPORTS_HEADERS)
    if worksheet:
        full_name = f"{report_data.get('driver_first_name', '')} {report_data.get('driver_last_name', '')}".strip()
        dt_obj = datetime.strptime(report_data.get("report_time"), '%Y-%m-%d %H:%M:%S')
        row = [full_name, dt_obj.strftime('%H:%M:%S'), dt_obj.strftime('%d.%m.%Y'),
               report_data.get("vehicle_number"), report_data.get("vehicle_brand", "Не вказано"),
               report_data.get("fuel_type", "Не знайдено"), report_data.get("consumption_rate", "Не знайдено"),
               report_data.get("tour_number"), report_data.get("tabel_number", "N/A"),
               report_data.get("fuel_card", "Не вказано"), report_data.get("distance", "Не вказано"),
               report_data.get("actual_refill", "Не вказано")]
        await asyncio.to_thread(worksheet.append_row, row)
        if report_data.get("tour_number") == "Вихідний":
            all_values = await asyncio.to_thread(worksheet.get_all_values)
            last_row = len(all_values)
            await asyncio.to_thread(worksheet.format, f"H{last_row}", {"backgroundColor": {"red": 1.0, "green": 1.0, "blue": 0.0}})
        logging.info(f"Звіт для {full_name} по туру {report_data.get('tour_number')} збережено.")

async def append_fuel_report_to_gsheet(report_data: dict):
    worksheet = await get_gsheet_worksheet(FUEL_REPORTS_WORKSHEET_TITLE, FUEL_REPORTS_HEADERS)
    if worksheet:
        row = [report_data.get("report_time"), report_data.get("driver_full_name"), report_data.get("liters"), report_data.get("price_per_liter"), report_data.get("check_code")]
        await asyncio.to_thread(worksheet.append_row, [str(item) for item in row], value_input_option='USER_ENTERED')

async def get_tours_from_gsheet() -> list[dict]:
    # Перевіряємо кеш
    if "tours_list" in tours_cache:
        return tours_cache["tours_list"]

    try:
        worksheet = await get_gsheet_worksheet(TOURS_WORKSHEET_TITLE, TOURS_HEADERS)
        if worksheet:
            # Отримуємо записи без перевірки заголовків
            records = await asyncio.to_thread(worksheet.get_all_records)
            # Зберігаємо в кеш
            tours_cache["tours_list"] = records
            return records
    except Exception as e:
        logging.error(f"Помилка при отриманні турів з Google Sheets: {e}")
    return []

async def append_tour_to_gsheet(tour_data: dict):
    worksheet = await get_gsheet_worksheet(TOURS_WORKSHEET_TITLE, TOURS_HEADERS)
    if worksheet:
        try:
            cell = await asyncio.to_thread(worksheet.find, str(tour_data.get("tour_number")), in_column=1)
            if cell:
                logging.warning(f"Тур з номером {tour_data.get('tour_number')} вже існує.")
                return False
        except Exception as e:
            if "Unable to locate" not in str(e) and "not found" not in str(e).lower():
                logging.error(f"Помилка при пошуку туру: {e}")
            # Тур не знайдено, можна створювати
        row = [tour_data.get("tour_number"), tour_data.get("distance"), tour_data.get("created_by"),
               tour_data.get("created_at")]
        await asyncio.to_thread(worksheet.append_row, [str(item) for item in row], value_input_option='USER_ENTERED')
        return True
    return False

async def update_tour_distance_in_db(tour_number: str, new_distance: float) -> bool:
    worksheet = await get_gsheet_worksheet(TOURS_WORKSHEET_TITLE, TOURS_HEADERS)
    if not worksheet: return False
    try:
        cell = await asyncio.to_thread(worksheet.find, tour_number, in_column=1)
        if cell:
            await asyncio.to_thread(worksheet.update_cell, cell.row, 2, str(new_distance))
            return True
        return False
    except Exception as e:
        logging.error(f"Помилка при оновленні відстані туру №{tour_number}: {e}")
    return False

async def check_if_report_exists(tour_number: str, report_date: str) -> bool:
    try:
        worksheet = await get_gsheet_worksheet(REPORTS_WORKSHEET_TITLE, REPORTS_HEADERS)
        if not worksheet: return False
        all_records = await asyncio.to_thread(worksheet.get_all_records)
        for record in all_records:
            if str(record.get("Номер туру")) == str(tour_number) and record.get("Дата звіту") == report_date:
                return True
        return False
    except Exception as e:
        logging.error(f"Помилка при перевірці дублікатів звітів: {e}")
        return False

async def append_breakdown_report_to_gsheet(report_data: dict):
    worksheet = await get_gsheet_worksheet(BREAKDOWN_REPORTS_WORKSHEET_TITLE, BREAKDOWN_REPORTS_HEADERS)
    if worksheet:
        row = [
            datetime.now().strftime('%d.%m.%Y %H:%M'),
            report_data.get("driver_full_name"),
            report_data.get("vehicle_number"),
            report_data.get("vehicle_brand"),
            report_data.get("breakdown_description"),
            "Нова",
            "",
            ""
        ]
        await asyncio.to_thread(worksheet.append_row, row)

async def update_breakdown_report_in_gsheet(row_index, status, comment, cost):
    worksheet = await get_gsheet_worksheet(BREAKDOWN_REPORTS_WORKSHEET_TITLE, BREAKDOWN_REPORTS_HEADERS)
    if worksheet:
        await asyncio.to_thread(worksheet.update_cell, row_index, 6, status)
        await asyncio.to_thread(worksheet.update_cell, row_index, 7, comment)
        await asyncio.to_thread(worksheet.update_cell, row_index, 8, cost)
        return True
    return False

async def append_maintenance_log_to_gsheet(data: dict):
    worksheet = await get_gsheet_worksheet(MAINTENANCE_WORKSHEET_TITLE, MAINTENANCE_HEADERS)
    if worksheet:
        row = [datetime.now().strftime('%d.%m.%Y %H:%M'), data.get("vehicle_number"), data.get("mileage"), data.get("work_type"), data.get("comment"), data.get("driver_name")]
        await asyncio.to_thread(worksheet.append_row, row)

async def save_vehicle_to_gsheet(vehicle_data: dict):
    worksheet = await get_gsheet_worksheet(VEHICLES_WORKSHEET_TITLE, VEHICLES_HEADERS)
    if worksheet:
        try:
            cell = await asyncio.to_thread(worksheet.find, vehicle_data.get("number", ""), in_column=1)
            if cell:
                await asyncio.to_thread(worksheet.update_cell, cell.row, 2, vehicle_data.get("fuel_type", "Не вказано"))
                await asyncio.to_thread(worksheet.update_cell, cell.row, 3, str(vehicle_data.get("consumption_rate", "Не вказано")))
                await asyncio.to_thread(worksheet.update_cell, cell.row, 4, vehicle_data.get("brand", "Не вказано"))
            else:
                row = [vehicle_data.get("number", ""), vehicle_data.get("fuel_type", "Не вказано"), str(vehicle_data.get("consumption_rate", "Не вказано")), vehicle_data.get("brand", "Не вказано")]
                await asyncio.to_thread(worksheet.append_row, row)
        except Exception as e:
            if "Unable to locate" in str(e) or "not found" in str(e).lower():
                # Авто не знайдено, створюємо новий запис
                row = [vehicle_data.get("number", ""), vehicle_data.get("fuel_type", "Не вказано"), str(vehicle_data.get("consumption_rate", "Не вказано")), vehicle_data.get("brand", "Не вказано")]
                await asyncio.to_thread(worksheet.append_row, row)
            else:
                logging.error(f"Помилка при збереженні авто: {e}")

async def update_vehicle_number_in_gsheet(old_number: str, new_number: str) -> bool:
    worksheet = await get_gsheet_worksheet(VEHICLES_WORKSHEET_TITLE, VEHICLES_HEADERS)
    if not worksheet: return False
    try:
        cell = await asyncio.to_thread(worksheet.find, old_number, in_column=1)
        if cell:
            await asyncio.to_thread(worksheet.update_cell, cell.row, 1, new_number)
            logging.info(f"Номер авто {old_number} оновлено на {new_number}.")
            return True
        return False
    except Exception as e:
        logging.error(f"Помилка при оновленні номера авто {old_number}: {e}")
        return False

async def record_oil_change_in_db(vehicle_number: str) -> bool:
    """Скидає лічильник пробігу після заміни масла."""
    try:
        worksheet = await get_gsheet_worksheet(MILEAGE_WORKSHEET_TITLE, MILEAGE_HEADERS)
        if not worksheet: return False
        cell = await asyncio.to_thread(worksheet.find, vehicle_number, in_column=1)
        if cell:
            await asyncio.to_thread(worksheet.update_cell, cell.row, 3, "0") # Скидання лічильника
            await asyncio.to_thread(worksheet.update_cell, cell.row, 4, datetime.now().strftime('%d.%m.%Y')) # Оновлення дати
            logging.info(f"Лічильник пробігу для {vehicle_number} скинуто.")
            return True
        return False
    except Exception as e:
        logging.error(f"Помилка при записі заміни масла для {vehicle_number} в Google Sheets: {e}")
        return False

# НОВА ФУНКЦІЯ: Збереження звіту про заміну масла
async def log_oil_change_to_gsheet(data: dict):
    """Зберігає звіт про заміну масла в відповідний аркуш."""
    worksheet = await get_gsheet_worksheet(OIL_CHANGE_REPORTS_WORKSHEET_TITLE, OIL_CHANGE_REPORTS_HEADERS)
    if worksheet:
        row = [
            datetime.now().strftime('%d.%m.%Y %H:%M'),
            data.get("vehicle_number"),
            data.get("executor_name"),
            data.get("price"),
            data.get("liters")
        ]
        await asyncio.to_thread(worksheet.append_row, [str(item) for item in row], value_input_option='USER_ENTERED')
        logging.info(f"Збережено звіт про заміну масла для {data.get('vehicle_number')}")


def parse_receipt_text(text: str) -> dict or None:
    try:
        liters, price, check_code = None, None, None
        normalization_map = {'E': 'Е', 'K': 'К', 'H': 'Н', 'C': 'С', 'M': 'М', 'B': 'В', 'e': 'е', 'k': 'к', 'h': 'н', 'c': 'с', 'm': 'м', 'b': 'в', 'a': 'а', 'o': 'о', 'p': 'р', 'i': 'і', 'x': 'х', 'y': 'у'}
        text_upper = text.upper()
        for lat, cyr in normalization_map.items(): text_upper = text_upper.replace(lat.upper(), cyr.upper())
        lines = text_upper.split('\n')
        fuel_pattern = re.compile(r'(\d+[\.,]\d{2,3})\s*(?:Л|1)?\s*[\*Х>]\s*(\d+[\.,]\d{2})')
        check_keyword_pattern = re.compile(r'(?:ЧЕК|МЕК|НЕК|ЦЕК)')
        check_code_pattern = re.compile(r'(\d{8,10})')
        for line in lines:
            if not liters and not price:
                fuel_match = fuel_pattern.search(line)
                if fuel_match: liters = float(fuel_match.group(1).replace(',', '.')); price = float(fuel_match.group(2).replace(',', '.'))
            if not check_code and check_keyword_pattern.search(line):
                code_match = check_code_pattern.search(line)
                if code_match: check_code = code_match.group(1)
        if all([liters, price, check_code]): return {"liters": liters, "price_per_liter": price, "check_code": check_code}
        return None
    except Exception: return None

async def safe_callback_answer(callback: types.CallbackQuery, text: str = None, show_alert: bool = False):
    try: await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest: pass
async def safe_edit_message(callback: types.CallbackQuery, text: str, **kwargs):
    try: await callback.message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): logging.error(f"Error editing message: {e}")

# ========================================
#         СТАНИ FSM
# ========================================
class Registration(StatesGroup):
    waiting_for_phone = State()
    waiting_for_name = State()
    adding_vehicle_prompt = State()
    adding_vehicle_number = State()
    adding_vehicle_brand = State()
    adding_fuel_type = State()
    adding_consumption_rate = State()

class ProfileManagement(StatesGroup):
    viewing_profile = State(); editing_first_name = State(); editing_last_name = State(); editing_fuel_card = State()
class TourManagement(StatesGroup):
    creating_tour_number = State(); creating_tour_distance = State(); viewing_tours = State()
    creating_tour_type = State()  # Стан для типу туру
class Reporting(StatesGroup):
    waiting_for_vehicle_choice = State(); manual_vehicle_number = State()
    manual_brand_choice = State(); manual_brand_input = State(); manual_fuel_type = State(); manual_consumption_rate = State()
    waiting_for_tour_number = State(); waiting_for_duplicate_confirmation = State()
    waiting_for_actual_refill = State()  # Только фактическая заправка
    waiting_for_datetime_choice = State(); waiting_for_trip_confirmation = State()
    searching_vehicles = State(); waiting_for_note = State()  # Додано стан для примітки
class FuelReport(StatesGroup):
    waiting_for_receipt_photo = State(); waiting_for_manual_liters = State(); waiting_for_manual_price = State()
    waiting_for_manual_check_code = State(); waiting_for_edit_or_confirm = State(); waiting_for_edit_liters = State()
    waiting_for_edit_price = State(); waiting_for_edit_check_code = State()

# НОВІ СТАНИ: для реєстрації заміни масла співробітником ТО
class OilChangeByTO(StatesGroup):
    selecting_vehicle = State()
    entering_price = State()
    entering_liters = State()
    confirming = State()

class Maintenance(StatesGroup):
    selecting_vehicle = State(); entering_mileage = State(); entering_work_type = State(); entering_comment = State(); confirming = State()
class BreakdownReport(StatesGroup):
    select_vehicle = State()
    enter_description = State()
class TechnicianPanel(StatesGroup):
    main_menu = State()
    viewing_breakdowns = State()
    manage_breakdown = State()
    enter_comment = State()
    enter_cost = State()
class AdminPanel(StatesGroup):
    main_menu = State(); directories_menu = State(); manage_tours_menu = State(); editing_tour_distance = State()
    manage_roles = State()
    select_user_for_role = State()
    select_new_role = State()
    editing_vehicle_number_select = State()
    editing_vehicle_number_new = State()
class AccountantPanel(StatesGroup):
    main_menu = State()
    viewing_reports = State()
    viewing_fuel_reports = State()
    viewing_maintenance_reports = State()
    # НОВИЙ СТАН: для перегляду звітів про заміну масла
    viewing_oil_change_reports = State()


# ========================================
#         КЛАВІАТУРИ
# ========================================

CANCEL_BUTTON_INLINE = InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_action")
CANCEL_KEYBOARD_REPLY = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True)

def get_main_menu_keyboard(user_role: str) -> ReplyKeyboardMarkup:
    keyboard = []
    common_buttons = [KeyboardButton(text="👤 Профіль")]

    if user_role.strip().lower() == "водій":
        keyboard.extend([
            [KeyboardButton(text="📋 Надіслати звіт про рейс")]
        ])
    elif user_role == "ТО":
        keyboard.append([KeyboardButton(text="🔧 Панель ТО")])
    elif user_role == "бухгалтер":
        keyboard.append([KeyboardButton(text="💰 Панель бухгалтера")])
    elif user_role == "адмін":
        keyboard.extend([
            [KeyboardButton(text="📋 Надіслати звіт про рейс")],
            [KeyboardButton(text="🔧 Панель ТО")],
            [KeyboardButton(text="💰 Панель бухгалтера")],
            [KeyboardButton(text="👑 Панель адміністратора")]
        ])

    keyboard.append(common_buttons)
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)

def get_phone_request_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поділитися номером", request_contact=True)]],
        resize_keyboard=True,
    )

def get_admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗺️ Створити новий тур", callback_data="admin_create_tour")],
        [InlineKeyboardButton(text="📚 Керування довідниками", callback_data="admin_manage_directories")],
        [InlineKeyboardButton(text="👥 Управління ролями", callback_data="admin_manage_roles")],
        [InlineKeyboardButton(text="🔔 Нагадування про звіти", callback_data="admin_send_reminders")],
        [InlineKeyboardButton(text="🧪 Тест нагадувань", callback_data="admin_test_reminders")],
        [InlineKeyboardButton(text="🛢️ Перевірка заміни масла", callback_data="admin_check_oil")],
        [InlineKeyboardButton(text="↩️ Назад до головного меню", callback_data="back_to_main_menu")]
    ])

def get_accountant_keyboard() -> InlineKeyboardMarkup:
    # ОНОВЛЕНО: Додано кнопку для перегляду звітів про заміну масла
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗒️ Переглянути звіти по рейсах", callback_data="acc_view_trip_reports")],
        [InlineKeyboardButton(text="⛽️ Переглянути звіти по пальному", callback_data="acc_view_fuel_reports")],
        [InlineKeyboardButton(text="🛠️ Переглянути звіти по ремонтах", callback_data="acc_view_maintenance_reports")],
        [InlineKeyboardButton(text="🛢️ Переглянути звіти по заміні масла", callback_data="acc_view_oil_reports")],
        [InlineKeyboardButton(text="↩️ Назад до головного меню", callback_data="back_to_main_menu")]
    ])

def get_directories_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚗 Переглянути авто", callback_data="dir_view_vehicles")],
        [InlineKeyboardButton(text="🗺️ Керування турами", callback_data="dir_manage_tours")],
        [InlineKeyboardButton(text="✏️ Редагувати номери авто", callback_data="admin_edit_vehicle_number")],
        [InlineKeyboardButton(text="↩️ Назад до адмін-панелі", callback_data="back_to_admin_panel")]
    ])

def get_car_brand_keyboard() -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=brand, callback_data=f"brand_{brand}") for brand in CAR_BRANDS]
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    keyboard.append([InlineKeyboardButton(text="➡️ Інша марка (ввести вручну)", callback_data="brand_manual_brand")])
    keyboard.append([CANCEL_BUTTON_INLINE])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_fuel_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Бензин", callback_data="fuel_type_Бензин"), InlineKeyboardButton(text="Газ", callback_data="fuel_type_Газ")],
        [InlineKeyboardButton(text="Дизель", callback_data="fuel_type_Дизель"), InlineKeyboardButton(text="Бензин/Газ", callback_data="fuel_type_Бензин/Газ")],
        [InlineKeyboardButton(text="➡️ Інший (ввести вручну)", callback_data="fuel_type_manual")],
        [CANCEL_BUTTON_INLINE]
    ])

def get_tour_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚛 Звичайний тур", callback_data="tour_type_звичайний")],
        [InlineKeyboardButton(text="🔄 Подвійний тур (1-ша частина)", callback_data="tour_type_подвійний_1")],
        [InlineKeyboardButton(text="🔄 Подвійний тур (2-га частина)", callback_data="tour_type_подвійний_2")],
        [CANCEL_BUTTON_INLINE]
    ])

def get_user_list_keyboard(users: dict) -> InlineKeyboardMarkup:
    buttons = []
    for user_id, user_info in users.items():
        buttons.append([InlineKeyboardButton(text=f"{user_info.get('first_name')} {user_info.get('last_name')} ({user_info.get('role', 'водій')})", callback_data=f"select_user_{user_id}")])
    buttons.append([InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_role_selection_keyboard(current_role: str) -> InlineKeyboardMarkup:
    buttons = []
    for role in ROLES:
        if role == current_role: continue
        buttons.append(InlineKeyboardButton(text=role.capitalize(), callback_data=f"set_role_{role}"))
    keyboard = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    keyboard.append([CANCEL_BUTTON_INLINE])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_datetime_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕒 використати поточний час", callback_data="dt_current_time")],
        [CANCEL_BUTTON_INLINE]
    ])

def get_trip_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Так, зберегти", callback_data="confirm_trip_report")],
        [InlineKeyboardButton(text="✏️ Авто", callback_data="edit_trip_vehicle"),
         InlineKeyboardButton(text="✏️ Номер туру", callback_data="edit_trip_tour_number")],
        [InlineKeyboardButton(text="✏️ Факт. заправка", callback_data="edit_trip_actual_refill"),
         InlineKeyboardButton(text="✏️ Час", callback_data="edit_trip_datetime")],
        [InlineKeyboardButton(text="✏️ Примітка", callback_data="edit_trip_note")],
        [InlineKeyboardButton(text="❌ Скасувати звіт", callback_data="cancel_action")]
    ])

def get_fuel_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Так, зберегти", callback_data="confirm_fuel_report")],
        [InlineKeyboardButton(text="✏️ Літри", callback_data="edit_fuel_liters"), InlineKeyboardButton(text="✏️ Ціна", callback_data="edit_fuel_price"), InlineKeyboardButton(text="✏️ Код чеку", callback_data="edit_fuel_check_code")],
        [InlineKeyboardButton(text="❌ Скасувати операцію", callback_data="cancel_action")]
    ])

async def get_tour_selection_keyboard() -> InlineKeyboardMarkup:
    tours = await get_tours_from_gsheet()
    buttons = []
    for tour in tours[:10]:
        tour_text = f"№{tour['Номер туру']} ({tour['Відстань км']} км)"
        buttons.append([InlineKeyboardButton(text=tour_text, callback_data=f"select_tour_{tour['Номер туру']}")])
    buttons.append([InlineKeyboardButton(text="✏️ Ввести тур вручну", callback_data="manual_tour_entry")])
    buttons.append([InlineKeyboardButton(text="🎉 Вихідний", callback_data="tour_day_off")])
    buttons.append([CANCEL_BUTTON_INLINE])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_vehicle_selection_keyboard(user_vehicles: list, user_role: str = "водій") -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=f"🚙 {v['brand']} ({v['number']})", callback_data=f"select_vehicle_{v['number']}")] for v in user_vehicles] if user_vehicles else []

    # Тільки адмін та ТО можуть додавати нові авто
    if user_role in ["адмін", "ТО"]:
        buttons.extend([
            [InlineKeyboardButton(text="➕ Ввести авто вручну", callback_data="manual_vehicle_entry")],
            [InlineKeyboardButton(text="🔍 Знайти в базі", callback_data="database_vehicle_lookup")],
            [CANCEL_BUTTON_INLINE]
        ])
    else:
        # Інші ролі можуть тільки шукати в базі
        buttons.extend([
            [InlineKeyboardButton(text="🔍 Знайти в базі", callback_data="database_vehicle_lookup")],
            [CANCEL_BUTTON_INLINE]
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_found_vehicles_keyboard(found_vehicles: list, is_breakdown_report: bool = False) -> InlineKeyboardMarkup:
    buttons = []
    if not found_vehicles:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Авто не знайдено. Спробуйте ще.", callback_data="no_vehicles_found")]])

    for vehicle in found_vehicles:
        callback_data = f"found_vehicle_{vehicle['Номер авто']}"
        if is_breakdown_report:
            callback_data = f"report_breakdown_{vehicle['Номер авто']}"
        buttons.append([InlineKeyboardButton(text=f"🚙 {vehicle['Марка авто']} ({vehicle['Номер авто']})", callback_data=callback_data)])

    buttons.append([InlineKeyboardButton(text="↩️ Скасувати пошук", callback_data="cancel_search")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def get_editable_vehicles_keyboard() -> InlineKeyboardMarkup:
    worksheet = await get_gsheet_worksheet(VEHICLES_WORKSHEET_TITLE, VEHICLES_HEADERS)
    if not worksheet:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Не вдалося отримати список авто", callback_data="back_to_directories_menu")]])

    records = await asyncio.to_thread(worksheet.get_all_records, expected_headers=VEHICLES_HEADERS)
    buttons = [[InlineKeyboardButton(text=f"🚙 {r.get('Марка авто')} ({r.get('Номер авто')})", callback_data=f"edit_veh_num_{r.get('Номер авто')}")] for r in records]
    buttons.append([InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_directories_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def get_report_breakdown_keyboard(user_vehicles) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=f"🚙 {v['brand']} ({v['number']})", callback_data=f"report_breakdown_{v['number']}")] for v in user_vehicles]
    buttons.append([InlineKeyboardButton(text="↩️ Назад до профілю", callback_data="back_to_profile")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def get_technician_keyboard() -> InlineKeyboardMarkup:
    # ОНОВЛЕНО: Замінено кнопку "Заміна масла" на "Зареєструвати заміну масла"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔧 Записати ТО", callback_data="technician_record_maintenance")],
        [InlineKeyboardButton(text="🛢️ Зареєструвати заміну масла", callback_data="to_register_oil_change")],
        [InlineKeyboardButton(text="🛠️ Тікети з поломками", callback_data="technician_view_breakdowns")],
        [InlineKeyboardButton(text="↩️ Назад до головного меню", callback_data="back_to_main_menu")]
    ])

async def get_breakdown_tickets_keyboard(tickets) -> InlineKeyboardMarkup:
    buttons = []
    if not tickets:
        buttons.append([InlineKeyboardButton(text="✅ Активних тікетів немає", callback_data="dummy")])
    for i, ticket in enumerate(tickets):
        buttons.append([InlineKeyboardButton(text=f"№{i+1}: {ticket.get('Опис поломки')} ({ticket.get('Номер авто')})", callback_data=f"manage_ticket_{i+2}")])
    buttons.append([InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_technician_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ========================================
#         ГОЛОВНІ ОБРОБНИКИ
# ========================================

async def show_main_menu(message: types.Message, state: FSMContext, user_id: str):
    """Очищує стан і показує головне меню відповідно до ролі користувача."""
    await state.clear()
    user_data = await get_user_from_gsheet(user_id)
    user_role = user_data.get("role", "водій") if user_data else "водій"
    await message.answer("Ви у головному меню. Оберіть опцію:", reply_markup=get_main_menu_keyboard(user_role))

@dp.message(F.text == "❌ Скасувати", StateFilter("*"))
@dp.callback_query(F.data == "cancel_action", StateFilter("*"))
async def universal_cancel_handler(query_or_message: types.CallbackQuery | types.Message, state: FSMContext):
    """Універсальний обробник для скасування будь-якої дії."""
    current_state = await state.get_state()
    if current_state is None:
        if isinstance(query_or_message, types.CallbackQuery):
            await safe_callback_answer(query_or_message)
        return

    logging.info(f"Скасовано дію зі стану: {current_state} користувачем {query_or_message.from_user.id}")

    if isinstance(query_or_message, types.CallbackQuery):
        message = query_or_message.message
        await safe_callback_answer(query_or_message)
        try:
            await message.edit_text("Дію скасовано.", reply_markup=None)
        except TelegramBadRequest:
            pass
    else:
        message = query_or_message
        await message.answer("Дію скасовано.", reply_markup=ReplyKeyboardRemove())

    await show_main_menu(message, state, str(query_or_message.from_user.id))

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = str(message.from_user.id)
    user_data = await get_user_from_gsheet(user_id)

    if user_data and user_data.get("first_name"):
        await message.answer(f"З поверненням, {user_data.get('first_name', 'Друже')}! 👋")
        await show_main_menu(message, state, user_id)
    else:
        await message.answer("👋 Вітаю! Будь ласка, поділіться своїм номером телефону, щоб увійти.",
                             reply_markup=get_phone_request_keyboard())
        await state.set_state(Registration.waiting_for_phone)

@dp.callback_query(F.data == "back_to_main_menu")
async def back_to_main_menu_callback(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await show_main_menu(callback.message, state, str(callback.from_user.id))

# ========================================
#         БЛОК РЕЄСТРАЦІЇ
# ========================================
@dp.message(Registration.waiting_for_phone, F.contact)
async def process_phone(message: types.Message, state: FSMContext):
    phone_number = message.contact.phone_number
    await state.update_data(phone_number=phone_number)

    user_info_from_db = await check_user_by_contact_or_name(phone_number=phone_number)

    if user_info_from_db:
        user_data = {
            "first_name": user_info_from_db.get('first_name'), "last_name": user_info_from_db.get('last_name'),
            "phone_number": user_info_from_db.get('phone_number'), "fuel_card": user_info_from_db.get('fuel_card'),
            "vehicles": user_info_from_db.get('vehicles', []), "role": user_info_from_db.get('role', 'водій')
        }
        await save_user_to_gsheet(str(message.from_user.id), user_data)
        await message.answer(
            f"✅ Знайдено ваш обліковий запис, {user_data.get('first_name', 'Друже')}! Вхід виконано.",
            reply_markup=ReplyKeyboardRemove()
        )
        await show_main_menu(message, state, str(message.from_user.id))
    else:
        await message.answer("❌ Обліковий запис з цим номером не знайдено. Спробуємо знайти за ПІБ. Будь ласка, введіть ваше повне ім'я та прізвище.",
                             reply_markup=CANCEL_KEYBOARD_REPLY)
        await state.set_state(Registration.waiting_for_name)

@dp.message(Registration.waiting_for_name)
async def process_name_for_search(message: types.Message, state: FSMContext):
    name_parts = message.text.strip().split()
    if len(name_parts) < 2:
        await message.answer("Будь ласка, введіть повне ім'я та прізвище.", reply_markup=CANCEL_KEYBOARD_REPLY)
        return

    first_name, last_name = name_parts[0], " ".join(name_parts[1:])
    user_info_from_db = await check_user_by_contact_or_name(first_name=first_name, last_name=last_name)

    if user_info_from_db:
        user_data = {
            "first_name": user_info_from_db.get('first_name'), "last_name": user_info_from_db.get('last_name'),
            "phone_number": user_info_from_db.get('phone_number'), "fuel_card": user_info_from_db.get('fuel_card'),
            "vehicles": user_info_from_db.get('vehicles', []), "role": user_info_from_db.get('role', 'водій')
        }
        await save_user_to_gsheet(str(message.from_user.id), user_data)
        await message.answer(
            f"✅ Знайдено ваш обліковий запис, {user_data.get('first_name', 'Друже')}! Вхід виконано.",
            reply_markup=ReplyKeyboardRemove()
        )
        await show_main_menu(message, state, str(message.from_user.id))
    else:
        await message.answer("❌ Вашого облікового запису не знайдено. Будь ласка, зверніться до адміністратора.")
        await state.clear()

@dp.callback_query(F.data == "add_vehicle_no", Registration.adding_vehicle_prompt)
async def prompt_add_vehicle_no(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "Добре, ви зможете додати авто пізніше.")
    await show_main_menu(callback.message, state, str(callback.from_user.id))

@dp.callback_query(F.data == "add_vehicle_yes")
async def prompt_add_vehicle_yes(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)

    # Перевіряємо роль користувача
    user_data = await get_user_from_gsheet(str(callback.from_user.id))
    if user_data and user_data.get("role") not in ["адмін", "ТО"]:
        await safe_edit_message(callback, "❌ Тільки адміністратори та ТО можуть додавати автомобілі.")
        return

    await safe_edit_message(callback, "<b>Крок 1/4:</b> Введіть номерний знак (`BC 1234 AA`)", parse_mode="HTML")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(Registration.adding_vehicle_number)

@dp.message(Registration.adding_vehicle_number)
async def process_add_vehicle_number(message: types.Message, state: FSMContext):
    match = VEHICLE_NUMBER_PATTERN.search(message.text)
    if not match:
        await message.answer("❗️ Невірний формат. Введіть `BC 1234 AA`.", reply_markup=CANCEL_KEYBOARD_REPLY)
        return
    vehicle_number = f"{match.group(1).upper()}{match.group(2)}{match.group(3).upper()}"
    await state.update_data(reg_vehicle_number=vehicle_number)
    await message.answer("<b>Крок 2/4:</b> Оберіть марку та модель.", parse_mode="HTML", reply_markup=get_car_brand_keyboard())
    await state.set_state(Registration.adding_vehicle_brand)

@dp.callback_query(F.data.startswith("brand_"), Registration.adding_vehicle_brand)
async def process_add_vehicle_brand_callback(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    brand = callback.data.split("brand_")[1]
    if brand == "manual_brand":
        await safe_edit_message(callback, "Введіть марку авто вручну:")
        await state.set_state(Registration.adding_vehicle_brand)
    else:
        await state.update_data(reg_vehicle_brand=brand)
        await safe_edit_message(callback, f"Обрано марку: {brand}")
        await callback.message.answer("<b>Крок 3/4:</b> Оберіть тип палива:", parse_mode="HTML", reply_markup=get_fuel_type_keyboard())
        await state.set_state(Registration.adding_fuel_type)

@dp.message(Registration.adding_vehicle_brand)
async def process_add_vehicle_brand_manual(message: types.Message, state: FSMContext):
    await state.update_data(reg_vehicle_brand=message.text)
    await message.answer("<b>Крок 3/4:</b> Оберіть тип палива:", parse_mode="HTML", reply_markup=get_fuel_type_keyboard())
    await state.set_state(Registration.adding_fuel_type)

@dp.callback_query(F.data.startswith("fuel_type_"), Registration.adding_fuel_type)
async def process_add_fuel_type_callback(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    fuel_type = callback.data.split("fuel_type_")[1]
    if fuel_type == "manual":
        await safe_edit_message(callback, "Введіть тип палива вручну:")
    else:
        await state.update_data(reg_fuel_type=fuel_type)
        await safe_edit_message(callback, f"Обрано тип палива: {fuel_type}")
        await callback.message.answer("<b>Крок 4/4:</b> Введіть норму розходу л/100км.", parse_mode="HTML", reply_markup=CANCEL_KEYBOARD_REPLY)
        await state.set_state(Registration.adding_consumption_rate)

@dp.message(Registration.adding_fuel_type)
async def process_add_fuel_type_manual(message: types.Message, state: FSMContext):
    await state.update_data(reg_fuel_type=message.text)
    await message.answer("<b>Крок 4/4:</b> Введіть норму розходу л/100км.", parse_mode="HTML", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(Registration.adding_consumption_rate)

@dp.message(Registration.adding_consumption_rate)
async def process_add_consumption_rate(message: types.Message, state: FSMContext):
    try:
        rate = float(message.text.replace(',', '.'))
        if rate <= 0:
            await message.answer("❗️ Норма витрати має бути більше 0. Введіть ще раз:", reply_markup=CANCEL_KEYBOARD_REPLY)
            return

        await state.update_data(reg_consumption_rate=rate)
        fsm_data = await state.get_data()
        new_vehicle = {"number": fsm_data.get("reg_vehicle_number"), "brand": fsm_data.get("reg_vehicle_brand"),
                       "fuel_type": fsm_data.get("reg_fuel_type"), "consumption_rate": fsm_data.get("reg_consumption_rate")}

        user_id = str(message.from_user.id)
        user_data = await get_user_from_gsheet(user_id)
        if user_data:
            user_data.setdefault("vehicles", []).append(new_vehicle)
            await save_user_to_gsheet(user_id, user_data)
            await save_vehicle_to_gsheet(new_vehicle)
            await message.answer(f"✅ Авто <b>{new_vehicle['brand']} ({new_vehicle['number']})</b> додано!\n\nДодати ще один?", parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Так", callback_data="add_vehicle_yes")], [InlineKeyboardButton(text="Ні", callback_data="add_vehicle_no")]]))
            await state.set_state(Registration.adding_vehicle_prompt)
        else:
            await message.answer("Помилка, профіль не знайдено. /start")
            await state.clear()
    except (ValueError, TypeError):
        await message.answer("❗️Невірний формат, введіть число.", reply_markup=CANCEL_KEYBOARD_REPLY)

# ========================================
#         БЛОК ЗВІТІВ ПРО РЕЙС
# ========================================
async def show_trip_data_for_confirmation(message_or_callback: types.Message | types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    try: time_str = datetime.strptime(data.get("report_time"), '%Y-%m-%d %H:%M:%S').strftime('%d.%m.%Y %H:%M')
    except (TypeError, ValueError): time_str = "не вказано"

    note = data.get('note', '')
    note_text = f"📝 **Примітка**: `{note}`\n" if note else "📝 **Примітка**: немає\n"

    text = (f"**Перевірте фінальний звіт:**\n\n"
            f"🚚 **Марка**: `{data.get('vehicle_brand', 'не вказано')}`\n"
            f"🔢 **Номер**: `{data.get('vehicle_number', 'не вказано')}`\n"
            f"💧 **Паливо**: `{data.get('fuel_type', 'не знайдено')}`\n"
            f"📈 **Норма**: `{data.get('consumption_rate', 'не знайдено')}` л/100км\n"
            f"🔄 **Тур**: `{data.get('tour_number', 'не вказано')}`\n"
            f"🛢️ **Факт**: `{data.get('actual_refill', 'не вказано')}` л\n"
            f"🕒 **Час**: `{time_str}`\n"
            f"📋 **Табель**: `{data.get('tabel_number', 'N/A')}`\n"
            f"{note_text}\n"
            f"Все вірно? Можна змінити будь-яке поле.")
    keyboard = get_trip_edit_keyboard()
    if isinstance(message_or_callback, types.CallbackQuery): await safe_edit_message(message_or_callback, text, reply_markup=keyboard, parse_mode="Markdown")
    else: await message_or_callback.answer(text, reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(Reporting.waiting_for_trip_confirmation)

async def go_to_tour_number_step(message_or_callback: types.Message | types.CallbackQuery, state: FSMContext):
    text = "**Оберіть тур/маршрут** або введіть вручну:"
    keyboard = await get_tour_selection_keyboard()
    if isinstance(message_or_callback, types.Message): await message_or_callback.answer(text, parse_mode="Markdown", reply_markup=keyboard)
    else: await safe_edit_message(message_or_callback, text, parse_mode="Markdown", reply_markup=keyboard)
    await state.set_state(Reporting.waiting_for_tour_number)

async def check_tour_and_proceed(tour_number: str, source: types.Message | types.CallbackQuery, state: FSMContext):
    today_str = datetime.now().strftime('%d.%m.%Y')
    if await check_if_report_exists(tour_number, today_str):
        await state.update_data(tour_number_to_confirm=tour_number)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚠️ Все одно надіслати", callback_data="force_submit_report")], [InlineKeyboardButton(text="❌ Обрати інший тур", callback_data="cancel_duplicate_report")]])
        msg_text = f"❗️ **Увага!** Звіт по туру **№{tour_number}** на сьогодні вже існує.\nНадіслати ще один (на з'ясування)?"
        if isinstance(source, types.CallbackQuery): await safe_edit_message(source, msg_text, reply_markup=keyboard, parse_mode="Markdown")
        else: await source.answer(msg_text, reply_markup=keyboard, parse_mode="Markdown")
        await state.set_state(Reporting.waiting_for_duplicate_confirmation)
    else:
        await process_tour_selection_logic(tour_number, source, state)

@dp.message(F.text == "📋 Надіслати звіт про рейс")
async def start_trip_report(message: types.Message, state: FSMContext):
    await state.clear()
    user_data = await get_user_from_gsheet(str(message.from_user.id))
    if not user_data: await message.answer("Будь ласка, зареєструйтесь /start."); return
    user_vehicles = user_data.get("vehicles", [])
    user_role = user_data.get("role", "водій")
    await message.answer("Оберіть автомобіль для звіту:", reply_markup=get_vehicle_selection_keyboard(user_vehicles, user_role))
    await state.set_state(Reporting.waiting_for_vehicle_choice)

@dp.callback_query(F.data.startswith("select_vehicle_"), Reporting.waiting_for_vehicle_choice)
async def process_registered_vehicle_choice(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    vehicle_number = callback.data.split("select_vehicle_")[1]
    user_data = await get_user_from_gsheet(str(callback.from_user.id))
    if user_data:
        selected_vehicle = next((v for v in user_data.get("vehicles", []) if v["number"] == vehicle_number), None)
        if selected_vehicle:
            await state.update_data(vehicle_number=selected_vehicle.get("number"), vehicle_brand=selected_vehicle.get("brand"), fuel_type=selected_vehicle.get("fuel_type"), consumption_rate=selected_vehicle.get("consumption_rate"))
            await safe_edit_message(callback, f"Обрано: <b>{selected_vehicle['brand']} ({selected_vehicle['number']})</b>.", parse_mode="HTML")
            await go_to_tour_number_step(callback, state)
        else: await safe_edit_message(callback, "Помилка. Авто не знайдено."); await state.clear()
    else: await safe_edit_message(callback, "Помилка завантаження профілю."); await state.clear()

@dp.callback_query(F.data == "manual_vehicle_entry", Reporting.waiting_for_vehicle_choice)
async def process_manual_vehicle_choice(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)

    # Перевіряємо роль користувача
    user_data = await get_user_from_gsheet(str(callback.from_user.id))
    if user_data and user_data.get("role") not in ["адмін", "ТО"]:
        await safe_edit_message(callback, "❌ Тільки адміністратори та ТО можуть додавати нові авто.")
        return

    await safe_edit_message(callback, "<b>Введіть номер авто</b> (`BC 1234 AA`):", parse_mode="HTML")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(Reporting.manual_vehicle_number)

@dp.callback_query(F.data == "database_vehicle_lookup", Reporting.waiting_for_vehicle_choice)
async def process_database_vehicle_choice(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "🔍 Введіть номер авто для пошуку. Дозволяється вводити лише цифри. Наприклад, `1234`:")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(Reporting.searching_vehicles)

@dp.message(Reporting.searching_vehicles)
async def process_searching_vehicles(message: types.Message, state: FSMContext):
    query = re.sub(r'[^0-9]', '', message.text)
    if not query:
        await message.answer("Будь ласка, введіть хоча б одну цифру для пошуку.", reply_markup=get_found_vehicles_keyboard([]))
        return

    worksheet = await get_gsheet_worksheet(VEHICLES_WORKSHEET_TITLE, VEHICLES_HEADERS)
    if not worksheet:
        await message.answer("❌ Не вдалося отримати довідник автомобілів.")
        return

    records = await asyncio.to_thread(worksheet.get_all_records, expected_headers=VEHICLES_HEADERS)

    found_vehicles = [r for r in records if query in re.sub(r'[^0-9]', '', str(r.get('Номер авто', '')))]

    if found_vehicles:
        await message.answer(f"Знайдено {len(found_vehicles)} авто. Оберіть одне:", reply_markup=get_found_vehicles_keyboard(found_vehicles))
    else:
        await message.answer("❌ Автомобілів за вашим запитом не знайдено.", reply_markup=get_found_vehicles_keyboard([]))

@dp.callback_query(F.data.startswith("found_vehicle_"), Reporting.searching_vehicles)
async def process_found_vehicle_selection(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    vehicle_number = callback.data.split("found_vehicle_")[1]
    vehicle_data = await get_vehicle_data_from_db(vehicle_number)

    if vehicle_data:
        # Перевіряємо, чи це пошук для прикріплення чи для звіту
        current_state_name = await state.get_state()
        data = await state.get_data()

        # Якщо це пошук з кнопки "Прикріпити авто"
        if data.get("is_attaching_vehicle"):
            # Показуємо питання про прикріплення
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Так, прикріпити", callback_data=f"confirm_attach_{vehicle_number}")],
                [InlineKeyboardButton(text="❌ Ні, скасувати", callback_data="cancel_attach")]
            ])
            await safe_edit_message(callback,
                f"**Прикріпити авто {vehicle_data.get('vehicle_brand', 'Не вказано')} ({vehicle_number})?**\n\n"
                f"Тип палива: {vehicle_data.get('fuel_type', 'Не вказано')}\n"
                f"Норма розходу: {vehicle_data.get('consumption_rate', 'Не вказано')} л/100км",
                reply_markup=keyboard, parse_mode="Markdown")
        else:
            # Звичайний пошук для звіту
            await state.update_data(vehicle_number=vehicle_number, **vehicle_data)
            await safe_edit_message(callback, f"Обрано: <b>{vehicle_data.get('vehicle_brand', 'Не вказано')} ({vehicle_number})</b>.", parse_mode="HTML")
            await go_to_tour_number_step(callback, state)
    else:
        await safe_edit_message(callback, "❌ Помилка: дані про авто не знайдено.")
        user_data = await get_user_from_gsheet(str(callback.from_user.id))
        user_vehicles = user_data.get("vehicles", [])
        user_role = user_data.get("role", "водій")
        await callback.message.answer("Оберіть автомобіль для звіту:", reply_markup=get_vehicle_selection_keyboard(user_vehicles, user_role))
        await state.set_state(Reporting.waiting_for_vehicle_choice)

@dp.callback_query(F.data == "cancel_search", Reporting.searching_vehicles)
async def cancel_search(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await state.clear()
    user_data = await get_user_from_gsheet(str(callback.from_user.id))
    user_vehicles = user_data.get("vehicles", [])
    user_role = user_data.get("role", "водій")
    await safe_edit_message(callback, "Пошук скасовано.")
    await callback.message.answer("Оберіть автомобіль для звіту:", reply_markup=get_vehicle_selection_keyboard(user_vehicles, user_role))
    await state.set_state(Reporting.waiting_for_vehicle_choice)


@dp.message(Reporting.manual_vehicle_number)
async def process_manual_report_vehicle_number(message: types.Message, state: FSMContext):
    match = VEHICLE_NUMBER_PATTERN.search(message.text)
    if not match:
        await message.answer("❗️ Невірний формат.", reply_markup=CANCEL_KEYBOARD_REPLY)
        return
    vehicle_number = f"{match.group(1).upper()}{match.group(2)}{match.group(3).upper()}"
    await state.update_data(vehicle_number=vehicle_number)
    await message.answer("Оберіть або введіть марку авто:", reply_markup=get_car_brand_keyboard())
    await state.set_state(Reporting.manual_brand_choice)

@dp.callback_query(F.data.startswith("brand_"), Reporting.manual_brand_choice)
async def process_manual_report_brand_choice(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    brand = callback.data.split("brand_")[1]
    if brand == "manual_brand":
        await safe_edit_message(callback, "Введіть марку авто вручну:")
        await state.set_state(Reporting.manual_brand_input)
    else:
        await state.update_data(vehicle_brand=brand)
        await safe_edit_message(callback, f"Обрано: {brand}")
        await callback.message.answer("Оберіть тип палива:", reply_markup=get_fuel_type_keyboard())
        await state.set_state(Reporting.manual_fuel_type)

@dp.message(Reporting.manual_brand_input)
async def process_manual_report_brand_input(message: types.Message, state: FSMContext):
    await state.update_data(vehicle_brand=message.text)
    await message.answer("Оберіть тип палива:", reply_markup=get_fuel_type_keyboard())
    await state.set_state(Reporting.manual_fuel_type)

@dp.callback_query(F.data.startswith("fuel_type_"), Reporting.manual_fuel_type)
async def process_manual_fuel_type_callback(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    fuel_type = callback.data.split("fuel_type_")[1]
    if fuel_type == "manual":
        await safe_edit_message(callback, "Введіть тип палива вручну:")
    else:
        await state.update_data(fuel_type=fuel_type)
        await safe_edit_message(callback, f"Обрано: {fuel_type}")
        await callback.message.answer("Введіть норму розходу л/100км:", reply_markup=CANCEL_KEYBOARD_REPLY)
        await state.set_state(Reporting.manual_consumption_rate)

@dp.message(Reporting.manual_fuel_type)
async def process_manual_fuel_type(message: types.Message, state: FSMContext):
    await state.update_data(fuel_type=message.text)
    await message.answer("Введіть норму розходу л/100км:", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(Reporting.manual_consumption_rate)

@dp.message(Reporting.manual_consumption_rate)
async def process_manual_consumption_rate(message: types.Message, state: FSMContext):
    try:
        rate = float(message.text.replace(',', '.'))
        if rate <= 0:
            await message.answer("❗️ Норма витрати має бути більше 0. Введіть ще раз:", reply_markup=CANCEL_KEYBOARD_REPLY)
            return
        await state.update_data(consumption_rate=rate)
        await go_to_tour_number_step(message, state)
    except (ValueError, TypeError):
        await message.answer("❗️Невірний формат, введіть число.", reply_markup=CANCEL_KEYBOARD_REPLY)

@dp.callback_query(F.data == "manual_tour_entry", Reporting.waiting_for_tour_number)
async def manual_tour_entry(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "Введіть номер туру/маршруту:")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)

@dp.callback_query(F.data.startswith("select_tour_"), Reporting.waiting_for_tour_number)
async def process_tour_selection(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await check_tour_and_proceed(callback.data.split("select_tour_")[1], callback, state)

async def process_tour_selection_logic(tour_number: str, source: types.Message | types.CallbackQuery, state: FSMContext):
    message_obj = source.message if isinstance(source, types.CallbackQuery) else source
    tours = await get_tours_from_gsheet()

    # Перевіряємо чи це подвійний тур (формат номер\номер або номер/номер)
    if '\\' in tour_number or '/' in tour_number:
        # Определяем разделитель и разбиваем
        if '\\' in tour_number:
            parts = tour_number.split('\\')
        else:
            parts = tour_number.split('/')

        if len(parts) == 2:
            # Це подвійний тур - пропонуємо вибір
            first_tour = parts[0].strip()
            second_tour = parts[1].strip()

            # Спочатку шукаємо об'єднаний тур
            combined_tour = next((t for t in tours if str(t.get("Номер туру")) == tour_number), None)

            if combined_tour:
                # Якщо знайшли об'єднаний тур, використовуємо його відстань
                total_distance = float(str(combined_tour.get("Відстань км", 0)).replace(',', '.')) if combined_tour.get("Відстань км") else 0

                # Для частин шукаємо окремі тури або ділимо навпіл
                first_selected = next((t for t in tours if str(t.get("Номер туру")) == first_tour), None)
                second_selected = next((t for t in tours if str(t.get("Номер туру")) == second_tour), None)

                if first_selected and second_selected:
                    # Якщо є окремі тури, використовуємо їх відстані
                    distance1 = float(str(first_selected.get("Відстань км", 0)).replace(',', '.')) if first_selected.get("Відстань км") else 0
                    distance2 = float(str(second_selected.get("Відстань км", 0)).replace(',', '.')) if second_selected.get("Відстань км") else 0
                else:
                    # Якщо немає окремих турів, ділимо відстань навпіл
                    distance1 = total_distance / 2
                    distance2 = total_distance / 2
            else:
                # Якщо немає об'єднаного туру, шукаємо окремі
                first_selected = next((t for t in tours if str(t.get("Номер туру")) == first_tour), None)
                second_selected = next((t for t in tours if str(t.get("Номер туру")) == second_tour), None)

                if not first_selected or not second_selected:
                    await message_obj.answer("Помилка: тур не знайдено в базі.", reply_markup=await get_tour_selection_keyboard())
                    return

                # Сумуємо відстані окремих турів
                distance1 = float(str(first_selected.get("Відстань км", 0)).replace(',', '.')) if first_selected.get("Відстань км") else 0
                distance2 = float(str(second_selected.get("Відстань км", 0)).replace(',', '.')) if second_selected.get("Відстань км") else 0
                total_distance = distance1 + distance2

            # Зберігаємо дані про подвійний тур
            await state.update_data(
                double_tour_full=tour_number,
                first_tour=first_tour,
                second_tour=second_tour,
                first_distance=distance1,
                second_distance=distance2,
                total_distance=total_distance
            )

            # Пропонуємо вибір: частина чи цілий тур
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Звіт про частину туру", callback_data="double_tour_part")],
                [InlineKeyboardButton(text="📋 Звіт про цілий тур", callback_data="double_tour_full")],
                [InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_action")]
            ])

            if isinstance(source, types.CallbackQuery):
                await safe_edit_message(source, f"**Подвійний тур {tour_number}**\n\nОберіть тип звіту:", reply_markup=keyboard, parse_mode="Markdown")
            else:
                await source.answer(f"**Подвійний тур {tour_number}**\n\nОберіть тип звіту:", reply_markup=keyboard, parse_mode="Markdown")
            return
        else:
            await message_obj.answer("Помилка: невірний формат подвійного туру.", reply_markup=await get_tour_selection_keyboard())
            return
    else:
        # Звичайний тур
        selected_tour = next((t for t in tours if str(t.get("Номер туру")) == tour_number), None)

        if not selected_tour:
            await message_obj.answer("Помилка: тур не знайдено.", reply_markup=await get_tour_selection_keyboard())
            return

        # Отримуємо відстань туру
        distance = selected_tour.get("Відстань км", 0)
        try:
            distance = float(str(distance).replace(',', '.'))
        except (ValueError, TypeError):
            distance = 0

        # Звичайний тур має табель = 1
        await state.update_data(tour_number=tour_number, tabel_number=1, distance=distance)
        message_text = f"Обрано тур №{tour_number} ({distance} км).\nВведіть фактичну заправку (л):"

    if isinstance(source, types.CallbackQuery):
        await safe_edit_message(source, message_text)
        await source.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    else:
        await source.answer(message_text, reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(Reporting.waiting_for_actual_refill)

@dp.callback_query(F.data == "tour_day_off", Reporting.waiting_for_tour_number)
async def process_tour_day_off(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await state.update_data(tour_number="Вихідний", tabel_number=0, actual_refill=0, distance=0)
    await safe_edit_message(callback, "Обрано: <b>Вихідний</b>", parse_mode="HTML")
    await callback.message.answer("Оберіть час:", reply_markup=get_datetime_choice_keyboard())
    await state.set_state(Reporting.waiting_for_datetime_choice)

@dp.callback_query(F.data == "double_tour_part", Reporting.waiting_for_tour_number)
async def choose_double_tour_part(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    data = await state.get_data()
    first_tour = data.get("first_tour")
    second_tour = data.get("second_tour")
    first_distance = data.get("first_distance", 0)
    second_distance = data.get("second_distance", 0)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"1️⃣ Тур №{first_tour} ({first_distance} км)", callback_data=f"select_part_{first_tour}")],
        [InlineKeyboardButton(text=f"2️⃣ Тур №{second_tour} ({second_distance} км)", callback_data=f"select_part_{second_tour}")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_double_choice")]
    ])

    await safe_edit_message(callback, "**Оберіть частину туру:**", reply_markup=keyboard, parse_mode="Markdown")

@dp.callback_query(F.data == "double_tour_full", Reporting.waiting_for_tour_number)
async def choose_double_tour_full(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    data = await state.get_data()
    double_tour_full = data.get("double_tour_full")
    total_distance = data.get("total_distance", 0)

    await state.update_data(tour_number=double_tour_full, tabel_number=1, distance=total_distance)
    await safe_edit_message(callback, f"Обрано цілий тур №{double_tour_full} ({total_distance} км).\nВведіть фактичну заправку (л):")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(Reporting.waiting_for_actual_refill)

@dp.callback_query(F.data.startswith("select_part_"), Reporting.waiting_for_tour_number)
async def choose_tour_part(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    part_number = callback.data.split("select_part_")[1]
    data = await state.get_data()

    if part_number == data.get("first_tour"):
        distance = data.get("first_distance", 0)
    else:
        distance = data.get("second_distance", 0)

    await state.update_data(tour_number=part_number, tabel_number=0.5, distance=distance)
    await safe_edit_message(callback, f"Обрано частину туру №{part_number} ({distance} км, табель: 0.5).\nВведіть фактичну заправку (л):")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(Reporting.waiting_for_actual_refill)

@dp.callback_query(F.data == "back_to_double_choice", Reporting.waiting_for_tour_number)
async def back_to_double_choice(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    data = await state.get_data()
    double_tour_full = data.get("double_tour_full")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Звіт про частину туру", callback_data="double_tour_part")],
        [InlineKeyboardButton(text="📋 Звіт про цілий тур", callback_data="double_tour_full")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_action")]
    ])

    await safe_edit_message(callback, f"**Подвійний тур {double_tour_full}**\n\nОберіть тип звіту:", reply_markup=keyboard, parse_mode="Markdown")

@dp.message(Reporting.waiting_for_tour_number)
async def process_tour_number_message(message: types.Message, state: FSMContext):
    await check_tour_and_proceed(message.text.strip(), message, state)

@dp.callback_query(F.data == "force_submit_report", Reporting.waiting_for_duplicate_confirmation)
async def force_submit_duplicate_report(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await state.update_data(is_clarification_report=True)
    data = await state.get_data()
    tour_number = data.get("tour_number_to_confirm")
    await safe_edit_message(callback, f"Звіт по туру №{tour_number} буде надіслано на з'ясування.")
    await process_tour_selection_logic(tour_number, callback, state)

@dp.callback_query(F.data == "cancel_duplicate_report", Reporting.waiting_for_duplicate_confirmation)
async def cancel_duplicate_report(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await state.update_data(is_clarification_report=False)
    await safe_edit_message(callback, "Добре, оберіть інший тур:", reply_markup=await get_tour_selection_keyboard())
    await state.set_state(Reporting.waiting_for_tour_number)

@dp.message(Reporting.waiting_for_actual_refill)
async def process_actual_refill_input(message: types.Message, state: FSMContext):
    try:
        value = float(message.text.replace(',', '.'))
        if value < 0:
            await message.answer("❗️ Значення не може бути від'ємним. Введіть додатнє число:", reply_markup=CANCEL_KEYBOARD_REPLY)
            return
        await state.update_data(actual_refill=value)
        await message.answer("Додайте примітку до рейсу (або напишіть 'немає' якщо примітки немає):", reply_markup=CANCEL_KEYBOARD_REPLY)
        await state.set_state(Reporting.waiting_for_note)
    except ValueError:
        await message.answer("❗️Невірний формат, введіть число.", reply_markup=CANCEL_KEYBOARD_REPLY)

@dp.message(Reporting.waiting_for_note)
async def process_note_input(message: types.Message, state: FSMContext):
    note = message.text.strip()
    if note.lower() in ['немає', 'нема', 'без примітки', '-', '']:
        note = ""
    await state.update_data(note=note)
    await message.answer("Оберіть час:", reply_markup=get_datetime_choice_keyboard())
    await state.set_state(Reporting.waiting_for_datetime_choice)

@dp.callback_query(F.data == "edit_trip_actual_refill", Reporting.waiting_for_trip_confirmation)
async def edit_trip_actual_refill(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "Введіть нову фактичну заправку (л):")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(Reporting.waiting_for_actual_refill)

@dp.callback_query(F.data == "edit_trip_note", Reporting.waiting_for_trip_confirmation)
async def edit_trip_note(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "Введіть нову примітку:")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(Reporting.waiting_for_note)

@dp.callback_query(F.data == "edit_trip_datetime", Reporting.waiting_for_trip_confirmation)
async def edit_trip_report_datetime(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "Оберіть час рейсу:", reply_markup=get_datetime_choice_keyboard())
    await state.set_state(Reporting.waiting_for_datetime_choice)

@dp.callback_query(F.data == "confirm_trip_report", Reporting.waiting_for_trip_confirmation)
async def confirm_and_save_trip_report(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "⏳ Зберігаю звіт...", reply_markup=None)
    data = await state.get_data()
    driver_info = await get_user_from_gsheet(str(callback.from_user.id))
    if not driver_info:
        await safe_edit_message(callback, "❌ Помилка: профіль не знайдено.")
        await state.clear()
        return

    # Тільки адмін та ТО можуть створювати нові записи автомобілів
    if data.get("vehicle_number") and data.get("vehicle_brand") and driver_info.get("role") in ["адмін", "ТО"]:
        await save_vehicle_to_gsheet({"number": data.get("vehicle_number"), "brand": data.get("vehicle_brand"), "fuel_type": data.get("fuel_type", "Не вказано"), "consumption_rate": data.get("consumption_rate", "Не вказано")})

    report_data = {**data, "driver_first_name": driver_info.get("first_name", "N/A"), "driver_last_name": driver_info.get("last_name", "N/A"), "fuel_card": driver_info.get("fuel_card", "Не вказано")}

    try:
        worksheet_title = CLARIFICATION_WORKSHEET_TITLE if data.get("is_clarification_report") else REPORTS_WORKSHEET_TITLE
        await append_report_to_gsheet(report_data, worksheet_title=worksheet_title)
        vehicle_number, distance, tour_number = data.get("vehicle_number"), data.get("distance", 0), data.get("tour_number", "")
        if vehicle_number and distance and tour_number != "Вихідний":
            try:
                distance_float = float(distance)
                if distance_float > 0: await update_vehicle_mileage(vehicle_number, distance_float)
            except (ValueError, TypeError) as e: logging.error(f"Помилка конвертації відстані '{distance}': {e}")
        await safe_edit_message(callback, "✅ Звіт успішно збережено!")
    except Exception as e:
        logging.error(f"Фінальна помилка збереження звіту: {e}")
        await safe_edit_message(callback, "❌ Помилка збереження.")
    finally:
        await show_main_menu(callback.message, state, str(callback.from_user.id))

# Спеціальний обробник для скасування у стані підтвердження звіту
@dp.callback_query(F.data == "cancel_action", StateFilter(Reporting.waiting_for_trip_confirmation))
async def cancel_trip_report_confirmation(callback: types.CallbackQuery, state: FSMContext):
    await universal_cancel_handler(callback, state)

@dp.callback_query(F.data == "dt_current_time", Reporting.waiting_for_datetime_choice)
async def use_current_time(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await state.update_data(report_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    await safe_edit_message(callback, "Час встановлено на поточний.")
    await show_trip_data_for_confirmation(callback, state)





# ========================================
#         БЛОК ОБРОБКИ ЧЕКІВ АЗС
# ========================================
async def show_fuel_data_for_confirmation(message_or_callback: types.Message | types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    text = (f"**Перевірте дані по пальному:**\n\n"
            f"⛽️ Літри: `{data.get('liters', 'не вказано')}`\n"
            f"💰 Ціна: `{data.get('price_per_liter', 'не вказано')}` грн/л\n"
            f"🧾 Код чеку: `{data.get('check_code', 'не вказано')}`\n\n"
            f"Все вірно?")
    keyboard = get_fuel_edit_keyboard()
    if isinstance(message_or_callback, types.CallbackQuery): await safe_edit_message(message_or_callback, text, reply_markup=keyboard, parse_mode="Markdown")
    else: await message_or_callback.answer(text, reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(FuelReport.waiting_for_edit_or_confirm)

@dp.message(F.text == "⛽️ Надіслати чек АЗС")
async def request_receipt_photo(message: types.Message, state: FSMContext):
    user_data = await get_user_from_gsheet(str(message.from_user.id))
    if not user_data:
        await message.answer("Будь ласка, зареєструйтесь /start.");
        return

    await state.clear()
    await message.answer("Надішліть фото чека з АЗС або натисніть 'Скасувати'.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(FuelReport.waiting_for_receipt_photo)

@dp.message(FuelReport.waiting_for_receipt_photo, F.photo)
# Функция обработки фото убрана

@dp.callback_query(F.data == "fuel_manual_start")
async def start_manual_fuel_input(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "Введіть кількість літрів:")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(FuelReport.waiting_for_manual_liters)

@dp.message(FuelReport.waiting_for_manual_liters)
async def process_manual_liters(message: types.Message, state: FSMContext):
    try:
        value = float(message.text.replace(',', '.'))
        if value <= 0:
            await message.answer("❗️ Значення має бути більше 0. Введіть ще раз:", reply_markup=CANCEL_KEYBOARD_REPLY)
            return
        await state.update_data(liters=value)
        await message.answer("Введіть ціну за літр:", reply_markup=CANCEL_KEYBOARD_REPLY)
        await state.set_state(FuelReport.waiting_for_manual_price)
    except ValueError:
        await message.answer("❗️Невірний формат.", reply_markup=CANCEL_KEYBOARD_REPLY)

@dp.message(FuelReport.waiting_for_manual_price)
async def process_manual_price(message: types.Message, state: FSMContext):
    try:
        value = float(message.text.replace(',', '.'))
        if value <= 0:
            await message.answer("❗️ Значення має бути більше 0. Введіть ще раз:", reply_markup=CANCEL_KEYBOARD_REPLY)
            return
        await state.update_data(price_per_liter=value)
        await message.answer("Введіть код чеку:", reply_markup=CANCEL_KEYBOARD_REPLY)
        await state.set_state(FuelReport.waiting_for_manual_check_code)
    except ValueError:
        await message.answer("❗️Невірний формат.", reply_markup=CANCEL_KEYBOARD_REPLY)

@dp.message(FuelReport.waiting_for_manual_check_code)
async def process_manual_check_code(message: types.Message, state: FSMContext):
    await state.update_data(check_code=message.text)
    await show_fuel_data_for_confirmation(message, state)

@dp.callback_query(F.data.startswith("edit_fuel_"), FuelReport.waiting_for_edit_or_confirm)
async def edit_fuel_report_field(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    field = callback.data.split("edit_fuel_")[1]
    if field == "liters":
        await state.set_state(FuelReport.waiting_for_edit_liters)
        await safe_edit_message(callback, "Нова кількість літрів:")
    elif field == "price":
        await state.set_state(FuelReport.waiting_for_edit_price)
        await safe_edit_message(callback, "Нова ціна:")
    elif field == "check_code":
        await state.set_state(FuelReport.waiting_for_edit_check_code)
        await safe_edit_message(callback, "Новий код чеку:")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)

@dp.message(FuelReport.waiting_for_edit_liters)
async def process_edited_liters(message: types.Message, state: FSMContext):
    try:
        value = float(message.text.replace(',', '.'))
        if value <= 0:
            await message.answer("❗️ Значення має бути більше 0. Введіть ще раз:", reply_markup=CANCEL_KEYBOARD_REPLY)
            return
        await state.update_data(liters=value)
        await show_fuel_data_for_confirmation(message, state)
    except ValueError:
        await message.answer("❗️Невірний формат.", reply_markup=CANCEL_KEYBOARD_REPLY)

@dp.message(FuelReport.waiting_for_edit_price)
async def process_edited_price(message: types.Message, state: FSMContext):
    try:
        value = float(message.text.replace(',', '.'))
        if value <= 0:
            await message.answer("❗️ Значення має бути більше 0. Введіть ще раз:", reply_markup=CANCEL_KEYBOARD_REPLY)
            return
        await state.update_data(price_per_liter=value)
        await show_fuel_data_for_confirmation(message, state)
    except ValueError:
        await message.answer("❗️Невірний формат.", reply_markup=CANCEL_KEYBOARD_REPLY)

@dp.message(FuelReport.waiting_for_edit_check_code)
async def process_edited_check_code(message: types.Message, state: FSMContext):
    await state.update_data(check_code=message.text)
    await show_fuel_data_for_confirmation(message, state)

@dp.callback_query(F.data == "confirm_fuel_report", FuelReport.waiting_for_edit_or_confirm)
async def confirm_and_save_fuel_report(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "⏳ Зберігаю...", reply_markup=None)
    data = await state.get_data()
    driver_info = await get_user_from_gsheet(str(callback.from_user.id))
    if not driver_info:
        await safe_edit_message(callback, "❌ Помилка: профіль не знайдено.")
        await state.clear()
        return
    report_data = {"report_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "driver_full_name": f"{driver_info.get('first_name', '')} {driver_info.get('last_name', '')}".strip(), **data}
    try:
        await append_fuel_report_to_gsheet(report_data)
        await safe_edit_message(callback, "✅ Звіт по пальному збережено!")
    except Exception as e:
        logging.error(f"Фінальна помилка збереження звіту: {e}")
        await safe_edit_message(callback, "❌ Помилка збереження.")
    finally:
        await show_main_menu(callback.message, state, str(callback.from_user.id))

# ========================================
#         БЛОК ПРОФІЛЮ КОРИСТУВАЧА
# ========================================
@dp.message(F.text == "👤 Профіль")
async def show_user_profile(message: types.Message, state: FSMContext):
    await state.clear()
    user_data = await get_user_from_gsheet(str(message.from_user.id))
    if not user_data:
        await message.answer("Зареєструйтесь через /start.")
        return

    profile_text = (f"👤 **Ваш профіль:**\n\n"
        f"**Ім'я:** {user_data.get('first_name', 'N/A')}\n**Прізвище:** {user_data.get('last_name', 'N/A')}\n"
        f"**Телефон:** {user_data.get('phone_number', 'N/A')}\n**Паливна картка:** {user_data.get('fuel_card', 'N/A')}\n"
        f"**Кількість авто:** {len(user_data.get('vehicles', []))}\n"
        f"**Роль:** {user_data.get('role', 'N/A')}")
    keyboard_buttons = []

    if user_data.get("role") == "адмін":
        keyboard_buttons.append([InlineKeyboardButton(text="✏️ Ім'я", callback_data="edit_profile_first_name"), InlineKeyboardButton(text="✏️ Прізвище", callback_data="edit_profile_last_name")])
        keyboard_buttons.append([InlineKeyboardButton(text="✏️ Паливна картка", callback_data="edit_profile_fuel_card")])

    # Керування авто для всіх ролей (різний функціонал)
    if user_data.get("role") in ["адмін", "ТО"]:
        keyboard_buttons.append([InlineKeyboardButton(text="Керувати авто", callback_data="manage_vehicles")])
    else:
        keyboard_buttons.append([InlineKeyboardButton(text="Прикріплені авто", callback_data="manage_vehicles")])

    if user_data.get("role") == "водій":
        keyboard_buttons.append([InlineKeyboardButton(text="🚨 Повідомити про поломку", callback_data="report_breakdown")])

    keyboard_buttons.append([InlineKeyboardButton(text="↩️ Головне меню", callback_data="back_to_main_menu")])

    await message.answer(profile_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons), parse_mode="Markdown")
    await state.set_state(ProfileManagement.viewing_profile)

@dp.callback_query(F.data == "edit_profile_first_name", ProfileManagement.viewing_profile)
async def edit_profile_first_name(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "Введіть нове ім'я:")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(ProfileManagement.editing_first_name)

@dp.message(ProfileManagement.editing_first_name)
async def process_edited_first_name(message: types.Message, state: FSMContext):
    user_id = str(message.from_user.id)
    user_data = await get_user_from_gsheet(user_id)
    if user_data:
        user_data["first_name"] = message.text.strip()
        await save_user_to_gsheet(user_id, user_data)
        await message.answer(f"Ім'я змінено!")
        await show_user_profile(message, state)
    else: await message.answer("Помилка: профіль не знайдено.")

@dp.callback_query(F.data == "edit_profile_last_name", ProfileManagement.viewing_profile)
async def edit_profile_last_name(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "Введіть нове прізвище:")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(ProfileManagement.editing_last_name)

@dp.message(ProfileManagement.editing_last_name)
async def process_edited_last_name(message: types.Message, state: FSMContext):
    user_id = str(message.from_user.id)
    user_data = await get_user_from_gsheet(user_id)
    if user_data:
        user_data["last_name"] = message.text.strip()
        await save_user_to_gsheet(user_id, user_data)
        await message.answer(f"Прізвище змінено!")
        await show_user_profile(message, state)
    else: await message.answer("Помилка: профіль не знайдено.")

@dp.callback_query(F.data == "edit_profile_fuel_card", ProfileManagement.viewing_profile)
async def edit_profile_fuel_card(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "Введіть новий номер картки:")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(ProfileManagement.editing_fuel_card)

@dp.message(ProfileManagement.editing_fuel_card)
async def process_edited_fuel_card(message: types.Message, state: FSMContext):
    user_id = str(message.from_user.id)
    user_data = await get_user_from_gsheet(user_id)
    if user_data:
        user_data["fuel_card"] = message.text.strip()
        await save_user_to_gsheet(user_id, user_data)
        await message.answer(f"Номер картки змінено!")
        await show_user_profile(message, state)
    else: await message.answer("Помилка: профіль не знайдено.")

@dp.callback_query(F.data == "manage_vehicles", ProfileManagement.viewing_profile)
async def manage_user_vehicles(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    user_data = await get_user_from_gsheet(str(callback.from_user.id))
    vehicles = user_data.get("vehicles", []) if user_data else []
    user_role = user_data.get("role", "водій") if user_data else "водій"

    keyboard_buttons = []
    if vehicles:
        for v in vehicles:
            mileage_data = await get_vehicle_mileage_from_db(v.get('number'))
            text = f"🚙 {v['brand']} ({v['number']})\n   Пробіг: {mileage_data.get('total_mileage', 0):.0f} км"

            # Для водителів та бухгалтерів - додаємо кнопки перегляду та відкріплення
            if user_role not in ["адмін", "ТО"]:
                keyboard_buttons.append([
                    InlineKeyboardButton(text=f"👁️ {v['brand']} ({v['number']})", callback_data=f"view_vehicle_{v['number']}"),
                    InlineKeyboardButton(text="🔓", callback_data=f"detach_vehicle_{v['number']}")
                ])
            else:
                keyboard_buttons.append([InlineKeyboardButton(text=text, callback_data=f"view_vehicle_{v['number']}")])

    # Різні кнопки для різних ролей
    if user_role in ["адмін", "ТО"]:
        keyboard_buttons.append([InlineKeyboardButton(text="➕ Додати авто", callback_data="add_vehicle_yes")])
        message_text = "🚗 **Ваші автомобілі:**"
    else:
        # Для водителів та бухгалтерів - кнопка прикріпити авто
        keyboard_buttons.append([InlineKeyboardButton(text="🔗 Прикріпити авто", callback_data="attach_vehicle")])
        message_text = "🚗 **Прикріплені автомобілі:**"
        if not vehicles:
            message_text += "\n\n_Ви можете прикріпити авто з загальної бази._"
        else:
            message_text += "\n\n_👁️ - переглянути деталі, 🔓 - відкріпити авто_"

    keyboard_buttons.append([InlineKeyboardButton(text="↩️ Назад до профілю", callback_data="back_to_profile")])

    await safe_edit_message(callback, message_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons), parse_mode="Markdown")

@dp.callback_query(F.data == "attach_vehicle")
async def attach_vehicle_start(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await state.update_data(is_attaching_vehicle=True)
    await safe_edit_message(callback, "🔍 Введіть номер авто для пошуку в базі (можна вводити тільки цифри):")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(Reporting.searching_vehicles)

@dp.callback_query(F.data.startswith("view_vehicle_"))
async def view_vehicle_details(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    vehicle_number = callback.data.split("view_vehicle_")[1]
    vehicle_data = await get_vehicle_data_from_db(vehicle_number)
    mileage_data = await get_vehicle_mileage_from_db(vehicle_number)

    if not vehicle_data:
        await safe_edit_message(callback, "❌ Дані про авто не знайдено.")
        return

    text = (f"**Інформація про авто {vehicle_number}:**\n\n"
            f"**Марка:** {vehicle_data.get('vehicle_brand', 'N/A')}\n"
            f"**Тип палива:** {vehicle_data.get('fuel_type', 'N/A')}\n"
            f"**Норма розходу:** {vehicle_data.get('consumption_rate', 'N/A')} л/100км\n"
            f"**Загальний пробіг:** {mileage_data.get('total_mileage', 0):.0f} км\n"
            f"**Пробіг з останньої заміни масла:** {mileage_data.get('mileage_since_oil_change', 0):.0f} км")

    # Перевіряємо роль користувача
    user_data = await get_user_from_gsheet(str(callback.from_user.id))
    user_role = user_data.get("role", "водій") if user_data else "водій"

    keyboard_buttons = []

    # Для водителів та бухгалтерів - кнопка відкріпити
    if user_role not in ["адмін", "ТО"]:
        keyboard_buttons.append([InlineKeyboardButton(text="🔓 Відкріпити авто", callback_data=f"detach_vehicle_{vehicle_number}")])

    keyboard_buttons.append([InlineKeyboardButton(text="↩️ Назад", callback_data="manage_vehicles")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await safe_edit_message(callback, text, reply_markup=keyboard, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("confirm_attach_"))
async def confirm_attach_vehicle(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    vehicle_number = callback.data.split("confirm_attach_")[1]

    user_id = str(callback.from_user.id)
    user_data = await get_user_from_gsheet(user_id)
    vehicle_data = await get_vehicle_data_from_db(vehicle_number)

    if user_data and vehicle_data:
        # Додаємо авто до списку користувача
        new_vehicle = {
            "number": vehicle_number,
            "brand": vehicle_data.get("vehicle_brand", "Не вказано"),
            "fuel_type": vehicle_data.get("fuel_type", "Не вказано"),
            "consumption_rate": vehicle_data.get("consumption_rate", "Не вказано")
        }

        user_data.setdefault("vehicles", []).append(new_vehicle)
        await save_user_to_gsheet(user_id, user_data)

        await safe_edit_message(callback, f"✅ Авто {vehicle_data.get('vehicle_brand', 'Не вказано')} ({vehicle_number}) успішно прикріплено!")
        await asyncio.sleep(2)
        await manage_user_vehicles(callback, state)
    else:
        await safe_edit_message(callback, "❌ Помилка при прикріпленні авто.")

@dp.callback_query(F.data == "cancel_attach")
async def cancel_attach_vehicle(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "Прикріплення скасовано.")
    await asyncio.sleep(1)
    await manage_user_vehicles(callback, state)

@dp.callback_query(F.data.startswith("detach_vehicle_"))
async def detach_vehicle(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    vehicle_number = callback.data.split("detach_vehicle_")[1]

    user_id = str(callback.from_user.id)
    user_data = await get_user_from_gsheet(user_id)

    if user_data:
        # Видаляємо авто зі списку користувача
        vehicles = user_data.get("vehicles", [])
        user_data["vehicles"] = [v for v in vehicles if v.get("number") != vehicle_number]
        await save_user_to_gsheet(user_id, user_data)

        await safe_edit_message(callback, f"✅ Авто ({vehicle_number}) успішно відкріплено!")
        await asyncio.sleep(2)
        await manage_user_vehicles(callback, state)
    else:
        await safe_edit_message(callback, "❌ Помилка при відкріпленні авто.")

@dp.callback_query(F.data == "report_breakdown")
async def report_breakdown_start(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    user_data = await get_user_from_gsheet(str(callback.from_user.id))
    vehicles = user_data.get("vehicles", [])
    if not vehicles:
        await safe_edit_message(callback, "У вас немає авто. Будь ласка, додайте авто, щоб повідомити про поломку.")
        return

    keyboard = await get_report_breakdown_keyboard(vehicles)
    await safe_edit_message(callback, "🚨 **Повідомити про поломку**\nОберіть автомобіль:", reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(BreakdownReport.select_vehicle)

@dp.callback_query(F.data.startswith("report_breakdown_"), BreakdownReport.select_vehicle)
async def select_vehicle_for_breakdown(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    vehicle_number = callback.data.split("report_breakdown_")[1]
    await state.update_data(vehicle_number=vehicle_number)
    await safe_edit_message(callback, f"Обрано: **{vehicle_number}**.\n\nОпишіть, будь ласка, поломку:", parse_mode="Markdown")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(BreakdownReport.enter_description)

@dp.message(BreakdownReport.enter_description)
async def save_breakdown_report(message: types.Message, state: FSMContext):
    data = await state.get_data()
    vehicle_number = data.get("vehicle_number")
    user_info = await get_user_from_gsheet(str(message.from_user.id))

    report_data = {
        "driver_full_name": f"{user_info.get('first_name')} {user_info.get('last_name')}",
        "vehicle_number": vehicle_number,
        "vehicle_brand": next((v.get('brand') for v in user_info.get('vehicles', []) if v.get('number') == vehicle_number), "N/A"),
        "breakdown_description": message.text,
    }

    await append_breakdown_report_to_gsheet(report_data)
    await message.answer("✅ Ваше повідомлення про поломку збережено. Найближчим часом з вами зв'яжуться.", reply_markup=ReplyKeyboardRemove())
    await show_main_menu(message, state, str(message.from_user.id))

@dp.callback_query(F.data == "back_to_profile")
async def back_to_profile_menu(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await state.clear()
    await show_user_profile(callback.message, state)

# ========================================
#         БЛОК ПАНЕЛЕЙ (ТО, БУХГАЛТЕР, АДМІН)
# ========================================
@dp.message(F.text == "🔧 Панель ТО")
async def show_technician_panel(message: types.Message, state: FSMContext):
    user_data = await get_user_from_gsheet(str(message.from_user.id))
    if user_data.get("role") not in ["ТО", "адмін"]: await message.answer("❌ Немає доступу."); return
    await state.clear()
    await message.answer("🔧 **Панель ТО**\nОберіть дію:", reply_markup=await get_technician_keyboard(), parse_mode="Markdown")
    await state.set_state(TechnicianPanel.main_menu)

@dp.callback_query(F.data == "back_to_technician_panel")
async def back_to_technician_panel(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "🔧 **Панель ТО**\nОберіть дію:", reply_markup=await get_technician_keyboard(), parse_mode="Markdown")
    await state.set_state(TechnicianPanel.main_menu)

# НОВИЙ БЛОК: Реєстрація заміни масла співробітником ТО
@dp.callback_query(F.data == "to_register_oil_change", TechnicianPanel.main_menu)
async def to_start_oil_change_log(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    worksheet = await get_gsheet_worksheet(VEHICLES_WORKSHEET_TITLE, VEHICLES_HEADERS)
    if not worksheet:
        await safe_edit_message(callback, "❌ Не вдалося завантажити список авто.")
        return

    records = await asyncio.to_thread(worksheet.get_all_records, expected_headers=VEHICLES_HEADERS)

    buttons = [[InlineKeyboardButton(text=f"🚙 {v['Марка авто']} ({v['Номер авто']})", callback_data=f"to_oil_select_{v['Номер авто']}")] for v in records]
    buttons.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="back_to_technician_panel")])

    await safe_edit_message(callback, "🛢️ **Реєстрація заміни масла**\n\nОберіть автомобіль, для якого було виконано заміну:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await state.set_state(OilChangeByTO.selecting_vehicle)

@dp.callback_query(F.data.startswith("to_oil_select_"), OilChangeByTO.selecting_vehicle)
async def to_oil_vehicle_selected(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    vehicle_number = callback.data.split("to_oil_select_")[1]
    await state.update_data(vehicle_number=vehicle_number)

    await safe_edit_message(callback, f"Авто: **{vehicle_number}**.\n\nВведіть **вартість** заміни (грн):", parse_mode="Markdown")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(OilChangeByTO.entering_price)

@dp.message(OilChangeByTO.entering_price)
async def to_oil_price_entered(message: types.Message, state: FSMContext):
    try:
        price = float(message.text.replace(',', '.'))
        if price <= 0:
            await message.answer("❗️ Вартість має бути більше 0. Введіть ще раз:", reply_markup=CANCEL_KEYBOARD_REPLY)
            return
        await state.update_data(price=price)
        await message.answer("Тепер введіть **кількість** використаного масла (л):", reply_markup=CANCEL_KEYBOARD_REPLY)
        await state.set_state(OilChangeByTO.entering_liters)
    except (ValueError, TypeError):
        await message.answer("❗️Введіть вартість числом.", reply_markup=CANCEL_KEYBOARD_REPLY)

@dp.message(OilChangeByTO.entering_liters)
async def to_oil_liters_entered(message: types.Message, state: FSMContext):
    try:
        liters = float(message.text.replace(',', '.'))
        if liters <= 0:
            await message.answer("❗️ Кількість має бути більше 0. Введіть ще раз:", reply_markup=CANCEL_KEYBOARD_REPLY)
            return
        await state.update_data(liters=liters)
        data = await state.get_data()

        text = (f"**Підтвердіть дані:**\n\n"
                f"Авто: `{data.get('vehicle_number')}`\n"
                f"Вартість: `{data.get('price')}` грн\n"
                f"Кількість: `{data.get('liters')}` л\n\n"
                f"Зберегти звіт та скинути лічильник пробігу для цього авто?")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Так, виконано", callback_data="to_oil_confirm_yes")],
            [InlineKeyboardButton(text="❌ Скасувати", callback_data="back_to_technician_panel")]
        ])

        await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")
        await state.set_state(OilChangeByTO.confirming)
    except (ValueError, TypeError):
        await message.answer("❗️Введіть кількість числом.", reply_markup=CANCEL_KEYBOARD_REPLY)

@dp.callback_query(F.data == "to_oil_confirm_yes", OilChangeByTO.confirming)
async def to_oil_change_confirmed(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "⏳ Зберігаю дані...", reply_markup=None)

    data = await state.get_data()
    user_info = await get_user_from_gsheet(str(callback.from_user.id))
    executor_name = f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip() if user_info else "N/A"

    report_data = {**data, "executor_name": executor_name}

    # Зберігаємо звіт
    await log_oil_change_to_gsheet(report_data)

    # Скидаємо лічильник пробігу
    await record_oil_change_in_db(data.get("vehicle_number"))

    await safe_edit_message(callback, "✅ Заміну масла успішно зареєстровано!")
    await asyncio.sleep(2)
    await back_to_technician_panel(callback, state)


@dp.callback_query(F.data == "technician_record_maintenance", TechnicianPanel.main_menu)
async def start_maintenance_log(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    worksheet = await get_gsheet_worksheet(VEHICLES_WORKSHEET_TITLE, VEHICLES_HEADERS)
    records = await asyncio.to_thread(worksheet.get_all_records, expected_headers=VEHICLES_HEADERS)

    buttons = [[InlineKeyboardButton(text=f"🚙 {v['Марка авто']} ({v['Номер авто']})", callback_data=f"maint_select_{v['Номер авто']}")] for v in records]
    buttons.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="back_to_technician_panel")])
    await safe_edit_message(callback, "🔧 **Запис ТО**: Оберіть авто:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(Maintenance.selecting_vehicle)

@dp.callback_query(F.data.startswith("maint_select_"), Maintenance.selecting_vehicle)
async def maintenance_vehicle_selected(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    vehicle_number = callback.data.split("maint_select_")[1]
    await state.update_data(vehicle_number=vehicle_number)
    await safe_edit_message(callback, f"Авто: **{vehicle_number}**.\nВведіть поточний пробіг (км):", parse_mode="Markdown")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(Maintenance.entering_mileage)

@dp.message(Maintenance.entering_mileage)
async def maintenance_mileage_entered(message: types.Message, state: FSMContext):
    try:
        await state.update_data(mileage=int(message.text))
        await message.answer("Введіть тип робіт:", reply_markup=CANCEL_KEYBOARD_REPLY)
        await state.set_state(Maintenance.entering_work_type)
    except (ValueError, TypeError):
        await message.answer("❗️Введіть пробіг цілим числом.", reply_markup=CANCEL_KEYBOARD_REPLY)

@dp.message(Maintenance.entering_work_type)
async def maintenance_work_type_entered(message: types.Message, state: FSMContext):
    await state.update_data(work_type=message.text)
    await message.answer("Додайте коментар (необов'язково):", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(Maintenance.entering_comment)

@dp.message(Maintenance.entering_comment)
async def maintenance_comment_entered(message: types.Message, state: FSMContext):
    await state.update_data(comment=message.text)
    data = await state.get_data()
    text = (f"**Перевірка даних ТО:**\n\n`{data.get('vehicle_number')}`, пробіг `{data.get('mileage')}` км\n**Роботи:** `{data.get('work_type')}`\n**Коментар:** `{data.get('comment')}`\n\nВсе вірно?")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Так", callback_data="maint_confirm_yes")], [InlineKeyboardButton(text="❌ Скасувати", callback_data="back_to_technician_panel")]])
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(Maintenance.confirming)

@dp.callback_query(F.data == "maint_confirm_yes", Maintenance.confirming)
async def maintenance_confirmed(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    data = await state.get_data()
    user_info = await get_user_from_gsheet(str(callback.from_user.id))
    full_name = f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip() if user_info else "N/A"
    await append_maintenance_log_to_gsheet({**data, "driver_name": full_name})
    await safe_edit_message(callback, "✅ Запис про ТО збережено!")
    await back_to_technician_panel(callback, state)

@dp.callback_query(F.data == "technician_view_breakdowns", TechnicianPanel.main_menu)
async def view_breakdown_tickets(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    worksheet = await get_gsheet_worksheet(BREAKDOWN_REPORTS_WORKSHEET_TITLE, BREAKDOWN_REPORTS_HEADERS)
    if not worksheet:
        await safe_edit_message(callback, "❌ Не вдалося отримати тікети поломок.")
        return

    records = await asyncio.to_thread(worksheet.get_all_records, expected_headers=BREAKDOWN_REPORTS_HEADERS)
    active_tickets = [r for r in records if r.get('Статус') == 'Нова']

    if not active_tickets:
        await safe_edit_message(callback, "✅ Активних тікетів немає.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_technician_panel")]]))
    else:
        keyboard = await get_breakdown_tickets_keyboard(active_tickets)
        await safe_edit_message(callback, "🛠️ **Активні тікети з поломками:**", parse_mode="Markdown", reply_markup=keyboard)

    await state.set_state(TechnicianPanel.viewing_breakdowns)

@dp.callback_query(F.data.startswith("manage_ticket_"), TechnicianPanel.viewing_breakdowns)
async def manage_breakdown_ticket(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    row_index = int(callback.data.split("_")[2])

    worksheet = await get_gsheet_worksheet(BREAKDOWN_REPORTS_WORKSHEET_TITLE, BREAKDOWN_REPORTS_HEADERS)
    if not worksheet: return
    ticket_data = await asyncio.to_thread(worksheet.row_values, row_index)

    ticket = dict(zip(BREAKDOWN_REPORTS_HEADERS, ticket_data))

    await state.update_data(current_ticket_row=row_index, current_ticket_data=ticket)

    text = (f"**Управління тікетом поломки:**\n\n"
            f"**Дата:** {ticket.get('Дата')}\n"
            f"**Водій:** {ticket.get('ПІБ Водія')}\n"
            f"**Авто:** `{ticket.get('Номер авто')}`\n"
            f"**Опис:** {ticket.get('Опис поломки')}\n\n"
            f"Оберіть дію:")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Виконано", callback_data="ticket_set_completed")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_technician_panel")]
    ])

    await safe_edit_message(callback, text, parse_mode="Markdown", reply_markup=keyboard)
    await state.set_state(TechnicianPanel.manage_breakdown)

@dp.callback_query(F.data == "ticket_set_completed", TechnicianPanel.manage_breakdown)
async def prompt_comment_for_completed_ticket(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "Напишіть коментар до виконаного ремонту:")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(TechnicianPanel.enter_comment)

@dp.message(TechnicianPanel.enter_comment)
async def prompt_cost_for_completed_ticket(message: types.Message, state: FSMContext):
    await state.update_data(comment_to_add=message.text)
    await message.answer("Введіть вартість ремонту (число):", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(TechnicianPanel.enter_cost)

@dp.message(TechnicianPanel.enter_cost)
async def save_completed_ticket(message: types.Message, state: FSMContext):
    try:
        repair_cost = float(message.text.replace(',', '.'))
        data = await state.get_data()
        row_index = data.get("current_ticket_row")
        comment = data.get("comment_to_add")

        await update_breakdown_report_in_gsheet(row_index, "Виконано", comment, repair_cost)

        await message.answer(f"✅ Тікет успішно оновлено.", reply_markup=ReplyKeyboardRemove())
        await show_technician_panel(message, state)
    except ValueError:
        await message.answer("❗️ Невірний формат. Введіть число.", reply_markup=CANCEL_KEYBOARD_REPLY)

@dp.message(F.text == "💰 Панель бухгалтера")
async def show_accountant_panel(message: types.Message, state: FSMContext):
    user_data = await get_user_from_gsheet(str(message.from_user.id))
    if user_data.get("role") not in ["бухгалтер", "адмін"]: await message.answer("❌ Немає доступу."); return
    await state.clear()
    await message.answer("💰 **Панель бухгалтера**\nОберіть дію:", reply_markup=get_accountant_keyboard(), parse_mode="Markdown")
    await state.set_state(AccountantPanel.main_menu)

@dp.callback_query(F.data == "back_to_accountant_panel")
async def back_to_accountant_panel(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "💰 **Панель бухгалтера**\nОберіть дію:", reply_markup=get_accountant_keyboard(), parse_mode="Markdown")
    await state.set_state(AccountantPanel.main_menu)

@dp.callback_query(F.data == "acc_view_trip_reports", AccountantPanel.main_menu)
async def accountant_view_trip_reports(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    worksheet = await get_gsheet_worksheet(REPORTS_WORKSHEET_TITLE, REPORTS_HEADERS)
    if not worksheet:
        await safe_edit_message(callback, "❌ Не вдалося отримати звіти. Зверніться до адміністратора.")
        return

    records = await asyncio.to_thread(worksheet.get_all_records, expected_headers=REPORTS_HEADERS)
    if not records:
        await safe_edit_message(callback, "📂 Звітів по рейсах не знайдено.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_accountant_panel")]]))
        return

    # Розрахунок залишку палива для кожного водія
    driver_balances = {}
    for record in records:
        driver = record.get('ПІБ Водія')
        if driver not in driver_balances:
            driver_balances[driver] = 40.0  # Початковий залишок 40 літрів

        # Додаємо фактичну заправку
        actual_refill = float(record.get('Фактична заправка', 0))
        driver_balances[driver] += actual_refill

        # Віднімаємо витрачене паливо (дистанція * норма / 100)
        try:
            distance = float(record.get('КМ', 0))
            rate = float(record.get('Норма розходу', 0))
            consumption = (distance * rate) / 100.0
            driver_balances[driver] -= consumption
        except (ValueError, TypeError):
            pass

        # Обмеження залишку до 70 літрів (ємність бака)
        driver_balances[driver] = min(driver_balances[driver], 70.0)

    report_text = "🗒️ **Останні звіти по рейсах:**\n\n"
    for r in records[-10:]:
        report_text += f"**Водій:** {r.get('ПІБ Водія', 'N/A')}\n"
        report_text += f"**Дата:** {r.get('Дата звіту', 'N/A')}\n"
        report_text += f"**Авто:** `{r.get('Номер авто', 'N/A')}`\n"
        report_text += f"**Тур:** {r.get('Номер туру', 'N/A')} ({r.get('КМ', 'N/A')} км)\n"
        report_text += f"**Факт:** `{r.get('Фактична заправка', 'N/A')}` л\n"

        driver = r.get('ПІБ Водія')
        if driver in driver_balances:
            report_text += f"**Залишок:** `{driver_balances[driver]:.1f}` л\n\n"
        else:
            report_text += "**Залишок:** N/A\n\n"

    await safe_edit_message(callback, report_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_accountant_panel")]]))
    await state.set_state(AccountantPanel.viewing_reports)

@dp.callback_query(F.data == "acc_view_fuel_reports", AccountantPanel.main_menu)
async def accountant_view_fuel_reports(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    worksheet = await get_gsheet_worksheet(FUEL_REPORTS_WORKSHEET_TITLE, FUEL_REPORTS_HEADERS)
    if not worksheet:
        await safe_edit_message(callback, "❌ Не вдалося отримати звіти. Зверніться до адміністратора.")
        return

    records = await asyncio.to_thread(worksheet.get_all_records, expected_headers=FUEL_REPORTS_HEADERS)
    if not records:
        await safe_edit_message(callback, "⛽ Звітів по пальному не знайдено.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_accountant_panel")]]))
        return

    report_text = "⛽️ **Останні звіти по пальному:**\n\n"
    for r in records[-10:]:
        report_text += f"**Водій:** {r.get('ПІБ Водія', 'N/A')}\n"
        report_text += f"**Дата:** {r.get('Дата та час', 'N/A')}\n"
        report_text += f"**Літри:** {r.get('Літри', 'N/A')} л\n"
        report_text += f"**Ціна:** {r.get('Ціна за літр', 'N/A')} грн/л\n"
        report_text += f"**Код чеку:** `{r.get('Код чеку', 'N/A')}`\n\n"

    await safe_edit_message(callback, report_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_accountant_panel")]]))
    await state.set_state(AccountantPanel.viewing_fuel_reports)

@dp.callback_query(F.data == "acc_view_maintenance_reports", AccountantPanel.main_menu)
async def accountant_view_maintenance_reports(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    worksheet = await get_gsheet_worksheet(BREAKDOWN_REPORTS_WORKSHEET_TITLE, BREAKDOWN_REPORTS_HEADERS)
    if not worksheet:
        await safe_edit_message(callback, "❌ Не вдалося отримати звіти по ремонтах. Зверніться до адміністратора.")
        return

    records = await asyncio.to_thread(worksheet.get_all_records, expected_headers=BREAKDOWN_REPORTS_HEADERS)

    completed_reports = [r for r in records if r.get('Статус') == 'Виконано']

    if not completed_reports:
        await safe_edit_message(callback, "📂 Звітів по виконаних ремонтах не знайдено.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_accountant_panel")]]))
        return

    report_text = "🛠️ **Останні звіти по ремонтах:**\n\n"
    for r in completed_reports[-10:]:
        report_text += f"**Дата:** {r.get('Дата', 'N/A')}\n"
        report_text += f"**Авто:** `{r.get('Номер авто', 'N/A')}`\n"
        report_text += f"**Поломка:** {r.get('Опис поломки', 'N/A')}\n"
        report_text += f"**Коментар ТО:** {r.get('Коментар ТО', 'N/A')}\n"
        report_text += f"**Вартість:** {r.get('Вартість ремонту', 'N/A')} грн\n\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_accountant_panel")]])

    await safe_edit_message(callback, report_text, parse_mode="Markdown", reply_markup=keyboard)
    await state.set_state(AccountantPanel.viewing_maintenance_reports)

# НОВИЙ ОБРОБНИК: Перегляд звітів про заміну масла для бухгалтера
@dp.callback_query(F.data == "acc_view_oil_reports", AccountantPanel.main_menu)
async def accountant_view_oil_change_reports(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    worksheet = await get_gsheet_worksheet(OIL_CHANGE_REPORTS_WORKSHEET_TITLE, OIL_CHANGE_REPORTS_HEADERS)
    if not worksheet:
        await safe_edit_message(callback, "❌ Не вдалося отримати звіти. Зверніться до адміністратора.")
        return

    records = await asyncio.to_thread(worksheet.get_all_records, expected_headers=OIL_CHANGE_REPORTS_HEADERS)
    if not records:
        await safe_edit_message(callback, "🛢️ Звітів по заміні масла не знайдено.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_accountant_panel")]]))
        return

    report_text = "🛢️ **Останні звіти по заміні масла:**\n\n"
    for r in records[-10:]: # Показуємо останні 10 записів
        report_text += f"**Дата:** {r.get('Дата', 'N/A')}\n"
        report_text += f"**Авто:** `{r.get('Номер авто', 'N/A')}`\n"
        report_text += f"**Виконавець:** {r.get('Виконавець (ТО)', 'N/A')}\n"
        report_text += f"**Ціна:** {r.get('Ціна, грн', 'N/A')} грн\n"
        report_text += f"**Кількість:** {r.get('Кількість, л', 'N/A')} л\n\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_accountant_panel")]])
    await safe_edit_message(callback, report_text, parse_mode="Markdown", reply_markup=keyboard)
    await state.set_state(AccountantPanel.viewing_oil_change_reports)


@dp.message(F.text == "👑 Панель адміністратора")
async def show_admin_panel(message: types.Message, state: FSMContext):
    user_data = await get_user_from_gsheet(str(message.from_user.id))
    if user_data.get("role") != "адмін": await message.answer("❌ Немає доступу."); return
    await state.clear()
    await message.answer("👑 **Панель адміністратора**", reply_markup=get_admin_panel_keyboard(), parse_mode="Markdown")
    await state.set_state(AdminPanel.main_menu)

@dp.callback_query(F.data == "back_to_admin_panel")
async def back_to_admin_panel(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "👑 **Панель адміністратора**", reply_markup=get_admin_panel_keyboard(), parse_mode="Markdown")
    await state.set_state(AdminPanel.main_menu)

@dp.callback_query(F.data == "admin_manage_roles", AdminPanel.main_menu)
async def admin_manage_roles(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    users_data = await get_all_users_from_gsheet()
    keyboard = get_user_list_keyboard(users_data)
    await safe_edit_message(callback, "👥 **Керування ролями**\nОберіть користувача:", reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(AdminPanel.manage_roles)

@dp.callback_query(F.data.startswith("select_user_"), AdminPanel.manage_roles)
async def admin_select_user_for_role(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    user_id_to_edit = callback.data.split("_")[2]
    user_data = await get_user_from_gsheet(user_id_to_edit)
    if not user_data:
        await safe_edit_message(callback, "❌ Користувача не знайдено.")
        await state.set_state(AdminPanel.main_menu)
        return
    await state.update_data(user_id_to_edit=user_id_to_edit, current_role=user_data.get("role", "водій"))

    keyboard = get_role_selection_keyboard(user_data.get("role", "водій"))
    await safe_edit_message(callback, f"Оберіть нову роль для **{user_data.get('first_name')} {user_data.get('last_name')}** (поточна роль: {user_data.get('role', 'водій')})", reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(AdminPanel.select_new_role)

@dp.callback_query(F.data.startswith("set_role_"), AdminPanel.select_new_role)
async def admin_set_new_role(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    new_role = callback.data.split("_")[2]
    data = await state.get_data()
    user_id_to_edit = data.get("user_id_to_edit")
    user_data = await get_user_from_gsheet(user_id_to_edit)
    if user_data:
        user_data["role"] = new_role
        await save_user_to_gsheet(user_id_to_edit, user_data)
        clear_caches(user_id_to_edit)
        await safe_edit_message(callback, f"✅ Роль користувача **{user_data.get('first_name')} {user_data.get('last_name')}** змінено на **{new_role}**.", parse_mode="Markdown")
    else:
        await safe_edit_message(callback, "❌ Користувача не знайдено.")
    await asyncio.sleep(2)
    await back_to_admin_panel(callback, state)

@dp.callback_query(F.data == "admin_send_reminders", AdminPanel.main_menu)
async def admin_send_manual_reminders(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "⏳ Надсилаю нагадування всім водіям...")

    try:
        # Отримуємо список водіїв, які ще не подали звіт
        reported_users = await get_today_reports()
        users_data = await get_all_users_from_gsheet()

        drivers_to_remind = []
        for user_id, user_info in users_data.items():
            # Нормалізована перевірка ролі
            user_role = user_info.get("role", "").strip().lower()
            if user_role != "водій":
                continue
            if user_id not in reported_users:
                drivers_to_remind.append((user_id, f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip()))

        if not drivers_to_remind:
            await safe_edit_message(callback, "✅ Всі водії вже подали звіти за сьогодні!")
            await asyncio.sleep(3)
            await back_to_admin_panel(callback, state)
            return

        # Надсилаємо нагадування
        success_count = 0
        for user_id, user_name in drivers_to_remind:
            if await send_manual_reminder(bot, user_id, user_name):
                success_count += 1

        result_text = (f"🔔 **Нагадування надіслано!**\n\n"
                      f"✅ Успішно: {success_count}\n"
                      f"📊 Всього водіїв без звітів: {len(drivers_to_remind)}")

        if success_count < len(drivers_to_remind):
            failed_count = len(drivers_to_remind) - success_count
            result_text += f"\n❌ Помилки: {failed_count}"

        await safe_edit_message(callback, result_text, parse_mode="Markdown")
        await asyncio.sleep(5)
        await back_to_admin_panel(callback, state)

    except Exception as e:
        logging.error(f"Помилка при надсиланні ручних нагадувань: {e}")
        await safe_edit_message(callback, "❌ Сталася помилка при надсиланні нагадувань.")
        await asyncio.sleep(3)
        await back_to_admin_panel(callback, state)

@dp.callback_query(F.data == "admin_test_reminders", AdminPanel.main_menu)
async def admin_test_reminders(callback: types.CallbackQuery, state: FSMContext):
    """Тестування системи нагадувань"""
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "🧪 Тестування системи нагадувань...")

    try:
        # Отримуємо поточний час
        utc_now = datetime.utcnow()
        ukraine_hour = (utc_now.hour + UKRAINE_UTC_OFFSET) % 24

        # Перевіряємо статус aiocron
        aiocron_status = "✅ Активний" if AIOCRON_AVAILABLE else "❌ Неактивний"

        # Тестуємо функцію перевірки звітів
        test_result = "🔍 Тестування функції check_missing_reports()...\n"

        try:
            await check_missing_reports()
            test_result += "✅ Функція виконалась без помилок\n"
        except Exception as e:
            test_result += f"❌ Помилка в функції: {e}\n"

        # Перевіряємо кількість водіїв та звітів
        reported_users = await get_today_reports()
        users_data = await get_all_users_from_gsheet()

        # Детальна діагностика ролей
        all_roles = {}
        drivers_list = []
        for user_id, user_info in users_data.items():
            role = user_info.get("role", "не вказано").strip()
            if role not in all_roles:
                all_roles[role] = 0
            all_roles[role] += 1

            # Нормалізована перевірка ролі
            normalized_role = role.lower()
            if normalized_role == "водій":
                full_name = f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip()
                drivers_list.append(f"{full_name} (ID: {user_id})")

        drivers_count = len(drivers_list)
        reports_count = len(reported_users)

        # Формуємо інформацію про ролі
        roles_info = "\n".join([f"   • {role}: {count}" for role, count in all_roles.items()])

        result_text = (
            f"🧪 **Результати тестування нагадувань**\n\n"
            f"⏰ Поточний час:\n"
            f"   • UTC: {utc_now.strftime('%H:%M')}\n"
            f"   • Україна: {ukraine_hour:02d}:{utc_now.minute:02d}\n\n"
            f"🤖 Статус aiocron: {aiocron_status}\n\n"
            f"📊 Статистика:\n"
            f"   • Всього користувачів: {len(users_data)}\n"
            f"   • Водіїв: {drivers_count}\n"
            f"   • Звітів за сьогодні: {reports_count}\n"
            f"   • Активних нагадувань: {len(REMINDER_TRACKER)}\n\n"
            f"👥 Ролі в системі:\n{roles_info}\n\n"
            f"{test_result}\n"
            f"⚙️ Налаштування:\n"
            f"   • Час нагадувань: 13:00-18:00 (Україна)\n"
            f"   • Максимум нагадувань: {REMINDER_MAX_COUNT}\n"
        )

        # Якщо водіїв знайдено, додаємо їх список
        if drivers_list:
            drivers_text = "\n".join([f"   • {name}" for name in drivers_list[:5]])  # Показуємо перших 5
            if len(drivers_list) > 5:
                drivers_text += f"\n   • ... та ще {len(drivers_list) - 5}"
            result_text += f"\n🚗 Водії:\n{drivers_text}\n"

        await safe_edit_message(callback, result_text, parse_mode="Markdown")
        await asyncio.sleep(8)
        await back_to_admin_panel(callback, state)

    except Exception as e:
        logging.error(f"Помилка при тестуванні нагадувань: {e}")
        await safe_edit_message(callback, f"❌ Помилка при тестуванні: {e}")
        await asyncio.sleep(3)
        await back_to_admin_panel(callback, state)

@dp.callback_query(F.data == "admin_check_oil", AdminPanel.main_menu)
async def admin_check_oil_change(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "⏳ Перевіряю необхідність заміни масла...")

    try:
        # Викликаємо функцію перевірки заміни масла
        await check_all_vehicles_oil_change()

        await safe_edit_message(callback, "✅ **Перевірка заміни масла завершена!**\n\nНагадування надіслано всім, кому потрібна заміна масла.", parse_mode="Markdown")
        await asyncio.sleep(4)
        await back_to_admin_panel(callback, state)

    except Exception as e:
        logging.error(f"Помилка при ручній перевірці заміни масла: {e}")
        await safe_edit_message(callback, "❌ Сталася помилка при перевірці заміни масла.")
        await asyncio.sleep(3)
        await back_to_admin_panel(callback, state)

@dp.callback_query(F.data == "admin_create_tour", AdminPanel.main_menu)
async def admin_create_tour_start(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await state.clear()
    await safe_edit_message(callback, "Введіть **номер** нового туру:")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(TourManagement.creating_tour_number)

@dp.message(TourManagement.creating_tour_number)
async def admin_process_tour_number(message: types.Message, state: FSMContext):
    await state.update_data(tour_number=message.text)
    await message.answer("Тепер введіть **відстань** в км:", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(TourManagement.creating_tour_distance)

@dp.message(TourManagement.creating_tour_distance)
async def admin_process_tour_distance(message: types.Message, state: FSMContext):
    try:
        distance = float(message.text.replace(',', '.'))
        if distance <= 0:
            await message.answer("❗️ Відстань має бути більше 0 км. Введіть ще раз:", reply_markup=CANCEL_KEYBOARD_REPLY)
            return
        await state.update_data(distance=distance)
        await message.answer("Оберіть тип туру:", reply_markup=get_tour_type_keyboard())
        await state.set_state(TourManagement.creating_tour_type)
    except (ValueError, TypeError):
        await message.answer("❗️Невірний формат, введіть число.", reply_markup=CANCEL_KEYBOARD_REPLY)

@dp.callback_query(F.data.startswith("tour_type_"), TourManagement.creating_tour_type)
async def admin_process_tour_type(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    tour_type = callback.data.split("tour_type_")[1]

    data = await state.get_data()
    tour_number = data.get("tour_number")
    distance = data.get("distance")

    tour_data = {
        "tour_number": tour_number,
        "distance": distance,
        "created_by": callback.from_user.first_name or "Адмін",
        "created_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

    if await append_tour_to_gsheet(tour_data):
        await safe_edit_message(callback, f"✅ Тур **№{tour_number}** ({distance} км) створено!", parse_mode="Markdown")
    else:
        await safe_edit_message(callback, f"❌ Тур **№{tour_number}** вже існує.", parse_mode="Markdown")

    await asyncio.sleep(2)
    await state.clear()
    await back_to_admin_panel(callback, state)



@dp.callback_query(F.data == "admin_manage_directories", AdminPanel.main_menu)
async def admin_manage_directories(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "📚 **Керування довідниками**", reply_markup=get_directories_keyboard())
    await state.set_state(AdminPanel.directories_menu)

@dp.callback_query(F.data == "admin_edit_vehicle_number", AdminPanel.directories_menu)
async def admin_edit_vehicle_number_start(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    keyboard = await get_editable_vehicles_keyboard()
    await safe_edit_message(callback, "✏️ **Редагування номерів авто**\nОберіть авто для зміни номера:", reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(AdminPanel.editing_vehicle_number_select)

@dp.callback_query(F.data.startswith("edit_veh_num_"), AdminPanel.editing_vehicle_number_select)
async def admin_select_vehicle_to_edit(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    old_number = callback.data.split("edit_veh_num_")[1]
    await state.update_data(old_vehicle_number=old_number)
    await safe_edit_message(callback, f"Вибрано авто з номером: **{old_number}**.\n\nВведіть новий номерний знак:", parse_mode="Markdown")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(AdminPanel.editing_vehicle_number_new)

@dp.message(AdminPanel.editing_vehicle_number_new)
async def admin_process_new_vehicle_number(message: types.Message, state: FSMContext):
    match = VEHICLE_NUMBER_PATTERN.search(message.text)
    if not match:
        await message.answer("❗️ Невірний формат. Введіть `BC 1234 AA`.", reply_markup=CANCEL_KEYBOARD_REPLY)
        return

    new_number = f"{match.group(1).upper()}{match.group(2)}{match.group(3).upper()}"
    data = await state.get_data()
    old_number = data.get("old_vehicle_number")

    if await update_vehicle_number_in_gsheet(old_number, new_number):
        await message.answer(f"✅ Номер авто **{old_number}** успішно змінено на **{new_number}**.", reply_markup=ReplyKeyboardRemove())
    else:
        await message.answer(f"❌ Не вдалося оновити номер авто.", reply_markup=ReplyKeyboardRemove())

    await state.clear()
    await show_admin_panel(message, state)

@dp.callback_query(F.data == "dir_view_vehicles", AdminPanel.directories_menu)
async def dir_view_vehicles(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    worksheet = await get_gsheet_worksheet(VEHICLES_WORKSHEET_TITLE, VEHICLES_HEADERS)
    if not worksheet: await safe_callback_answer(callback, "Не вдалося відкрити довідник.", show_alert=True); return
    records = await asyncio.to_thread(worksheet.get_all_records, expected_headers=VEHICLES_HEADERS)
    text = "🚗 **Довідник автомобілів:**\n\n" + ("\n".join([f"`{r.get('Номер авто')}` | **{r.get('Марка авто')}** | Норма: {r.get('Норма л/100км')}" for r in records]) if records else "Довідник порожній.")
    await safe_edit_message(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_directories_menu")]]), parse_mode="Markdown")

@dp.callback_query(F.data == "dir_manage_tours", AdminPanel.directories_menu)
async def dir_manage_tours(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    tours = await get_tours_from_gsheet()
    if not tours:
        await safe_edit_message(callback, "Довідник турів порожній.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_directories_menu")]]))
        return
    buttons = [[InlineKeyboardButton(text=f"№{t['Номер туру']} ({t['Відстань км']} км)", callback_data=f"edit_tour_{t['Номер туру']}")] for t in tours]
    buttons.append([InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_directories_menu")])
    await safe_edit_message(callback, "🗺️ **Керування турами**", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(AdminPanel.manage_tours_menu)

@dp.callback_query(F.data.startswith("edit_tour_"), AdminPanel.manage_tours_menu)
async def edit_tour_distance_start(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    tour_number = callback.data.split("edit_tour_")[1]
    await state.update_data(tour_to_edit=tour_number)
    await safe_edit_message(callback, f"Нова відстань (км) для туру **№{tour_number}**: ")
    await callback.message.answer("Для скасування натисніть кнопку нижче.", reply_markup=CANCEL_KEYBOARD_REPLY)
    await state.set_state(AdminPanel.editing_tour_distance)

@dp.message(AdminPanel.editing_tour_distance)
async def process_new_tour_distance(message: types.Message, state: FSMContext):
    try:
        new_distance = float(message.text.replace(',', '.'))
        if new_distance <= 0:
            await message.answer("❗️ Відстань має бути більше 0 км. Введіть ще раз:", reply_markup=CANCEL_KEYBOARD_REPLY)
            return
        data = await state.get_data()
        tour_number = data.get("tour_to_edit")
        if await update_tour_distance_in_db(tour_number, new_distance):
            await message.answer(f"✅ Відстань для туру №{tour_number} оновлено.", reply_markup=ReplyKeyboardRemove())
        else:
            await message.answer(f"❌ Не вдалося оновити відстань.", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        await show_admin_panel(message, state)
    except (ValueError, TypeError):
        await message.answer("❗️Невірний формат, введіть число.", reply_markup=CANCEL_KEYBOARD_REPLY)

@dp.callback_query(F.data == "back_to_directories_menu")
async def back_to_directories_menu(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await safe_edit_message(callback, "📚 **Керування довідниками**", reply_markup=get_directories_keyboard())
    await state.set_state(AdminPanel.directories_menu)

# ========================================
#       ФУНКЦІЇ НАГАДУВАНЬ
# ========================================
async def get_today_reports() -> dict:
    today = datetime.now().strftime('%d.%m.%Y'); reported_users = {}
    try:
        worksheet = await get_gsheet_worksheet(REPORTS_WORKSHEET_TITLE, REPORTS_HEADERS)
        if worksheet:
            records = await asyncio.to_thread(worksheet.get_all_records, expected_headers=REPORTS_HEADERS)
            users_data = await get_all_users_from_gsheet()
            user_names = {f"{u.get('first_name', '')} {u.get('last_name', '')}".strip(): uid for uid, u in users_data.items()}
            for record in records:
                if record.get("Дата звіту") == today:
                    if (full_name := record.get("ПІБ Водія", "")) in user_names: reported_users[user_names[full_name]] = True
    except Exception as e: logging.error(f"Помилка отримання звітів за сьогодні: {e}")
    return reported_users

async def send_reminder(bot: Bot, user_id: str, reminder_count: int):
    try:
        # Створюємо текст нагадування
        message_text = f"⚠️ **Нагадування #{reminder_count}**: Ви ще не подали звіт за сьогодні."

        if reminder_count == REMINDER_MAX_COUNT:
            message_text += "\n\n**⚠️ Останнє попередження!** Якщо звіт не буде подано до 18:00, система автоматично зареєструє вихідний день."
        else:
            message_text += f"\n\n📋 Будь ласка, подайте звіт про рейс або оберіть 'Вихідний' в головному меню."

        # Надсилаємо повідомлення
        await bot.send_message(chat_id=int(user_id), text=message_text, parse_mode="Markdown")

        # Оновлюємо трекер нагадувань
        REMINDER_TRACKER[user_id] = {
            "count": reminder_count,
            "last_sent": datetime.now()
        }

        logging.info(f"Нагадування #{reminder_count} надіслано користувачу {user_id}")

    except Exception as e:
        logging.error(f"Помилка надсилання нагадування користувачу {user_id}: {e}")
        return False

    return True

async def send_manual_reminder(bot: Bot, user_id: str, user_name: str):
    """Надсилає ручне нагадування від адміністратора"""
    try:
        message_text = (f"🔔 **Ручне нагадування від адміністратора**\n\n"
                       f"Будь ласка, подайте звіт про рейс за сьогодні.\n"
                       f"Якщо у вас вихідний, оберіть відповідну опцію в боті.")
        await bot.send_message(chat_id=int(user_id), text=message_text, parse_mode="Markdown")
        logging.info(f"Ручне нагадування надіслано користувачу {user_name} ({user_id})")
        return True
    except Exception as e:
        logging.error(f"Помилка надсилання ручного нагадування {user_id}: {e}")
        return False

async def create_automatic_day_off_report(user_id: str):
    try:
        user_info = await get_user_from_gsheet(user_id)
        if not user_info: return False
        report_data = {"driver_first_name": user_info.get("first_name", ""), "driver_last_name": user_info.get("last_name", ""), "report_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "tour_number": "Вихідний (авто)", "tabel_number": 0, "vehicle_number": "N/A", "vehicle_brand": "N/A", "fuel_type": "N/A", "consumption_rate": 0, "fuel_card": user_info.get("fuel_card", "N/A"), "distance": 0, "actual_refill": 0}
        if user_info.get("vehicles"):
            last_vehicle = user_info["vehicles"][-1]
            report_data.update({k: last_vehicle.get(k, "N/A") for k in ["vehicle_number", "vehicle_brand", "fuel_type"]})
            report_data["consumption_rate"] = last_vehicle.get("consumption_rate", 0)
        await append_report_to_gsheet(report_data)
        await bot.send_message(chat_id=int(user_id), text="🔄 **Авто-звіт**: зареєстровано вихідний день.", parse_mode="Markdown")
        return True
    except Exception as e: logging.error(f"Помилка створення авто-звіту для {user_id}: {e}"); return False

async def check_all_vehicles_oil_change():
    logging.info("Перевірка заміни масла для всіх авто")
    try:
        worksheet = await get_gsheet_worksheet(MILEAGE_WORKSHEET_TITLE, MILEAGE_HEADERS)
        if not worksheet: return
        records = await asyncio.to_thread(worksheet.get_all_records, expected_headers=MILEAGE_HEADERS)
        for record in records:
            vehicle_number = record.get("Номер авто")
            try:
                mileage_since_oil = float(str(record.get("Пробіг з останньої заміни масла км", 0)).replace(',', '.'))
            except (ValueError, TypeError):
                continue

            if mileage_since_oil >= OIL_CHANGE_KM:
                # Перевіряємо, чи не було повідомлення за останню добу, щоб уникнути спаму
                last_notification_key = f"oil_notif_{vehicle_number}"
                if last_notification_key in all_users_cache:
                    continue # Пропускаємо, якщо вже сповіщали нещодавно

                await notify_oil_change_needed(vehicle_number, mileage_since_oil)
                # Кешуємо факт відправки повідомлення на 24 години
                all_users_cache[last_notification_key] = True

    except Exception as e: logging.error(f"Помилка перевірки заміни масла: {e}")

async def check_missing_reports():
    # Отримуємо поточний час в UTC та конвертуємо в український час
    utc_now = datetime.utcnow()
    ukraine_hour = (utc_now.hour + UKRAINE_UTC_OFFSET) % 24

    # Перевіряємо, чи зараз час для нагадувань (13:00-18:00 за українським часом)
    reminder_start_ukraine = 13  # 13:00 за українським часом
    reminder_end_ukraine = 18    # 18:00 за українським часом

    if not (reminder_start_ukraine <= ukraine_hour <= reminder_end_ukraine):
        return

    logging.info(f"Перевірка звітів о {ukraine_hour}:00 (український час)")

    try:
        reported_users = await get_today_reports()
        users_data = await get_all_users_from_gsheet()

        drivers_count = 0
        reminded_count = 0

        for user_id, user_info in users_data.items():
            # Нормалізуємо роль для перевірки
            user_role = user_info.get("role", "").strip().lower()
            if user_role != "водій":
                continue

            drivers_count += 1

            if user_id in reported_users:
                # Користувач вже подав звіт, видаляємо з трекера
                if user_id in REMINDER_TRACKER:
                    del REMINDER_TRACKER[user_id]
                continue

            # Користувач не подав звіт, надсилаємо нагадування
            reminder_count = REMINDER_TRACKER.get(user_id, {}).get("count", 0) + 1

            if reminder_count > REMINDER_MAX_COUNT:
                # Максимум нагадувань досягнуто, створюємо автоматичний звіт про вихідний
                if await create_automatic_day_off_report(user_id):
                    if user_id in REMINDER_TRACKER:
                        del REMINDER_TRACKER[user_id]
                    logging.info(f"Створено автоматичний звіт про вихідний для користувача {user_id}")
                continue

            # Надсилаємо нагадування
            await send_reminder(bot, user_id, reminder_count)
            reminded_count += 1

        logging.info(f"Перевірка завершена: {drivers_count} водіїв, {reminded_count} нагадувань надіслано")

    except Exception as e:
        logging.error(f"Помилка перевірки звітів: {e}")

async def reset_daily_reminders():
    """Очищає трекер нагадувань о півночі"""
    global REMINDER_TRACKER
    REMINDER_TRACKER.clear()
    logging.info("Трекер нагадувань очищено о півночі")

# ========================================
#         ЗАПУСК БОТА
# ========================================
async def main():
    logging.info("Запуск бота...")
    initialize_gsheet_client()
    if AIOCRON_AVAILABLE:
        logging.info("Планувальник нагадувань активний")

        # Запускаємо перевірку заміни масла о 13:00 за українським часом (11:00 UTC)
        aiocron.crontab('0 11 * * *')(check_all_vehicles_oil_change)
        logging.info("Заплановано перевірку заміни масла на 11:00 UTC (13:00 за Україною)")

        # Нагадування про звіти кожну годину з 11:00 до 16:00 UTC (13:00-18:00 за Україною)
        for hour in range(11, 17):  # 11, 12, 13, 14, 15, 16 UTC
            aiocron.crontab(f'0 {hour} * * *')(check_missing_reports)
        logging.info("Заплановано нагадування з 11:00 до 16:00 UTC (13:00-18:00 за Україною)")

        # Очищення трекера нагадувань о півночі за українським часом (22:00 UTC)
        aiocron.crontab('0 22 * * *')(reset_daily_reminders)
        logging.info("Заплановано очищення трекера нагадувань на 22:00 UTC (00:00 за Україною)")

    else:
        logging.warning("Планувальник ВИМКНЕНО! Встановіть 'aiocron' для роботи нагадувань")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот зупинено")
