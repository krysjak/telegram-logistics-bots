import asyncio
import io
import json
import logging
import re
from datetime import datetime, time, timedelta

# Нові імпорти для розпізнавання тексту та Google Sheets
import easyocr
import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, ReplyKeyboardMarkup,
                           ReplyKeyboardRemove)

# НОВИЙ ІМПОРТ: Додано бібліотеку для кешування
from cachetools import TTLCache 

# Імпорт для планування завдань - з обробкою помилки відсутності модуля
try:
    import aiocron
    AIOCRON_AVAILABLE = True
    logging.info("Модуль aiocron успішно імпортовано.")
except ModuleNotFoundError:
    AIOCRON_AVAILABLE = False
    logging.error("Модуль 'aiocron' не знайдено! Функціонал нагадувань буде вимкнено.")
    logging.error("Для встановлення виконайте: pip install aiocron cachetools")

# ========================================
#         НАЛАШТУВАННЯ ТА КОНСТАНТИ
# ========================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- ВАЖЛИВО! ВАШІ ДАНІ ВСТАВЛЕНО СЮДИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
SECRET_CODE = os.getenv("SECRET_CODE", "YOUR_SECRET_CODE")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "YOUR_SHEET_NAME")
ADMIN_USER_IDS = [int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()]

# Google service account credentials — load from JSON file or env
# See .env.example for setup instructions
_creds_path = os.getenv("GOOGLE_CREDS_PATH", "credentials.json")
GOOGLE_CREDS_JSON = {}
if os.path.exists(_creds_path):
    with open(_creds_path, "r") as _f:
        GOOGLE_CREDS_JSON = json.load(_f)
else:
    logging.warning("credentials.json not found — Google Sheets integration disabled")
  "universe_domain": "googleapis.com"
}
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

# --- ДИАГНОСТИКА ---
print(f"--- Завантажені ID адміністраторів: {ADMIN_USER_IDS} ---")

# Налаштування для нагадувань
REMINDER_START_HOUR = 13; REMINDER_MAX_COUNT = 5; REMINDER_INTERVAL = 1
# Налаштування для обслуговування авто
OIL_CHANGE_KM = 10000
# Словник для відслідковування надісланих нагадувань
REMINDER_TRACKER = {}
# Список марок авто
CAR_BRANDS = ["Mercedes-Benz", "Setra", "Neoplan", "Volvo", "MAN", "Scania", "DAF", "Iveco", "Renault", "Ford", "Volkswagen", "Temsa", "Isuzu", "Van Hool", "VDL", "Solaris", "Otokar"]

# Перевірка ключових змінних
if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE": logging.critical("ПОМИЛКА: BOT_TOKEN не заповнено!"); exit()
if GOOGLE_SHEET_NAME == "YOUR_GOOGLE_SHEET_NAME_HERE" or GOOGLE_CREDS_JSON.get("project_id") == "your-project-id": logging.critical("ПОМИЛКА: Змінні для Google Sheets не заповнено."); exit()

# Регулярні вирази
VEHICLE_NUMBER_PATTERN = re.compile(r'^\s*([A-Za-zА-Яа-я]{2})\s*(\d{4})\s*([A-Za-zА-Яа-я]{2})\s*$', re.IGNORECASE)

# Ініціалізація
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.info("Ініціалізація OCR-рідера...")
reader = easyocr.Reader(['uk', 'en'])
logging.info("OCR-рідер успішно ініціалізовано.")

# ОПТИМІЗАЦІЯ: Глобальний клієнт для Google Sheets та кеш
gsheet_client = None
# Кеш для даних окремих користувачів
user_cache = {}
# Кеш для списку всіх користувачів з "часом життя" 5 хвилин
all_users_cache = TTLCache(maxsize=1, ttl=300) 

# ========================================
#       ДОПОМІЖНІ ФУНКЦІЇ (GOOGLE SHEETS)
# ========================================

def initialize_gsheet_client():
    """ОПТИМІЗАЦІЯ: Ініціалізує клієнт gspread один раз при запуску."""
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
            gsheet_client = None # Встановлюємо None, щоб спробувати знову пізніше
    return gsheet_client

def is_admin(user_id: int) -> bool:
    """Перевіряє, чи має користувач адміністративні права"""
    return user_id in ADMIN_USER_IDS

async def get_gsheet_worksheet(worksheet_title: str, headers: list) -> gspread.Worksheet | None:
    """Отримує аркуш з Google таблиці, створює його з заголовками, якщо він не існує."""
    try:
        client = initialize_gsheet_client()
        if not client:
            return None
            
        spreadsheet = await asyncio.to_thread(client.open, GOOGLE_SHEET_NAME)
        
        try:
            worksheet = await asyncio.to_thread(spreadsheet.worksheet, worksheet_title)
            # Перевірка та оновлення заголовків
            first_row = await asyncio.to_thread(worksheet.row_values, 1)
            if not first_row or first_row != headers:
                await asyncio.to_thread(worksheet.clear)
                await asyncio.to_thread(worksheet.append_row, headers)
                await asyncio.to_thread(worksheet.format, 'A1:Z1', {'textFormat': {'bold': True}})
                logging.warning(f"Заголовки на аркуші '{worksheet_title}' були оновлені.")
        except gspread.exceptions.WorksheetNotFound:
            worksheet = await asyncio.to_thread(spreadsheet.add_worksheet, title=worksheet_title, rows="100", cols=len(headers) + 5)
            await asyncio.to_thread(worksheet.append_row, headers)
            await asyncio.to_thread(worksheet.format, 'A1:Z1', {'textFormat': {'bold': True}})
            logging.info(f"Створено новий аркуш '{worksheet_title}' із заголовками.")
        return worksheet
    except Exception as e:
        logging.error(f"Сталася помилка при роботі з Google Sheets: {e}")
        return None

# --- Нові функції для роботи з даними користувачів в Google Sheets ---

async def get_all_users_from_gsheet() -> dict:
    """ОПТИМІЗАЦІЯ: Завантажує дані всіх користувачів з кешу або Google Sheets."""
    if "all_users" in all_users_cache:
        logging.info("Отримано дані всіх користувачів з кешу.")
        return all_users_cache["all_users"]

    logging.info("Кеш застарів. Завантаження даних всіх користувачів з Google Sheets.")
    headers = ["user_id", "first_name", "last_name", "phone_number", "fuel_card", "vehicles_json"]
    worksheet = await get_gsheet_worksheet(USERS_WORKSHEET_TITLE, headers)
    if not worksheet:
        return {}
    
    users_data = {}
    records = await asyncio.to_thread(worksheet.get_all_records)
    for record in records:
        user_id = str(record.get("user_id"))
        if not user_id: continue
        
        vehicles_json = record.get("vehicles_json", "[]")
        try: vehicles = json.loads(vehicles_json)
        except json.JSONDecodeError: vehicles = []

        users_data[user_id] = {
            "first_name": record.get("first_name"), "last_name": record.get("last_name"),
            "phone_number": record.get("phone_number"), "fuel_card": record.get("fuel_card"), "vehicles": vehicles
        }
    
    all_users_cache["all_users"] = users_data  # Зберігаємо в кеш
    return users_data

async def get_user_from_gsheet(user_id: str) -> dict | None:
    """ОПТИМІЗАЦІЯ: Завантажує дані користувача з кешу або Google Sheets."""
    if user_id in user_cache:
        logging.info(f"Отримано дані користувача {user_id} з кешу.")
        return user_cache[user_id]

    logging.info(f"В кеші немає {user_id}. Завантаження з Google Sheets.")
    headers = ["user_id", "first_name", "last_name", "phone_number", "fuel_card", "vehicles_json"]
    worksheet = await get_gsheet_worksheet(USERS_WORKSHEET_TITLE, headers)
    if not worksheet: return None
    
    try:
        cell = await asyncio.to_thread(worksheet.find, user_id, in_column=1)
        if not cell: return None
        
        row_data = await asyncio.to_thread(worksheet.row_values, cell.row)
        
        vehicles_json = row_data[5] if len(row_data) > 5 else "[]"
        try: vehicles = json.loads(vehicles_json)
        except json.JSONDecodeError: vehicles = []

        user_data = {
            "first_name": row_data[1] if len(row_data) > 1 else "", "last_name": row_data[2] if len(row_data) > 2 else "",
            "phone_number": row_data[3] if len(row_data) > 3 else "", "fuel_card": row_data[4] if len(row_data) > 4 else "",
            "vehicles": vehicles
        }
        user_cache[user_id] = user_data # Зберігаємо в кеш
        return user_data
    except gspread.exceptions.CellNotFound: return None
    except Exception as e:
        logging.error(f"Помилка при зчитуванні даних користувача {user_id} з GSheet: {e}")
        return None

async def save_user_to_gsheet(user_id: str, user_data: dict):
    """ОПТИМІЗАЦІЯ: Зберігає дані користувача і оновлює кеш."""
    headers = ["user_id", "first_name", "last_name", "phone_number", "fuel_card", "vehicles_json"]
    worksheet = await get_gsheet_worksheet(USERS_WORKSHEET_TITLE, headers)
    if not worksheet:
        logging.error(f"Не вдалося зберегти дані для користувача {user_id}: аркуш не знайдено.")
        return

    vehicles_json = json.dumps(user_data.get("vehicles", []), ensure_ascii=False)
    row_data = [user_id, user_data.get("first_name", ""), user_data.get("last_name", ""), user_data.get("phone_number", ""), user_data.get("fuel_card", ""), vehicles_json]

    try:
        cell = await asyncio.to_thread(worksheet.find, user_id, in_column=1)
        if cell:
            await asyncio.to_thread(worksheet.update, f'A{cell.row}:F{cell.row}', [row_data])
            logging.info(f"Оновлено дані для користувача {user_id} в Google Sheets.")
        else:
            await asyncio.to_thread(worksheet.append_row, row_data)
            logging.info(f"Додано нового користувача {user_id} в Google Sheets.")
        
        # Оновлення кешу
        user_cache[user_id] = user_data
        if "all_users" in all_users_cache:
            all_users_cache["all_users"][user_id] = user_data
            
    except Exception as e:
        logging.error(f"Помилка при збереженні даних користувача {user_id} в GSheet: {e}")

# --- Інші функції для роботи з Google Sheets (з асинхронною оптимізацією) ---

async def get_vehicle_data_from_db(vehicle_number: str) -> dict:
    try:
        headers = ["Номер авто", "Тип палива", "Норма л/100км", "Марка авто"]
        worksheet = await get_gsheet_worksheet(VEHICLES_WORKSHEET_TITLE, headers)
        if worksheet:
            cell = await asyncio.to_thread(worksheet.find, vehicle_number, in_column=1)
            if cell:
                row_data = await asyncio.to_thread(worksheet.row_values, cell.row)
                return {"fuel_type": row_data[1] if len(row_data) > 1 else "Не вказано",
                        "consumption_rate": row_data[2] if len(row_data) > 2 else "Не вказано",
                        "vehicle_brand": row_data[3] if len(row_data) > 3 else "Не вказано"}
    except Exception as e:
        logging.error(f"Помилка при пошуку даних для {vehicle_number}: {e}")
    return {}

async def get_vehicle_mileage_from_db(vehicle_number: str) -> dict:
    try:
        headers = ["Номер авто", "Загальний пробіг км", "Пробіг з останньої заміни масла км", "Дата останньої заміни масла"]
        worksheet = await get_gsheet_worksheet(MILEAGE_WORKSHEET_TITLE, headers)
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
    headers = ["Номер авто", "Загальний пробіг км", "Пробіг з останньої заміни масла км", "Дата останньої заміни масла"]

    try:
        worksheet = await get_gsheet_worksheet(MILEAGE_WORKSHEET_TITLE, headers)
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

        await asyncio.to_thread(worksheet.update_cell, cell.row, 2, str(new_total_mileage))
        await asyncio.to_thread(worksheet.update_cell, cell.row, 3, str(new_mileage_since_oil))
        logging.info(f"Оновлено пробіг в базі для авто {vehicle_number}: загальний {new_total_mileage:.0f} км")
        if new_mileage_since_oil >= OIL_CHANGE_KM:
            asyncio.create_task(notify_oil_change_needed(vehicle_number, new_mileage_since_oil))
    except Exception as e:
        logging.error(f"Критична помилка при оновленні пробігу для {vehicle_number}: {e}")

async def notify_oil_change_needed(vehicle_number: str, mileage_since_oil_change: float):
    users_data = await get_all_users_from_gsheet()
    vehicle_owners_ids = [uid for uid, uinfo in users_data.items() if any(v.get("number") == vehicle_number for v in uinfo.get("vehicles", []))]
    if not vehicle_owners_ids: return

    message_text = (f"🛢️ **Увага! Заміна масла**\n\n"
                    f"Автомобіль **{vehicle_number}** потребує заміни масла.\n"
                    f"Поточний пробіг після останньої заміни: **{mileage_since_oil_change:.0f}** км "
                    f"(рекомендований інтервал: {OIL_CHANGE_KM} км).")
    for user_id in vehicle_owners_ids:
        try:
            await bot.send_message(chat_id=int(user_id), text=message_text, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Не вдалося відправити повідомлення про заміну масла користувачу {user_id}: {e}")

async def append_report_to_gsheet(report_data: dict, worksheet_title: str = REPORTS_WORKSHEET_TITLE):
    headers = ["ПІБ Водія", "Час звіту", "Дата звіту", "Номер авто", "Марка авто", "Тип палива", "Норма розходу", "Номер туру", "Табель", "Паливна картка", "КМ", "Планове паливо", "Фактична заправка", "Залишок палива"]
    worksheet = await get_gsheet_worksheet(worksheet_title, headers)
    if worksheet:
        full_name = f"{report_data.get('driver_first_name', '')} {report_data.get('driver_last_name', '')}".strip()
        dt_obj = datetime.strptime(report_data.get("report_time"), '%Y-%m-%d %H:%M:%S')
        row = [full_name, dt_obj.strftime('%H:%M:%S'), dt_obj.strftime('%d.%m.%Y'),
               report_data.get("vehicle_number"), report_data.get("vehicle_brand", "Не вказано"),
               report_data.get("fuel_type", "Не знайдено"), report_data.get("consumption_rate", "Не знайдено"),
               report_data.get("tour_number"), report_data.get("tabel_number", "N/A"),
               report_data.get("fuel_card", "Не вказано"), report_data.get("distance", "Не вказано"),
               report_data.get("planned_fuel", "Не вказано"), report_data.get("actual_refill", "Не вказано"),
               report_data.get("fuel_balance", "Не вказано")]
        await asyncio.to_thread(worksheet.append_row, row)
        if report_data.get("tour_number") == "Вихідний":
            all_values = await asyncio.to_thread(worksheet.get_all_values)
            last_row = len(all_values)
            await asyncio.to_thread(worksheet.format, f"H{last_row}", {"backgroundColor": {"red": 1.0, "green": 1.0, "blue": 0.0}})
        logging.info(f"Звіт для {full_name} по туру {report_data.get('tour_number')} збережено.")

async def append_fuel_report_to_gsheet(report_data: dict):
    headers = ["Дата та час", "ПІБ Водія", "Літри", "Ціна за літр", "Код чеку"]
    worksheet = await get_gsheet_worksheet(FUEL_REPORTS_WORKSHEET_TITLE, headers)
    if worksheet:
        row = [report_data.get("report_time"), report_data.get("driver_full_name"), report_data.get("liters"), report_data.get("price_per_liter"), report_data.get("check_code")]
        await asyncio.to_thread(worksheet.append_row, [str(item) for item in row], value_input_option='USER_ENTERED')

async def get_tours_from_gsheet() -> list[dict]:
    try:
        headers = ["Номер туру", "Відстань км", "Ким створено", "Дата створення"]
        worksheet = await get_gsheet_worksheet(TOURS_WORKSHEET_TITLE, headers)
        if worksheet:
            records = await asyncio.to_thread(worksheet.get_all_records)
            try: records.sort(key=lambda x: datetime.strptime(x.get("Дата створення", "1970-01-01 00:00:00"), '%Y-%m-%d %H:%M:%S'), reverse=True)
            except (ValueError, TypeError): logging.warning("Не вдалося відсортувати тури за датою.")
            return records
    except Exception as e:
        logging.error(f"Помилка при отриманні турів з Google Sheets: {e}")
    return []

async def append_tour_to_gsheet(tour_data: dict):
    headers = ["Номер туру", "Відстань км", "Ким створено", "Дата створення"]
    worksheet = await get_gsheet_worksheet(TOURS_WORKSHEET_TITLE, headers)
    if worksheet:
        cell = await asyncio.to_thread(worksheet.find, str(tour_data.get("tour_number")), in_column=1)
        if cell:
            logging.warning(f"Тур з номером {tour_data.get('tour_number')} вже існує.")
            return False
        row = [tour_data.get("tour_number"), tour_data.get("distance"), tour_data.get("created_by"), tour_data.get("created_at")]
        await asyncio.to_thread(worksheet.append_row, [str(item) for item in row], value_input_option='USER_ENTERED')
        return True
    return False

async def update_tour_distance_in_db(tour_number: str, new_distance: float) -> bool:
    headers = ["Номер туру", "Відстань км", "Ким створено", "Дата створення"]
    worksheet = await get_gsheet_worksheet(TOURS_WORKSHEET_TITLE, headers)
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
        headers = ["ПІБ Водія", "Час звіту", "Дата звіту", "Номер авто", "Марка авто", "Тип палива", "Норма розходу", "Номер туру", "Табель", "Паливна картка", "КМ", "Планове паливо", "Фактична заправка", "Залишок палива"]
        worksheet = await get_gsheet_worksheet(REPORTS_WORKSHEET_TITLE, headers)
        if not worksheet: return False
        all_records = await asyncio.to_thread(worksheet.get_all_records)
        for record in all_records:
            if str(record.get("Номер туру")) == str(tour_number) and record.get("Дата звіту") == report_date:
                return True
        return False
    except Exception as e:
        logging.error(f"Помилка при перевірці дублікатів звітів: {e}")
        return False

async def append_maintenance_log_to_gsheet(data: dict):
    headers = ["Дата", "Номер авто", "Пробіг", "Тип робіт", "Коментар", "Виконавець"]
    worksheet = await get_gsheet_worksheet(MAINTENANCE_WORKSHEET_TITLE, headers)
    if worksheet:
        row = [datetime.now().strftime('%d.%m.%Y %H:%M'), data.get("vehicle_number"), data.get("mileage"), data.get("work_type"), data.get("comment"), data.get("driver_name")]
        await asyncio.to_thread(worksheet.append_row, row)

async def save_vehicle_to_gsheet(vehicle_data: dict):
    headers = ["Номер авто", "Тип палива", "Норма л/100км", "Марка авто"]
    worksheet = await get_gsheet_worksheet(VEHICLES_WORKSHEET_TITLE, headers)
    if worksheet:
        cell = await asyncio.to_thread(worksheet.find, vehicle_data.get("number", ""), in_column=1)
        if cell:
            await asyncio.to_thread(worksheet.update_cell, cell.row, 2, vehicle_data.get("fuel_type", "Не вказано"))
            await asyncio.to_thread(worksheet.update_cell, cell.row, 3, str(vehicle_data.get("consumption_rate", "Не вказано")))
            await asyncio.to_thread(worksheet.update_cell, cell.row, 4, vehicle_data.get("brand", "Не вказано"))
        else:
            row = [vehicle_data.get("number", ""), vehicle_data.get("fuel_type", "Не вказано"), str(vehicle_data.get("consumption_rate", "Не вказано")), vehicle_data.get("brand", "Не вказано")]
            await asyncio.to_thread(worksheet.append_row, row)

async def record_oil_change_in_db(vehicle_number: str) -> bool:
    try:
        headers = ["Номер авто", "Загальний пробіг км", "Пробіг з останньої заміни масла км", "Дата останньої заміни масла"]
        worksheet = await get_gsheet_worksheet(MILEAGE_WORKSHEET_TITLE, headers)
        if not worksheet: return False
        
        cell = await asyncio.to_thread(worksheet.find, vehicle_number, in_column=1)
        if cell:
            await asyncio.to_thread(worksheet.update_cell, cell.row, 3, "0")
            await asyncio.to_thread(worksheet.update_cell, cell.row, 4, datetime.now().strftime('%d.%m.%Y'))
            return True
        return False
    except Exception as e: 
        logging.error(f"Помилка при записі заміни масла для {vehicle_number} в Google Sheets: {e}")
        return False

# --- Функції, що не взаємодіють з GSheet або вже були асинхронними ---
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
    except TelegramBadRequest: pass # Ігноруємо помилки застарілих колбеків
async def safe_edit_message(callback: types.CallbackQuery, text: str, **kwargs):
    try: await callback.message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): logging.error(f"Error editing message: {e}")

# ========================================
#         СТАНИ FSM (без змін)
# ========================================
class Registration(StatesGroup):
    waiting_for_first_name = State(); waiting_for_last_name = State(); waiting_for_phone = State(); waiting_for_fuel_card = State()
    waiting_for_secret_code = State(); adding_vehicle_prompt = State(); adding_vehicle_number = State(); adding_vehicle_brand = State()
    adding_fuel_type = State(); adding_consumption_rate = State()
class ProfileManagement(StatesGroup):
    viewing_profile = State(); editing_first_name = State(); editing_last_name = State(); editing_fuel_card = State()
class TourManagement(StatesGroup):
    creating_tour_number = State(); creating_tour_distance = State(); viewing_tours = State()
class Reporting(StatesGroup):
    waiting_for_vehicle_choice = State(); waiting_for_db_lookup_number = State(); manual_vehicle_number = State()
    manual_brand_choice = State(); manual_brand_input = State(); manual_fuel_type = State(); manual_consumption_rate = State()
    waiting_for_tour_number = State(); waiting_for_duplicate_confirmation = State(); waiting_for_manual_tour_distance = State()
    waiting_for_planned_fuel = State(); waiting_for_actual_refill = State(); waiting_for_fuel_balance = State()
    waiting_for_datetime_choice = State(); waiting_for_manual_datetime = State(); waiting_for_trip_confirmation = State()
class FuelReport(StatesGroup):
    waiting_for_receipt_photo = State(); waiting_for_manual_liters = State(); waiting_for_manual_price = State()
    waiting_for_manual_check_code = State(); waiting_for_edit_or_confirm = State(); waiting_for_edit_liters = State()
    waiting_for_edit_price = State(); waiting_for_edit_check_code = State()
class OilChangeManagement(StatesGroup):
    selecting_vehicle = State(); confirming_oil_change = State()
class Maintenance(StatesGroup):
    selecting_vehicle = State(); entering_mileage = State(); entering_work_type = State(); entering_comment = State(); confirming = State()
class AdminPanel(StatesGroup):
    main_menu = State(); directories_menu = State(); manage_tours_menu = State(); editing_tour_distance = State()

# ========================================
#         КЛАВІАТУРИ (зміни в get_tour_selection_keyboard)
# ========================================
def get_main_menu_keyboard(is_user_admin: bool = False) -> ReplyKeyboardMarkup:
    keyboard = [[KeyboardButton(text="📋 Надіслати звіт про рейс")], [KeyboardButton(text="⛽️ Надіслати чек АЗС")], [KeyboardButton(text="👤 Профіль")]]
    if is_user_admin: keyboard.append([KeyboardButton(text="👑 Панель адміністратора")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
def get_admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗺️ Створити новий тур", callback_data="admin_create_tour")], [InlineKeyboardButton(text="📚 Керування довідниками", callback_data="admin_manage_directories")], [InlineKeyboardButton(text="↩️ Назад до головного меню", callback_data="back_to_main_menu_from_admin")]])
def get_directories_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚗 Автомобілі (перегляд)", callback_data="dir_view_vehicles")], [InlineKeyboardButton(text="🗺️ Тури (редагування)", callback_data="dir_manage_tours")], [InlineKeyboardButton(text="↩️ Назад до адмін-панелі", callback_data="back_to_admin_panel")]])
def get_car_brand_keyboard() -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=brand, callback_data=f"brand_{brand}") for brand in CAR_BRANDS]
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]; keyboard.append([InlineKeyboardButton(text="➡️ Інша марка (ввести вручну)", callback_data="brand_manual_brand")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)
def get_datetime_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🕒 використати поточний час", callback_data="dt_current_time")],[InlineKeyboardButton(text="📅 ввести дату і час вручну", callback_data="dt_manual_time")]])
def get_trip_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Так, зберегти", callback_data="confirm_trip_report")], [InlineKeyboardButton(text="✏️ Авто", callback_data="edit_trip_vehicle"), InlineKeyboardButton(text="✏️ Номер туру", callback_data="edit_trip_tour_number")], [InlineKeyboardButton(text="✏️ Планове паливо", callback_data="edit_trip_planned_fuel"), InlineKeyboardButton(text="✏️ Факт. заправка", callback_data="edit_trip_actual_refill")], [InlineKeyboardButton(text="✏️ Залишок палива", callback_data="edit_trip_fuel_balance"), InlineKeyboardButton(text="✏️ Час", callback_data="edit_trip_datetime")], [InlineKeyboardButton(text="❌ Скасувати звіт", callback_data="cancel_trip_report")]])
def get_fuel_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Так, зберегти", callback_data="confirm_fuel_report")],[InlineKeyboardButton(text="✏️ Літри", callback_data="edit_fuel_liters"),InlineKeyboardButton(text="✏️ Ціна", callback_data="edit_fuel_price"),InlineKeyboardButton(text="✏️ Код чеку", callback_data="edit_fuel_check_code")],[InlineKeyboardButton(text="❌ Скасувати операцію", callback_data="cancel_fuel_report")]])

async def get_tour_selection_keyboard() -> InlineKeyboardMarkup:
    """ОПТИМІЗАЦІЯ: Клавіатура створюється асинхронно"""
    tours = await get_tours_from_gsheet()
    buttons = []
    for tour in tours[:10]:
        tour_text = f"№{tour['Номер туру']} ({tour['Відстань км']} км)"
        buttons.append([InlineKeyboardButton(text=tour_text, callback_data=f"select_tour_{tour['Номер туру']}")])
    buttons.append([InlineKeyboardButton(text="✏️ Ввести тур вручну", callback_data="manual_tour_entry")])
    buttons.append([InlineKeyboardButton(text="🎉 Вихідний", callback_data="tour_day_off")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ========================================
#         БЛОК РЕЄСТРАЦІЇ ТА АВТОМОБІЛІВ
# ========================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = str(message.from_user.id)
    user_data = await get_user_from_gsheet(user_id)
    if user_data:
        await message.answer(f"З поверненням, {user_data.get('first_name', 'Друже')}! 👋",
                             reply_markup=get_main_menu_keyboard(is_admin(message.from_user.id)))
    else:
        await message.answer("👋 Вітаю! Давайте зареєструємось.\n\nВведіть ваше ім'я:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(Registration.waiting_for_first_name)
@dp.message(Registration.waiting_for_first_name)
async def process_first_name(message: types.Message, state: FSMContext): await state.update_data(first_name=message.text); await message.answer("Тепер введіть ваше прізвище:"); await state.set_state(Registration.waiting_for_last_name)
@dp.message(Registration.waiting_for_last_name)
async def process_last_name(message: types.Message, state: FSMContext): await state.update_data(last_name=message.text); await message.answer("Натисніть кнопку, щоб поділитися номером телефону.", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Поділитися номером", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)); await state.set_state(Registration.waiting_for_phone)
@dp.message(Registration.waiting_for_phone, F.contact)
async def process_phone(message: types.Message, state: FSMContext): await state.update_data(phone_number=message.contact.phone_number); await message.answer("Введіть номер вашої паливної картки:", reply_markup=ReplyKeyboardRemove()); await state.set_state(Registration.waiting_for_fuel_card)
@dp.message(Registration.waiting_for_fuel_card)
async def process_fuel_card(message: types.Message, state: FSMContext):
    await state.update_data(fuel_card=message.text.strip())
    await message.answer("Майже готово! Введіть секретний код."); await state.set_state(Registration.waiting_for_secret_code)

@dp.message(Registration.waiting_for_secret_code)
async def process_secret_code(message: types.Message, state: FSMContext):
    if message.text == SECRET_CODE:
        fsm_data = await state.get_data()
        new_user_data = {"first_name": fsm_data.get('first_name'), "last_name": fsm_data.get('last_name'),
                         "phone_number": fsm_data.get('phone_number'), "fuel_card": fsm_data.get('fuel_card'), "vehicles": []}
        await save_user_to_gsheet(str(message.from_user.id), new_user_data)
        await message.answer("✅ Реєстрацію завершено!\n\nБажаєте додати автомобіль?",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Так", callback_data="add_vehicle_yes")], [InlineKeyboardButton(text="Ні", callback_data="add_vehicle_no")]]))
        await state.set_state(Registration.adding_vehicle_prompt)
    else: await message.answer("Невірний код. Спробуйте ще раз.")

@dp.callback_query(F.data == "add_vehicle_no", Registration.adding_vehicle_prompt)
async def prompt_add_vehicle_no(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); await state.clear()
    await safe_edit_message(callback, "Добре, ви зможете додати авто пізніше.")
    await callback.message.answer("Ви у головному меню:", reply_markup=get_main_menu_keyboard(is_admin(callback.from_user.id)))

@dp.callback_query(F.data == "add_vehicle_yes")
async def prompt_add_vehicle_yes(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); await state.clear()
    await safe_edit_message(callback, "<b>Крок 1/4:</b> Введіть номерний знак (`BC 1234 AA`)", parse_mode="HTML")
    await state.set_state(Registration.adding_vehicle_number)

@dp.message(Registration.adding_vehicle_number)
async def process_add_vehicle_number(message: types.Message, state: FSMContext):
    match = VEHICLE_NUMBER_PATTERN.search(message.text)
    if not match: await message.answer("❗️ Невірний формат. Введіть `BC 1234 AA`."); return
    vehicle_number = f"{match.group(1).upper()}{match.group(2)}{match.group(3).upper()}"
    await state.update_data(reg_vehicle_number=vehicle_number)
    await message.answer("<b>Крок 2/4:</b> Введіть марку та модель.", parse_mode="HTML"); await state.set_state(Registration.adding_vehicle_brand)
@dp.message(Registration.adding_vehicle_brand)
async def process_add_vehicle_brand(message: types.Message, state: FSMContext): await state.update_data(reg_vehicle_brand=message.text); await message.answer("<b>Крок 3/4:</b> Введіть тип палива.", parse_mode="HTML"); await state.set_state(Registration.adding_fuel_type)
@dp.message(Registration.adding_fuel_type)
async def process_add_fuel_type(message: types.Message, state: FSMContext): await state.update_data(reg_fuel_type=message.text); await message.answer("<b>Крок 4/4:</b> Введіть норму розходу л/100км.", parse_mode="HTML"); await state.set_state(Registration.adding_consumption_rate)

@dp.message(Registration.adding_consumption_rate)
async def process_add_consumption_rate(message: types.Message, state: FSMContext):
    try:
        rate = float(message.text.replace(',', '.')); await state.update_data(reg_consumption_rate=rate)
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
        else: await message.answer("Помилка, профіль не знайдено. /start"); await state.clear()
    except (ValueError, TypeError): await message.answer("❗️Невірний формат, введіть число.")

# ========================================
#         БЛОК ЗВІТІВ ПРО РЕЙС
# ========================================
def get_vehicle_selection_keyboard(user_vehicles: list) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=f"🚙 {v['brand']} ({v['number']})", callback_data=f"select_vehicle_{v['number']}")] for v in user_vehicles] if user_vehicles else []
    buttons.extend([[InlineKeyboardButton(text="➕ Ввести авто вручну", callback_data="manual_vehicle_entry")],
                    [InlineKeyboardButton(text="🔍 Знайти в базі", callback_data="database_vehicle_lookup")]])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def show_trip_data_for_confirmation(message_or_callback: types.Message | types.CallbackQuery, state: FSMContext):
    await state.update_data(is_editing=False)
    data = await state.get_data()
    try: time_str = datetime.strptime(data.get("report_time"), '%Y-%m-%d %H:%M:%S').strftime('%d.%m.%Y %H:%M')
    except (TypeError, ValueError): time_str = "не вказано"
    text = (f"**Перевірте фінальний звіт:**\n\n"
            f"🚚 **Марка**: `{data.get('vehicle_brand', 'не вказано')}`\n"
            f"🔢 **Номер**: `{data.get('vehicle_number', 'не вказано')}`\n"
            f"💧 **Паливо**: `{data.get('fuel_type', 'не знайдено')}`\n"
            f"📈 **Норма**: `{data.get('consumption_rate', 'не знайдено')}` л/100км\n"
            f"🔄 **Тур**: `{data.get('tour_number', 'не вказано')}`\n"
            f"⛽ **План**: `{data.get('planned_fuel', 'не вказано')}` л\n"
            f"🛢️ **Факт**: `{data.get('actual_refill', 'не вказано')}` л\n"
            f"⚖️ **Залишок**: `{data.get('fuel_balance', 'не вказано')}` л\n"
            f"🕒 **Час**: `{time_str}`\n"
            f"📋 **Табель**: `{data.get('tabel_number', 'N/A')}`\n\n"
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
    responder = safe_edit_message if isinstance(source, types.CallbackQuery) else (source.answer, source)

    if await check_if_report_exists(tour_number, today_str):
        await state.update_data(tour_number_to_confirm=tour_number)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚠️ Все одно надіслати", callback_data="force_submit_report")], [InlineKeyboardButton(text="❌ Обрати інший тур", callback_data="cancel_duplicate_report")]])
        msg_text = f"❗️ **Увага!** Звіт по туру **№{tour_number}** на сьогодні вже існує.\nНадіслати ще один (на з'ясування)?"
        if isinstance(source, types.CallbackQuery): await responder(source, msg_text, reply_markup=keyboard, parse_mode="Markdown")
        else: await responder[0](msg_text, reply_markup=keyboard, parse_mode="Markdown")
        await state.set_state(Reporting.waiting_for_duplicate_confirmation)
    else:
        await process_tour_selection_logic(tour_number, source, state)

@dp.message(F.text == "📋 Надіслати звіт про рейс")
async def start_trip_report(message: types.Message, state: FSMContext):
    await state.clear()
    user_data = await get_user_from_gsheet(str(message.from_user.id))
    if not user_data: await message.answer("Будь ласка, зареєструйтесь /start."); return
    user_vehicles = user_data.get("vehicles", [])
    await message.answer("Оберіть автомобіль для звіту:", reply_markup=get_vehicle_selection_keyboard(user_vehicles))
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
    await safe_callback_answer(callback); await safe_edit_message(callback, "<b>Введіть номер авто</b> (`BC 1234 AA`):", parse_mode="HTML")
    await state.set_state(Reporting.manual_vehicle_number)

@dp.callback_query(F.data == "database_vehicle_lookup", Reporting.waiting_for_vehicle_choice)
async def process_database_vehicle_choice(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); await safe_edit_message(callback, "Введіть номер авто для пошуку:")
    await state.set_state(Reporting.waiting_for_db_lookup_number)

@dp.message(Reporting.waiting_for_db_lookup_number)
async def process_db_lookup_number(message: types.Message, state: FSMContext):
    match = VEHICLE_NUMBER_PATTERN.search(message.text)
    if not match: await message.answer("❗️ Невірний формат."); return
    vehicle_number = f"{match.group(1).upper()}{match.group(2)}{match.group(3).upper()}"
    vehicle_data = await get_vehicle_data_from_db(vehicle_number)
    if not vehicle_data or not vehicle_data.get("vehicle_brand"): await message.answer("Авто не знайдено."); return
    await state.update_data(vehicle_number=vehicle_number, **vehicle_data)
    await message.answer(f"Знайдено: <b>{vehicle_data.get('vehicle_brand')} ({vehicle_number})</b>.", parse_mode="HTML")
    await go_to_tour_number_step(message, state)

@dp.message(Reporting.manual_vehicle_number)
async def process_manual_report_vehicle_number(message: types.Message, state: FSMContext):
    match = VEHICLE_NUMBER_PATTERN.search(message.text)
    if not match: await message.answer("❗️ Невірний формат."); return
    vehicle_number = f"{match.group(1).upper()}{match.group(2)}{match.group(3).upper()}"
    await state.update_data(vehicle_number=vehicle_number)
    await message.answer("Оберіть або введіть марку авто:", reply_markup=get_car_brand_keyboard())
    await state.set_state(Reporting.manual_brand_choice)

@dp.callback_query(F.data.startswith("brand_"), Reporting.manual_brand_choice)
async def process_manual_report_brand_choice(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); brand = callback.data.split("brand_")[1]
    if brand == "manual_brand":
        await safe_edit_message(callback, "Введіть марку авто вручну:")
        await state.set_state(Reporting.manual_brand_input)
    else:
        await state.update_data(vehicle_brand=brand); await safe_edit_message(callback, f"Обрано: {brand}")
        await callback.message.answer("Введіть тип палива:"); await state.set_state(Reporting.manual_fuel_type)

@dp.message(Reporting.manual_brand_input)
async def process_manual_report_brand_input(message: types.Message, state: FSMContext):
    await state.update_data(vehicle_brand=message.text); await message.answer("Введіть тип палива:")
    await state.set_state(Reporting.manual_fuel_type)

@dp.message(Reporting.manual_fuel_type)
async def process_manual_fuel_type(message: types.Message, state: FSMContext):
    await state.update_data(fuel_type=message.text); await message.answer("Введіть норму розходу л/100км:")
    await state.set_state(Reporting.manual_consumption_rate)

@dp.message(Reporting.manual_consumption_rate)
async def process_manual_consumption_rate(message: types.Message, state: FSMContext):
    try:
        await state.update_data(consumption_rate=float(message.text.replace(',', '.')))
        await go_to_tour_number_step(message, state)
    except (ValueError, TypeError): await message.answer("❗️Невірний формат, введіть число.")

@dp.callback_query(F.data == "manual_tour_entry", Reporting.waiting_for_tour_number)
async def manual_tour_entry(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); await safe_edit_message(callback, "Введіть номер туру/маршруту:")

@dp.callback_query(F.data.startswith("select_tour_"), Reporting.waiting_for_tour_number)
async def process_tour_selection(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await check_tour_and_proceed(callback.data.split("select_tour_")[1], callback, state)

async def process_tour_selection_logic(tour_number: str, source: types.Message | types.CallbackQuery, state: FSMContext):
    message_obj = source.message if isinstance(source, types.CallbackQuery) else source
    tours = await get_tours_from_gsheet()
    selected_tour = next((t for t in tours if str(t.get("Номер туру")) == tour_number), None)
    if not selected_tour:
        await message_obj.answer("Помилка: тур не знайдено.", reply_markup=await get_tour_selection_keyboard()); return

    await state.update_data(tour_number=tour_number, tabel_number=1)
    data = await state.get_data()
    consumption_rate = data.get("consumption_rate"); distance_str = str(selected_tour.get("Відстань км", '0')).replace(',', '.')
    message_text = f"Обрано тур №{tour_number}.\n"
    try:
        distance = float(distance_str); rate = float(consumption_rate)
        planned_fuel = (distance * rate) / 100
        await state.update_data(planned_fuel=planned_fuel, distance=distance)
        message_text += f"План палива: {planned_fuel:.2f} л.\n\nВведіть фактичну заправку (л):"
        await state.set_state(Reporting.waiting_for_actual_refill)
    except (ValueError, TypeError, AttributeError):
        message_text += "Не вдалося розрахувати план.\n\nВведіть планові витрати палива (л):"
        await state.set_state(Reporting.waiting_for_planned_fuel)
    if isinstance(source, types.CallbackQuery): await safe_edit_message(source, message_text)
    else: await source.answer(message_text)

@dp.callback_query(F.data == "tour_day_off", Reporting.waiting_for_tour_number)
async def process_tour_day_off(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await state.update_data(tour_number="Вихідний", tabel_number=0, planned_fuel=0, actual_refill=0, fuel_balance=0, distance=0)
    await safe_edit_message(callback, "Обрано: <b>Вихідний</b>", parse_mode="HTML")
    await callback.message.answer("Оберіть час:", reply_markup=get_datetime_choice_keyboard())
    await state.set_state(Reporting.waiting_for_datetime_choice)
    
@dp.message(Reporting.waiting_for_tour_number)
async def process_tour_number_message(message: types.Message, state: FSMContext):
    await check_tour_and_proceed(message.text.strip(), message, state)

@dp.callback_query(F.data == "force_submit_report", Reporting.waiting_for_duplicate_confirmation)
async def force_submit_duplicate_report(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); await state.update_data(is_clarification_report=True)
    data = await state.get_data(); tour_number = data.get("tour_number_to_confirm")
    await safe_edit_message(callback, f"Звіт по туру №{tour_number} буде надіслано на з'ясування.")
    await process_tour_selection_logic(tour_number, callback, state)
    
@dp.callback_query(F.data == "cancel_duplicate_report", Reporting.waiting_for_duplicate_confirmation)
async def cancel_duplicate_report(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); await state.update_data(is_clarification_report=False)
    await safe_edit_message(callback, "Добре, оберіть інший тур:", reply_markup=await get_tour_selection_keyboard())
    await state.set_state(Reporting.waiting_for_tour_number)

@dp.message(Reporting.waiting_for_manual_tour_distance)
async def process_manual_tour_distance(message: types.Message, state: FSMContext):
    try:
        distance = float(message.text.replace(',', '.')); await state.update_data(distance=distance); data = await state.get_data()
        consumption_rate = data.get("consumption_rate")
        if consumption_rate:
            planned_fuel = (distance * float(consumption_rate)) / 100; await state.update_data(planned_fuel=planned_fuel)
            await message.answer(f"План палива: {planned_fuel:.2f} л.\n\nВведіть фактичну заправку (л):")
            await state.set_state(Reporting.waiting_for_actual_refill)
        else: await message.answer("Введіть планові витрати палива (л):"); await state.set_state(Reporting.waiting_for_planned_fuel)
    except ValueError: await message.answer("❗️ Невірний формат, введіть число.")

@dp.message(Reporting.waiting_for_planned_fuel)
async def process_planned_fuel_input(message: types.Message, state: FSMContext):
    try:
        await state.update_data(planned_fuel=float(message.text.replace(',', '.')))
        await message.answer("Введіть фактичну заправку (л):"); await state.set_state(Reporting.waiting_for_actual_refill)
    except ValueError: await message.answer("❗️ Невірний формат, введіть число.")
@dp.message(Reporting.waiting_for_actual_refill)
async def process_actual_refill_input(message: types.Message, state: FSMContext):
    try:
        await state.update_data(actual_refill=float(message.text.replace(',', '.')))
        await message.answer("Введіть перехідний залишок (л):"); await state.set_state(Reporting.waiting_for_fuel_balance)
    except ValueError: await message.answer("❗️ Невірний формат, введіть число.")
@dp.message(Reporting.waiting_for_fuel_balance)
async def process_fuel_balance_input(message: types.Message, state: FSMContext):
    try:
        await state.update_data(fuel_balance=float(message.text.replace(',', '.')))
        await message.answer("Оберіть час:", reply_markup=get_datetime_choice_keyboard())
        await state.set_state(Reporting.waiting_for_datetime_choice)
    except ValueError: await message.answer("❗️ Невірний формат, введіть число.")

@dp.callback_query(F.data == "edit_trip_planned_fuel", Reporting.waiting_for_trip_confirmation)
async def edit_trip_planned_fuel(callback: types.CallbackQuery, state: FSMContext): await safe_callback_answer(callback); await safe_edit_message(callback, "Введіть нові планові витрати (л):"); await state.set_state(Reporting.waiting_for_planned_fuel)
@dp.callback_query(F.data == "edit_trip_actual_refill", Reporting.waiting_for_trip_confirmation)
async def edit_trip_actual_refill(callback: types.CallbackQuery, state: FSMContext): await safe_callback_answer(callback); await safe_edit_message(callback, "Введіть нову фактичну заправку (л):"); await state.set_state(Reporting.waiting_for_actual_refill)
@dp.callback_query(F.data == "edit_trip_fuel_balance", Reporting.waiting_for_trip_confirmation)
async def edit_trip_fuel_balance(callback: types.CallbackQuery, state: FSMContext): await safe_callback_answer(callback); await safe_edit_message(callback, "Введіть новий залишок палива (л):"); await state.set_state(Reporting.waiting_for_fuel_balance)
@dp.callback_query(F.data == "edit_trip_datetime", Reporting.waiting_for_trip_confirmation)
async def edit_trip_report_datetime(callback: types.CallbackQuery, state: FSMContext): await safe_callback_answer(callback); await safe_edit_message(callback, "Оберіть час рейсу:", reply_markup=get_datetime_choice_keyboard()); await state.set_state(Reporting.waiting_for_datetime_choice)

@dp.callback_query(F.data == "confirm_trip_report", Reporting.waiting_for_trip_confirmation)
async def confirm_and_save_trip_report(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); await safe_edit_message(callback, "⏳ Зберігаю звіт...", reply_markup=None)
    data = await state.get_data()
    driver_info = await get_user_from_gsheet(str(callback.from_user.id))
    if not driver_info: await safe_edit_message(callback, "❌ Помилка: профіль не знайдено."); await state.clear(); return

    if data.get("vehicle_number") and data.get("vehicle_brand"):
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
        logging.error(f"Фінальна помилка збереження звіту: {e}"); await safe_edit_message(callback, "❌ Помилка збереження.")
    finally:
        await state.clear(); await callback.message.answer("Ви у головному меню:", reply_markup=get_main_menu_keyboard(is_admin(callback.from_user.id)))

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
    if not await get_user_from_gsheet(str(message.from_user.id)): await message.answer("Доступно лише для водіїв."); return
    await state.clear(); await message.answer("Надішліть фото чека з АЗС.", reply_markup=ReplyKeyboardRemove()); await state.set_state(FuelReport.waiting_for_receipt_photo)

@dp.message(FuelReport.waiting_for_receipt_photo, F.photo)
async def handle_receipt_photo(message: types.Message, state: FSMContext):
    await message.answer("⏳ Аналізую фото..."); photo_bytes = io.BytesIO(); await bot.download(message.photo[-1], destination=photo_bytes)
    try:
        # Виконуємо розпізнавання у фоновому потоці, щоб не блокувати бота
        result = await asyncio.to_thread(reader.readtext, photo_bytes.getvalue(), detail=0, paragraph=True)
        full_text = "\n".join(result); parsed_data = parse_receipt_text(full_text)
        if parsed_data: await state.update_data(**parsed_data); await show_fuel_data_for_confirmation(message, state)
        else: await message.answer("❌ Не вдалося розпізнати. Введіть вручну.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✏️ Ввести вручну", callback_data="fuel_manual_start")]]))
    except Exception as e: logging.error(f"Помилка OCR: {e}"); await message.answer("❌ Помилка обробки зображення."); await state.clear()

@dp.callback_query(F.data == "fuel_manual_start")
async def start_manual_fuel_input(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); await safe_edit_message(callback, "Введіть кількість літрів:"); await state.set_state(FuelReport.waiting_for_manual_liters)
@dp.message(FuelReport.waiting_for_manual_liters)
async def process_manual_liters(message: types.Message, state: FSMContext):
    try: await state.update_data(liters=float(message.text.replace(',', '.'))); await message.answer("Введіть ціну за літр:"); await state.set_state(FuelReport.waiting_for_manual_price)
    except ValueError: await message.answer("❗️Невірний формат.")
@dp.message(FuelReport.waiting_for_manual_price)
async def process_manual_price(message: types.Message, state: FSMContext):
    try: await state.update_data(price_per_liter=float(message.text.replace(',', '.'))); await message.answer("Введіть код чеку:"); await state.set_state(FuelReport.waiting_for_manual_check_code)
    except ValueError: await message.answer("❗️Невірний формат.")
@dp.message(FuelReport.waiting_for_manual_check_code)
async def process_manual_check_code(message: types.Message, state: FSMContext): await state.update_data(check_code=message.text); await show_fuel_data_for_confirmation(message, state)
@dp.callback_query(F.data.startswith("edit_fuel_"), FuelReport.waiting_for_edit_or_confirm)
async def edit_fuel_report_field(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); field = callback.data.split("edit_fuel_")[1]
    if field == "liters": await state.set_state(FuelReport.waiting_for_edit_liters); await safe_edit_message(callback, "Нова кількість літрів:")
    elif field == "price": await state.set_state(FuelReport.waiting_for_edit_price); await safe_edit_message(callback, "Нова ціна:")
    elif field == "check_code": await state.set_state(FuelReport.waiting_for_edit_check_code); await safe_edit_message(callback, "Новий код чеку:")
@dp.message(FuelReport.waiting_for_edit_liters)
async def process_edited_liters(message: types.Message, state: FSMContext):
    try: await state.update_data(liters=float(message.text.replace(',', '.'))); await show_fuel_data_for_confirmation(message, state)
    except ValueError: await message.answer("❗️Невірний формат.")
@dp.message(FuelReport.waiting_for_edit_price)
async def process_edited_price(message: types.Message, state: FSMContext):
    try: await state.update_data(price_per_liter=float(message.text.replace(',', '.'))); await show_fuel_data_for_confirmation(message, state)
    except ValueError: await message.answer("❗️Невірний формат.")
@dp.message(FuelReport.waiting_for_edit_check_code)
async def process_edited_check_code(message: types.Message, state: FSMContext):
    await state.update_data(check_code=message.text); await show_fuel_data_for_confirmation(message, state)

@dp.callback_query(F.data == "confirm_fuel_report", FuelReport.waiting_for_edit_or_confirm)
async def confirm_and_save_fuel_report(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); await safe_edit_message(callback, "⏳ Зберігаю...", reply_markup=None)
    data = await state.get_data(); driver_info = await get_user_from_gsheet(str(callback.from_user.id))
    if not driver_info: await safe_edit_message(callback, "❌ Помилка: профіль не знайдено."); await state.clear(); return
    report_data = {"report_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "driver_full_name": f"{driver_info.get('first_name', '')} {driver_info.get('last_name', '')}".strip(), **data}
    try:
        await append_fuel_report_to_gsheet(report_data); await safe_edit_message(callback, "✅ Звіт по пальному збережено!")
    except Exception as e:
        logging.error(f"Помилка збереження звіту по пальному: {e}"); await safe_edit_message(callback, "❌ Помилка збереження.")
    finally:
        await state.clear(); await callback.message.answer("Ви у головному меню:", reply_markup=get_main_menu_keyboard(is_admin(callback.from_user.id)))

@dp.callback_query(F.data == "cancel_fuel_report", FuelReport.waiting_for_edit_or_confirm)
async def cancel_fuel_report(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); await state.clear(); await safe_edit_message(callback, "Операцію скасовано.")
    await callback.message.answer("Ви у головному меню:", reply_markup=get_main_menu_keyboard(is_admin(callback.from_user.id)))

@dp.callback_query(F.data == "dt_current_time", Reporting.waiting_for_datetime_choice)
async def use_current_time(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); await state.update_data(report_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    await safe_edit_message(callback, "Час встановлено на поточний."); await show_trip_data_for_confirmation(callback, state)
@dp.callback_query(F.data == "dt_manual_time", Reporting.waiting_for_datetime_choice)
async def prompt_manual_datetime(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); await safe_edit_message(callback, "Введіть дату і час у форматі `ДД.ММ.РРРР ГГ:ХХ`:"); await state.set_state(Reporting.waiting_for_manual_datetime)
@dp.message(Reporting.waiting_for_manual_datetime)
async def process_manual_datetime(message: types.Message, state: FSMContext):
    try:
        manual_time = datetime.strptime(message.text, '%d.%m.%Y %H:%M'); await state.update_data(report_time=manual_time.strftime('%Y-%m-%d %H:%M:%S'))
        await message.answer("Час встановлено."); await show_trip_data_for_confirmation(message, state)
    except ValueError: await message.answer("❗️ Невірний формат. Введіть `ДД.ММ.РРРР ГГ:ХХ`.")

# ========================================
#         БЛОК ПРОФІЛЮ КОРИСТУВАЧА
# ========================================
@dp.message(F.text == "👤 Профіль")
async def show_user_profile(message: types.Message, state: FSMContext):
    await state.clear(); user_data = await get_user_from_gsheet(str(message.from_user.id))
    if not user_data: await message.answer("Зареєструйтесь через /start."); return
    
    profile_text = (f"👤 **Ваш профіль:**\n\n"
        f"**Ім'я:** {user_data.get('first_name', 'N/A')}\n**Прізвище:** {user_data.get('last_name', 'N/A')}\n"
        f"**Телефон:** {user_data.get('phone_number', 'N/A')}\n**Паливна картка:** {user_data.get('fuel_card', 'N/A')}\n"
        f"**Кількість авто:** {len(user_data.get('vehicles', []))}")
    keyboard_buttons = [[InlineKeyboardButton(text="✏️ Ім'я", callback_data="edit_profile_first_name"), InlineKeyboardButton(text="✏️ Прізвище", callback_data="edit_profile_last_name")],
                        [InlineKeyboardButton(text="✏️ Паливна картка", callback_data="edit_profile_fuel_card")],
                        [InlineKeyboardButton(text="� Керувати авто", callback_data="manage_vehicles")], [InlineKeyboardButton(text="🔧 Записати ТО", callback_data="record_maintenance")],
                        [InlineKeyboardButton(text="↩️ Головне меню", callback_data="back_to_main_menu")]]
    await message.answer(profile_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons), parse_mode="Markdown")
    await state.set_state(ProfileManagement.viewing_profile)

@dp.callback_query(F.data == "edit_profile_first_name", ProfileManagement.viewing_profile)
async def edit_profile_first_name(callback: types.CallbackQuery, state: FSMContext): await safe_callback_answer(callback); await safe_edit_message(callback, "Введіть нове ім'я:"); await state.set_state(ProfileManagement.editing_first_name)
@dp.message(ProfileManagement.editing_first_name)
async def process_edited_first_name(message: types.Message, state: FSMContext):
    user_id = str(message.from_user.id); user_data = await get_user_from_gsheet(user_id)
    if user_data:
        user_data["first_name"] = message.text.strip(); await save_user_to_gsheet(user_id, user_data)
        await message.answer(f"Ім'я змінено!"); await show_user_profile(message, state)
    else: await message.answer("Помилка: профіль не знайдено.")

@dp.callback_query(F.data == "edit_profile_last_name", ProfileManagement.viewing_profile)
async def edit_profile_last_name(callback: types.CallbackQuery, state: FSMContext): await safe_callback_answer(callback); await safe_edit_message(callback, "Введіть нове прізвище:"); await state.set_state(ProfileManagement.editing_last_name)
@dp.message(ProfileManagement.editing_last_name)
async def process_edited_last_name(message: types.Message, state: FSMContext):
    user_id = str(message.from_user.id); user_data = await get_user_from_gsheet(user_id)
    if user_data:
        user_data["last_name"] = message.text.strip(); await save_user_to_gsheet(user_id, user_data)
        await message.answer(f"Прізвище змінено!"); await show_user_profile(message, state)
    else: await message.answer("Помилка: профіль не знайдено.")

@dp.callback_query(F.data == "edit_profile_fuel_card", ProfileManagement.viewing_profile)
async def edit_profile_fuel_card(callback: types.CallbackQuery, state: FSMContext): await safe_callback_answer(callback); await safe_edit_message(callback, "Введіть новий номер картки:"); await state.set_state(ProfileManagement.editing_fuel_card)
@dp.message(ProfileManagement.editing_fuel_card)
async def process_edited_fuel_card(message: types.Message, state: FSMContext):
    user_id = str(message.from_user.id); user_data = await get_user_from_gsheet(user_id)
    if user_data:
        user_data["fuel_card"] = message.text.strip(); await save_user_to_gsheet(user_id, user_data)
        await message.answer(f"Номер картки змінено!"); await show_user_profile(message, state)
    else: await message.answer("Помилка: профіль не знайдено.")

@dp.callback_query(F.data == "manage_vehicles", ProfileManagement.viewing_profile)
async def manage_user_vehicles(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); user_data = await get_user_from_gsheet(str(callback.from_user.id))
    vehicles = user_data.get("vehicles", []) if user_data else []
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Додати авто", callback_data="add_vehicle_yes")], [InlineKeyboardButton(text="↩️ Назад до профілю", callback_data="back_to_profile")]])
    if not vehicles: await safe_edit_message(callback, "У вас немає доданих авто.", reply_markup=keyboard); return

    text = "🚗 **Ваші автомобілі:**\n\n"; tasks = []
    for vehicle in vehicles: tasks.append(get_vehicle_mileage_from_db(vehicle.get('number')))
    mileage_results = await asyncio.gather(*tasks)

    for i, vehicle in enumerate(vehicles):
        mileage_data = mileage_results[i]
        vehicle['total_mileage'] = mileage_data.get('total_mileage', 0)
        vehicle['mileage_since_oil_change'] = mileage_data.get('mileage_since_oil_change', 0)
        text += f"{i+1}. {vehicle['brand']} ({vehicle['number']})\n   Пробіг: {vehicle['total_mileage']:.0f} км | Заміна масла: {vehicle['mileage_since_oil_change']:.0f} км\n"
        if vehicle['mileage_since_oil_change'] >= OIL_CHANGE_KM: text += f"   ⚠️ **ПОТРІБНА ЗАМІНА МАСЛА!** ⚠️\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Додати авто", callback_data="add_vehicle_yes")], [InlineKeyboardButton(text="🛢️ Заміна масла", callback_data="record_oil_change")], [InlineKeyboardButton(text="↩️ Назад до профілю", callback_data="back_to_profile")]])
    await safe_edit_message(callback, text, reply_markup=keyboard, parse_mode="Markdown")

@dp.callback_query(F.data == "back_to_profile")
async def back_to_profile_menu(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await state.clear()
    try:
        await callback.message.delete()
    except TelegramBadRequest as e:
        if "message to delete not found" not in str(e):
            logging.error(f"Error deleting message: {e}")
    await show_user_profile(callback.message, state)
    

@dp.callback_query(F.data == "back_to_main_menu")
async def back_to_main_menu(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); await state.clear()
    await safe_edit_message(callback, "Ви у головному меню")
    await callback.message.answer("Оберіть опцію:", reply_markup=get_main_menu_keyboard(is_admin(callback.from_user.id)))

# ========================================
#         БЛОК ЗАМІНИ МАСЛА І ТО
# ========================================
@dp.callback_query(F.data == "record_oil_change", ProfileManagement.viewing_profile)
async def prompt_oil_change_vehicle_selection(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); user_data = await get_user_from_gsheet(str(callback.from_user.id))
    vehicles = user_data.get("vehicles", []) if user_data else []
    if not vehicles: await safe_edit_message(callback, "У вас немає авто.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_profile")]])); return
    buttons = [[InlineKeyboardButton(text=f"🚙 {v['brand']} ({v['number']})", callback_data=f"oil_select_{v['number']}")] for v in vehicles]
    buttons.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="back_to_profile")])
    await safe_edit_message(callback, "🛢️ **Заміна масла**: Оберіть авто:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(OilChangeManagement.selecting_vehicle)

@dp.callback_query(F.data.startswith("oil_select_"), OilChangeManagement.selecting_vehicle)
async def confirm_oil_change(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); vehicle_number = callback.data.split("oil_select_")[1]
    await state.update_data(selected_vehicle_for_oil_change=vehicle_number)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Так, підтвердити", callback_data="oil_confirm_yes")], [InlineKeyboardButton(text="❌ Ні, скасувати", callback_data="back_to_profile")]])
    await safe_edit_message(callback, f"Зареєструвати заміну масла для **{vehicle_number}**?\nЦе скине лічильник пробігу на **0**.", reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(OilChangeManagement.confirming_oil_change)

@dp.callback_query(F.data == "oil_confirm_yes", OilChangeManagement.confirming_oil_change)
async def process_oil_change_confirmation(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); data = await state.get_data(); vehicle_number = data.get("selected_vehicle_for_oil_change")
    if not vehicle_number: 
        await safe_edit_message(callback, "Помилка.")
        try:
            await callback.message.delete()
        except TelegramBadRequest as e:
            if "message to delete not found" not in str(e):
                logging.error(f"Error deleting message: {e}")
        await show_user_profile(callback.message, state)
        return
    
    if await record_oil_change_in_db(vehicle_number): 
        await safe_edit_message(callback, f"✅ Заміна масла для **{vehicle_number}** зареєстрована.", parse_mode="Markdown")
    else: 
        await safe_edit_message(callback, f"❌ Помилка оновлення даних для **{vehicle_number}**.", parse_mode="Markdown")
    
    await state.clear()
    await asyncio.sleep(2)
    try:
        await callback.message.delete()
    except TelegramBadRequest as e:
        if "message to delete not found" not in str(e):
            logging.error(f"Error deleting message: {e}")
    await show_user_profile(callback.message, state)
    

@dp.callback_query(F.data == "record_maintenance", ProfileManagement.viewing_profile)
async def start_maintenance_log(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); user_data = await get_user_from_gsheet(str(callback.from_user.id))
    vehicles = user_data.get("vehicles", []) if user_data else []
    if not vehicles: await safe_edit_message(callback, "У вас немає авто.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_profile")]])); return
    buttons = [[InlineKeyboardButton(text=f"🚙 {v['brand']} ({v['number']})", callback_data=f"maint_select_{v['number']}")] for v in vehicles]
    buttons.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="back_to_profile")])
    await safe_edit_message(callback, "🔧 **Запис ТО**: Оберіть авто:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(Maintenance.selecting_vehicle)

@dp.callback_query(F.data.startswith("maint_select_"), Maintenance.selecting_vehicle)
async def maintenance_vehicle_selected(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); vehicle_number = callback.data.split("maint_select_")[1]
    await state.update_data(vehicle_number=vehicle_number)
    await safe_edit_message(callback, f"Авто: **{vehicle_number}**.\nВведіть поточний пробіг (км):", parse_mode="Markdown")
    await state.set_state(Maintenance.entering_mileage)

@dp.message(Maintenance.entering_mileage)
async def maintenance_mileage_entered(message: types.Message, state: FSMContext):
    try: await state.update_data(mileage=int(message.text)); await message.answer("Введіть тип робіт:"); await state.set_state(Maintenance.entering_work_type)
    except (ValueError, TypeError): await message.answer("❗️Введіть пробіг цілим числом.")
@dp.message(Maintenance.entering_work_type)
async def maintenance_work_type_entered(message: types.Message, state: FSMContext): await state.update_data(work_type=message.text); await message.answer("Додайте коментар (необов'язково):"); await state.set_state(Maintenance.entering_comment)
@dp.message(Maintenance.entering_comment)
async def maintenance_comment_entered(message: types.Message, state: FSMContext):
    await state.update_data(comment=message.text); data = await state.get_data()
    text = (f"**Перевірка даних ТО:**\n\n`{data.get('vehicle_number')}`, пробіг `{data.get('mileage')}` км\n**Роботи:** `{data.get('work_type')}`\n**Коментар:** `{data.get('comment')}`\n\nВсе вірно?")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Так", callback_data="maint_confirm_yes")], [InlineKeyboardButton(text="❌ Скасувати", callback_data="back_to_profile")]])
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown"); await state.set_state(Maintenance.confirming)

@dp.callback_query(F.data == "maint_confirm_yes", Maintenance.confirming)
async def maintenance_confirmed(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); data = await state.get_data()
    user_info = await get_user_from_gsheet(str(callback.from_user.id))
    full_name = f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip() if user_info else "N/A"
    await append_maintenance_log_to_gsheet({**data, "driver_name": full_name})
    await safe_edit_message(callback, "✅ Запис про ТО збережено!")
    await state.clear()
    await asyncio.sleep(2)
    try:
        await callback.message.delete()
    except TelegramBadRequest as e:
        if "message to delete not found" not in str(e):
            logging.error(f"Error deleting message: {e}")
    await show_user_profile(callback.message, state)
    


# ========================================
#         БЛОК АДМІН-ПАНЕЛІ
# ========================================
@dp.message(F.text == "👑 Панель адміністратора")
async def show_admin_panel(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id): await message.answer("❌ Немає доступу."); return
    await state.clear(); await message.answer("👑 **Панель адміністратора**", reply_markup=get_admin_panel_keyboard(), parse_mode="Markdown")
    await state.set_state(AdminPanel.main_menu)
@dp.callback_query(F.data == "back_to_admin_panel")
async def back_to_admin_panel(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); await safe_edit_message(callback, "👑 **Панель адміністратора**", reply_markup=get_admin_panel_keyboard(), parse_mode="Markdown")
    await state.set_state(AdminPanel.main_menu)
@dp.callback_query(F.data == "back_to_main_menu_from_admin")
async def back_to_main_menu_from_admin(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); await state.clear()
    try:
        await callback.message.delete()
    except TelegramBadRequest as e:
        if "message to delete not found" not in str(e):
            logging.error(f"Error deleting message: {e}")
    await callback.message.answer("Ви у головному меню:", reply_markup=get_main_menu_keyboard(is_admin(callback.from_user.id)))
@dp.callback_query(F.data == "admin_create_tour", AdminPanel.main_menu)
async def admin_create_tour_start(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); await state.clear(); await safe_edit_message(callback, "Введіть **номер** нового туру:")
    await state.set_state(TourManagement.creating_tour_number)
@dp.message(TourManagement.creating_tour_number)
async def admin_process_tour_number(message: types.Message, state: FSMContext):
    await state.update_data(tour_number=message.text); await message.answer("Тепер введіть **відстань** в км:")
    await state.set_state(TourManagement.creating_tour_distance)
@dp.message(TourManagement.creating_tour_distance)
async def admin_process_tour_distance(message: types.Message, state: FSMContext):
    try:
        distance = float(message.text.replace(',', '.')); data = await state.get_data()
        user_info = await get_user_from_gsheet(str(message.from_user.id))
        full_name = f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip() if user_info else "Адмін"
        tour_data = {"tour_number": data.get("tour_number"), "distance": distance, "created_by": full_name, "created_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        if await append_tour_to_gsheet(tour_data): await message.answer(f"✅ Тур **№{tour_data['tour_number']}** ({distance} км) створено!")
        else: await message.answer(f"❌ Тур **№{tour_data['tour_number']}** вже існує.")
        await state.clear(); await message.answer("👑 **Панель адміністратора**", reply_markup=get_admin_panel_keyboard(), parse_mode="Markdown")
        await state.set_state(AdminPanel.main_menu)
    except (ValueError, TypeError): await message.answer("❗️Невірний формат, введіть число.")

@dp.callback_query(F.data == "admin_manage_directories", AdminPanel.main_menu)
async def admin_manage_directories(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); await safe_edit_message(callback, "📚 **Керування довідниками**", reply_markup=get_directories_keyboard())
    await state.set_state(AdminPanel.directories_menu)
@dp.callback_query(F.data == "dir_view_vehicles", AdminPanel.directories_menu)
async def dir_view_vehicles(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    headers = ["Номер авто", "Тип палива", "Норма л/100км", "Марка авто"]
    worksheet = await get_gsheet_worksheet(VEHICLES_WORKSHEET_TITLE, headers)
    if not worksheet: await safe_callback_answer(callback, "Не вдалося відкрити довідник.", show_alert=True); return
    records = await asyncio.to_thread(worksheet.get_all_records)
    text = "🚗 **Довідник автомобілів:**\n\n" + ("\n".join([f"`{r.get('Номер авто')}` | **{r.get('Марка авто')}** | Норма: {r.get('Норма л/100км')}" for r in records]) if records else "Довідник порожній.")
    await safe_edit_message(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_directories_menu")]]), parse_mode="Markdown")
@dp.callback_query(F.data == "dir_manage_tours", AdminPanel.directories_menu)
async def dir_manage_tours(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); tours = await get_tours_from_gsheet()
    if not tours: await safe_edit_message(callback, "Довідник турів порожній.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_directories_menu")]])); return
    buttons = [[InlineKeyboardButton(text=f"№{t['Номер туру']} ({t['Відстань км']} км)", callback_data=f"edit_tour_{t['Номер туру']}")] for t in tours]
    buttons.append([InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_directories_menu")])
    await safe_edit_message(callback, "🗺️ **Керування турами**", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(AdminPanel.manage_tours_menu)

@dp.callback_query(F.data.startswith("edit_tour_"), AdminPanel.manage_tours_menu)
async def edit_tour_distance_start(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); tour_number = callback.data.split("edit_tour_")[1]
    await state.update_data(tour_to_edit=tour_number); await safe_edit_message(callback, f"Нова відстань (км) для туру **№{tour_number}**: ")
    await state.set_state(AdminPanel.editing_tour_distance)
@dp.message(AdminPanel.editing_tour_distance)
async def process_new_tour_distance(message: types.Message, state: FSMContext):
    try:
        new_distance = float(message.text.replace(',', '.')); data = await state.get_data(); tour_number = data.get("tour_to_edit")
        if await update_tour_distance_in_db(tour_number, new_distance): await message.answer(f"✅ Відстань для туру №{tour_number} оновлено.")
        else: await message.answer(f"❌ Не вдалося оновити відстань.")
        await state.clear(); tours = await get_tours_from_gsheet()
        buttons = [[InlineKeyboardButton(text=f"№{t['Номер туру']} ({t['Відстань км']} км)", callback_data=f"edit_tour_{t['Номер туру']}")] for t in tours]
        buttons.append([InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_directories_menu")])
        await message.answer("🗺️ **Керування турами**", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)); await state.set_state(AdminPanel.manage_tours_menu)
    except (ValueError, TypeError): await message.answer("❗️Невірний формат, введіть число.")
@dp.callback_query(F.data == "back_to_directories_menu")
async def back_to_directories_menu(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback); await safe_edit_message(callback, "📚 **Керування довідниками**", reply_markup=get_directories_keyboard())
    await state.set_state(AdminPanel.directories_menu)

# ========================================
#       ФУНКЦІЇ НАГАДУВАНЬ
# ========================================
async def get_today_reports() -> dict:
    today = datetime.now().strftime('%d.%m.%Y'); reported_users = {}
    try:
        headers = ["ПІБ Водія", "Час звіту", "Дата звіту", "Номер авто", "Марка авто", "Тип палива", "Норма розходу", "Номер туру", "Табель", "Паливна картка", "КМ", "Планове паливо", "Фактична заправка", "Залишок палива"]
        worksheet = await get_gsheet_worksheet(REPORTS_WORKSHEET_TITLE, headers)
        if worksheet:
            records = await asyncio.to_thread(worksheet.get_all_records)
            users_data = await get_all_users_from_gsheet()
            user_names = {f"{u.get('first_name', '')} {u.get('last_name', '')}".strip(): uid for uid, u in users_data.items()}
            for record in records:
                if record.get("Дата звіту") == today:
                    if (full_name := record.get("ПІБ Водія", "")) in user_names: reported_users[user_names[full_name]] = True
    except Exception as e: logging.error(f"Помилка отримання звітів за сьогодні: {e}")
    return reported_users

async def send_reminder(bot: Bot, user_id: str, reminder_count: int):
    try:
        message_text = f"⚠️ **Нагадування #{reminder_count}**: Ви ще не подали звіт за сьогодні."
        if reminder_count == REMINDER_MAX_COUNT: message_text += "\n**Увага!** Якщо звіт не буде подано, система зареєструє вихідний."
        await bot.send_message(chat_id=int(user_id), text=message_text, parse_mode="Markdown")
        REMINDER_TRACKER[user_id] = {"count": reminder_count, "last_sent": datetime.now()}
    except Exception as e: logging.error(f"Помилка надсилання нагадування {user_id}: {e}")

async def create_automatic_day_off_report(user_id: str):
    try:
        user_info = await get_user_from_gsheet(user_id)
        if not user_info: return False
        report_data = {"driver_first_name": user_info.get("first_name", ""), "driver_last_name": user_info.get("last_name", ""), "report_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "tour_number": "Вихідний (авто)", "tabel_number": 0, "vehicle_number": "N/A", "vehicle_brand": "N/A", "fuel_type": "N/A", "consumption_rate": 0, "fuel_card": user_info.get("fuel_card", "N/A"), "distance": 0, "planned_fuel": 0, "actual_refill": 0, "fuel_balance": 0}
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
        headers = ["Номер авто", "Загальний пробіг км", "Пробіг з останньої заміни масла км", "Дата останньої заміни масла"]
        worksheet = await get_gsheet_worksheet(MILEAGE_WORKSHEET_TITLE, headers)
        if not worksheet: return
        records = await asyncio.to_thread(worksheet.get_all_records)
        for record in records:
            vehicle_number = record.get("Номер авто")
            try: mileage_since_oil = float(str(record.get("Пробіг з останньої заміни масла км", 0)).replace(',', '.'))
            except (ValueError, TypeError): continue
            if mileage_since_oil >= OIL_CHANGE_KM: await notify_oil_change_needed(vehicle_number, mileage_since_oil)
    except Exception as e: logging.error(f"Помилка перевірки заміни масла: {e}")

def setup_cron_job(schedule_str):
    def decorator(func):
        if AIOCRON_AVAILABLE: return aiocron.crontab(schedule_str)(func)
        return func
    return decorator

@setup_cron_job(f'0 {REMINDER_START_HOUR}-{REMINDER_START_HOUR+REMINDER_MAX_COUNT} * * *')
async def check_missing_reports():
    current_hour = datetime.now().hour
    if not (REMINDER_START_HOUR <= current_hour <= REMINDER_START_HOUR + REMINDER_MAX_COUNT): return
    logging.info(f"Перевірка звітів о {current_hour}:00")
    try:
        reported_users = await get_today_reports()
        users_data = await get_all_users_from_gsheet()
        for user_id, user_info in users_data.items():
            if user_id in reported_users:
                if user_id in REMINDER_TRACKER: del REMINDER_TRACKER[user_id]
                continue
            reminder_count = REMINDER_TRACKER.get(user_id, {}).get("count", 0) + 1
            if reminder_count > REMINDER_MAX_COUNT:
                if await create_automatic_day_off_report(user_id):
                    if user_id in REMINDER_TRACKER: del REMINDER_TRACKER[user_id]
                continue
            await send_reminder(bot, user_id, reminder_count)
    except Exception as e: logging.error(f"Помилка перевірки звітів: {e}")

async def main():
    logging.info("Запуск бота...")
    initialize_gsheet_client()
    if AIOCRON_AVAILABLE:
        logging.info(f"Планувальник нагадувань активний з {REMINDER_START_HOUR}:00")
        aiocron.crontab('0 9 * * *', func=check_all_vehicles_oil_change, start=True)
    else:
        logging.warning("Планувальник ВИМКНЕНО! Встановіть 'aiocron'")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): logging.info("Бот зупинено�")