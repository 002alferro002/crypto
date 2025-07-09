import os
from pathlib import Path
from typing import Dict, Any
import asyncio
import time

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    print("⚠️ Watchdog не установлен. Автоматическое обновление настроек недоступно.")
    print("Установите: pip install watchdog")

# Базовый путь проекта
BASE_DIR = Path(__file__).parent

# Путь к .env файлу
ENV_FILE_PATH = BASE_DIR / '.env'

# Глобальные переменные для системы обновления настроек
_settings_cache = {}
_last_modified = 0
_settings_callbacks = []
_file_observer = None

# Настройки по умолчанию
DEFAULT_SETTINGS = {
    # Настройки сервера
    'SERVER_HOST': '0.0.0.0',
    'SERVER_PORT': '8000',

    # Настройки базы данных
    'DATABASE_URL': 'postgresql://user:password@localhost:5432/cryptoscan',
    'DB_HOST': 'localhost',
    'DB_PORT': '5432',
    'DB_NAME': 'cryptoscan',
    'DB_USER': 'user',
    'DB_PASSWORD': 'password',

    # Настройки анализа объемов
    'ANALYSIS_HOURS': '1',
    'OFFSET_MINUTES': '0',
    'VOLUME_MULTIPLIER': '2.0',
    'MIN_VOLUME_USDT': '1000',
    'CONSECUTIVE_LONG_COUNT': '5',
    'ALERT_GROUPING_MINUTES': '5',
    'DATA_RETENTION_HOURS': '2',
    'UPDATE_INTERVAL_SECONDS': '1',
    'PAIRS_CHECK_INTERVAL_MINUTES': '30',
    'PRICE_CHECK_INTERVAL_MINUTES': '5',

    # Настройки фильтра цен
    'PRICE_HISTORY_DAYS': '30',
    'PRICE_DROP_PERCENTAGE': '10.0',
    'WATCHLIST_AUTO_UPDATE': 'True',

    # Настройки Telegram
    'TELEGRAM_BOT_TOKEN': '',
    'TELEGRAM_CHAT_ID': '',

    # Настройки Bybit API
    'BYBIT_API_KEY': '',
    'BYBIT_API_SECRET': '',

    # Настройки логирования
    'LOG_LEVEL': 'INFO',
    'LOG_FILE': 'cryptoscan.log',

    # Настройки WebSocket
    'WS_PING_INTERVAL': '20',
    'WS_PING_TIMEOUT': '10',
    'WS_CLOSE_TIMEOUT': '10',
    'WS_MAX_SIZE': '10000000',

    # Настройки синхронизации времени
    'TIME_SYNC_INTERVAL': '300',
    'TIME_SERVER_SYNC_INTERVAL': '3600',

    # Настройки имбаланса
    'MIN_GAP_PERCENTAGE': '0.1',
    'MIN_STRENGTH': '0.5',
    'FAIR_VALUE_GAP_ENABLED': 'True',
    'ORDER_BLOCK_ENABLED': 'True',
    'BREAKER_BLOCK_ENABLED': 'True',

    # Настройки стакана
    'ORDERBOOK_ENABLED': 'False',
    'ORDERBOOK_SNAPSHOT_ON_ALERT': 'False',

    # Настройки алертов
    'VOLUME_ALERTS_ENABLED': 'True',
    'CONSECUTIVE_ALERTS_ENABLED': 'True',
    'PRIORITY_ALERTS_ENABLED': 'True',
    'IMBALANCE_ENABLED': 'True',
    'NOTIFICATION_ENABLED': 'True',
    'VOLUME_TYPE': 'long',

    # Настройки социальных сетей
    'SOCIAL_SENTIMENT_ENABLED': 'False',
    'SOCIAL_ANALYSIS_PERIOD_HOURS': '72',
    'SOCIAL_MIN_MENTIONS_FOR_RATING': '3',
    'SOCIAL_CACHE_DURATION_MINUTES': '30',
}


class SettingsFileHandler(FileSystemEventHandler):
    """Обработчик изменений файла настроек"""

    def on_modified(self, event):
        if event.is_directory:
            return

        if event.src_path == str(ENV_FILE_PATH):
            asyncio.create_task(reload_settings())


def create_env_file():
    """Создание .env файла с настройками по умолчанию"""
    if ENV_FILE_PATH.exists():
        return

    with open(ENV_FILE_PATH, 'w', encoding='utf-8') as f:
        f.write("# Настройки CryptoScan\n")
        f.write("# Этот файл создан автоматически. Измените значения по необходимости.\n\n")

        # Группируем настройки по категориям
        categories = {
            'Сервер': ['SERVER_HOST', 'SERVER_PORT'],
            'База данных': ['DATABASE_URL', 'DB_HOST', 'DB_PORT', 'DB_NAME', 'DB_USER', 'DB_PASSWORD'],
            'Анализ объемов': ['ANALYSIS_HOURS', 'OFFSET_MINUTES', 'VOLUME_MULTIPLIER', 'MIN_VOLUME_USDT',
                               'CONSECUTIVE_LONG_COUNT', 'ALERT_GROUPING_MINUTES', 'DATA_RETENTION_HOURS',
                               'UPDATE_INTERVAL_SECONDS', 'PAIRS_CHECK_INTERVAL_MINUTES'],
            'Фильтр цен': ['PRICE_CHECK_INTERVAL_MINUTES', 'PRICE_HISTORY_DAYS', 'PRICE_DROP_PERCENTAGE'],
            'Watchlist': ['WATCHLIST_AUTO_UPDATE'],
            'Telegram': ['TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'],
            'Bybit API': ['BYBIT_API_KEY', 'BYBIT_API_SECRET'],
            'Логирование': ['LOG_LEVEL', 'LOG_FILE'],
            'WebSocket': ['WS_PING_INTERVAL', 'WS_PING_TIMEOUT', 'WS_CLOSE_TIMEOUT', 'WS_MAX_SIZE'],
            'Синхронизация времени': ['TIME_SYNC_INTERVAL', 'TIME_SERVER_SYNC_INTERVAL'],
            'Имбаланс': ['MIN_GAP_PERCENTAGE', 'MIN_STRENGTH', 'FAIR_VALUE_GAP_ENABLED',
                         'ORDER_BLOCK_ENABLED', 'BREAKER_BLOCK_ENABLED'],
            'Стакан': ['ORDERBOOK_ENABLED', 'ORDERBOOK_SNAPSHOT_ON_ALERT'],
            'Алерты': ['VOLUME_ALERTS_ENABLED', 'CONSECUTIVE_ALERTS_ENABLED', 'PRIORITY_ALERTS_ENABLED',
                       'IMBALANCE_ENABLED', 'NOTIFICATION_ENABLED', 'VOLUME_TYPE'],
            'Социальные сети': ['SOCIAL_SENTIMENT_ENABLED', 'SOCIAL_ANALYSIS_PERIOD_HOURS',
                                'SOCIAL_MIN_MENTIONS_FOR_RATING', 'SOCIAL_CACHE_DURATION_MINUTES']
        }

        for category, keys in categories.items():
            f.write(f"# {category}\n")
            for key in keys:
                if key in DEFAULT_SETTINGS:
                    f.write(f"{key}={DEFAULT_SETTINGS[key]}\n")
            f.write("\n")


def load_settings() -> Dict[str, Any]:
    """Загрузка настроек из .env файла или создание файла с настройками по умолчанию"""
    global _settings_cache, _last_modified

    # Проверяем, изменился ли файл
    try:
        current_modified = ENV_FILE_PATH.stat().st_mtime
        if current_modified == _last_modified and _settings_cache:
            return _settings_cache
        _last_modified = current_modified
    except FileNotFoundError:
        pass

    # Создаем .env файл если его нет
    if not ENV_FILE_PATH.exists():
        create_env_file()

    # Загружаем настройки из .env файла
    settings = {}

    try:
        with open(ENV_FILE_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    settings[key.strip()] = value.strip()
    except Exception as e:
        print(f"Ошибка чтения .env файла: {e}")
        return DEFAULT_SETTINGS

    # Дополняем недостающие настройки значениями по умолчанию
    for key, default_value in DEFAULT_SETTINGS.items():
        if key not in settings:
            settings[key] = default_value

    _settings_cache = settings
    return settings


async def reload_settings():
    """Асинхронная перезагрузка настроек"""
    try:
        # Небольшая задержка для завершения записи файла
        await asyncio.sleep(0.1)

        # Очищаем кэш
        global _settings_cache
        _settings_cache = {}

        # Загружаем новые настройки
        new_settings = load_settings()

        # Уведомляем все зарегистрированные компоненты
        for callback in _settings_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(new_settings)
                else:
                    callback(new_settings)
            except Exception as e:
                print(f"Ошибка обновления настроек в компоненте: {e}")

        print(f"✅ Настройки перезагружены из .env файла")

    except Exception as e:
        print(f"❌ Ошибка перезагрузки настроек: {e}")


def register_settings_callback(callback):
    """Регистрация callback для уведомления об изменении настроек"""
    _settings_callbacks.append(callback)


def unregister_settings_callback(callback):
    """Отмена регистрации callback"""
    if callback in _settings_callbacks:
        _settings_callbacks.remove(callback)


def start_settings_monitor():
    """Запуск мониторинга изменений файла настроек"""
    global _file_observer

    if not WATCHDOG_AVAILABLE:
        print("⚠️ Мониторинг настроек недоступен - watchdog не установлен")
        return

    try:
        if _file_observer is None:
            event_handler = SettingsFileHandler()
            _file_observer = Observer()
            _file_observer.schedule(event_handler, str(BASE_DIR), recursive=False)
            _file_observer.start()
            print("🔍 Мониторинг изменений .env файла запущен")
    except Exception as e:
        print(f"❌ Ошибка запуска мониторинга настроек: {e}")


def stop_settings_monitor():
    """Остановка мониторинга изменений файла настроек"""
    global _file_observer

    if not WATCHDOG_AVAILABLE:
        return

    if _file_observer:
        _file_observer.stop()
        _file_observer.join()
        _file_observer = None
        print("🛑 Мониторинг изменений .env файла остановлен")


def get_setting(key: str, default: Any = None) -> Any:
    """Получение значения настройки"""
    settings = load_settings()
    value = settings.get(key, default)

    # Преобразование строковых значений в нужные типы
    if isinstance(value, str):
        if value.lower() in ('true', 'false'):
            return value.lower() == 'true'
        try:
            if '.' in value:
                return float(value)
            return int(value)
        except ValueError:
            return value

    return value


def update_setting(key: str, value: Any):
    """Обновление настройки в .env файле"""
    global _settings_cache

    settings = load_settings()
    settings[key] = str(value)

    # Обновляем кэш
    _settings_cache[key] = str(value)

    # Перезаписываем .env файл
    with open(ENV_FILE_PATH, 'w', encoding='utf-8') as f:
        f.write("# Настройки CryptoScan\n")
        f.write("# Обновлено автоматически\n\n")

        for key, value in settings.items():
            if not key.startswith('#'):
                f.write(f"{key}={value}\n")

    print(f"⚙️ Настройка {key} обновлена на {value}")


# Инициализация настроек при импорте модуля
SETTINGS = load_settings()