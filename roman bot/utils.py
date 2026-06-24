# utils.py
import logging
import re
from aiogram import types
from aiogram.exceptions import TelegramBadRequest

VEHICLE_NUMBER_PATTERN = re.compile(r'^\s*([A-Za-zА-Яа-я]{2})\s*(\d{4})\s*([A-Za-zА-Яа-я]{2})\s*$', re.IGNORECASE)

async def safe_callback_answer(callback: types.CallbackQuery, text: str = None, show_alert: bool = False):
    """Безопасно отвечает на callback-запрос, игнорируя ошибки об истекшем времени."""
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest as e:
        if "query is too old" in str(e):
            logging.warning(f"Не удалось ответить на callback-запрос: {e}")
        else:
            raise
    except Exception as e:
        logging.error(f"Не удалось ответить на callback: {e}")

async def safe_edit_message(callback: types.CallbackQuery, text: str, **kwargs):
    """Безопасно редактирует сообщение, игнорируя ошибки 'message is not modified'."""
    try:
        await callback.message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            logging.info("Сообщение не было изменено, пропуск редактирования.")
        else:
            logging.error(f"Ошибка редактирования сообщения: {e}")
            # Не перевызываем ошибку, чтобы не прерывать выполнение
    except Exception as e:
        logging.error(f"Неожиданная ошибка при редактировании сообщения: {e}")

def parse_receipt_text(text: str) -> dict or None:
    """Распознает текст с чека для получения данных о топливе."""
    try:
        liters, price, check_code = None, None, None
        # Нормализация похожих символов
        normalization_map = {'E': 'Е', 'K': 'К', 'H': 'Н', 'C': 'С', 'M': 'М', 'B': 'В', 'e': 'е', 'k': 'к', 'h': 'н', 'c': 'с', 'm': 'м', 'b': 'в', 'a': 'а', 'o': 'о', 'p': 'р', 'i': 'і', 'x': 'х', 'y': 'у'}
        text_upper = text.upper()
        for lat, cyr in normalization_map.items():
            text_upper = text_upper.replace(lat.upper(), cyr.upper())

        lines = text_upper.split('\n')

        # Паттерны для поиска
        fuel_pattern = re.compile(r'(\d+[\.,]\d{2,3})\s*(?:Л|1)?\s*[\*Х>]\s*(\d+[\.,]\d{2})')
        check_keyword_pattern = re.compile(r'(?:ЧЕК|МЕК|НЕК|ЦЕК)')
        check_code_pattern = re.compile(r'(\d{8,10})')

        for line in lines:
            # Поиск литров и цены
            if not liters and not price:
                fuel_match = fuel_pattern.search(line)
                if fuel_match:
                    liters = float(fuel_match.group(1).replace(',', '.'))
                    price = float(fuel_match.group(2).replace(',', '.'))

            # Поиск кода чека
            if not check_code and check_keyword_pattern.search(line):
                code_match = check_code_pattern.search(line)
                if code_match:
                    check_code = code_match.group(1)

        if all([liters, price, check_code]):
            return {"liters": liters, "price_per_liter": price, "check_code": check_code}

        return None
    except Exception as e:
        logging.error(f"Ошибка при парсинге чека: {e}")
        return None
