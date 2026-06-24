# Використовуємо офіційний образ Python як базовий
FROM python:3.12-slim-bullseye

# Встановлюємо необхідні системні залежності для easyocr
# Для успішної роботи можуть бути потрібні додаткові пакети
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git \
    libgl1-mesa-glx \
    libglib2.0-0 \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Встановлюємо робочу директорію всередині контейнера
WORKDIR /app

# Копіюємо файл з залежностями
COPY requirements.txt .

# Встановлюємо Python-залежності
RUN pip install --no-cache-dir -r requirements.txt

# Копіюємо всі файли проекту в контейнер
COPY . .

# Вказуємо команду для запуску бота
CMD ["python", "betav2.py"]

