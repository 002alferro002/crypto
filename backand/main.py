import asyncio
import os
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import List, Dict, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn
import json

# Импорты наших модулей
from settings import get_setting, register_settings_callback, start_settings_monitor, stop_settings_monitor
from core.core_logger import get_logger
from database.database_connection import DatabaseConnection
from database.database_tables import DatabaseTables
from database.database_queries import DatabaseQueries
from alert.alert_manager import AlertManager
from bybit.bybit_websocket import BybitWebSocketManager
from bybit.bybit_rest_api import BybitRestAPI
from filter.filter_price import PriceFilter
from telegram.telegram_bot import TelegramBot
from times.times_manager import TimeManager
from cryptoscan.backand.websocket.websocket_manager import ConnectionManager

logger = get_logger(__name__)


# Функция для обновления настроек во всех компонентах
async def update_all_components_settings(new_settings: Dict):
    """Обновление настроек во всех компонентах системы"""
    try:
        logger.info("🔄 Обновление настроек во всех компонентах...")

        # Обновляем настройки в alert_manager
        if alert_manager:
            alert_manager.update_settings(new_settings)

        # Обновляем настройки в price_filter
        if price_filter:
            price_filter.update_settings(new_settings)

        # Обновляем настройки в telegram_bot
        if telegram_bot:
            telegram_token = new_settings.get('TELEGRAM_BOT_TOKEN')
            telegram_chat = new_settings.get('TELEGRAM_CHAT_ID')
            if telegram_token or telegram_chat:
                telegram_bot.update_settings(telegram_token, telegram_chat)

        # Уведомляем клиентов об обновлении настроек
        if connection_manager:
            await connection_manager.broadcast_json({
                "type": "settings_updated",
                "message": "Настройки обновлены из .env файла",
                "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000)
            })

        logger.info("✅ Настройки успешно обновлены во всех компонентах")

    except Exception as e:
        logger.error(f"❌ Ошибка обновления настроек в компонентах: {e}")


# Глобальные переменные
db_connection = None
db_queries = None
alert_manager = None
bybit_websocket = None
bybit_api = None
price_filter = None
telegram_bot = None
time_manager = None
connection_manager = None


# Модели данных для API
class WatchlistAdd(BaseModel):
    symbol: str


class WatchlistUpdate(BaseModel):
    id: int
    symbol: str
    is_active: bool


class FavoriteAdd(BaseModel):
    symbol: str
    notes: Optional[str] = None
    color: Optional[str] = '#FFD700'


class FavoriteUpdate(BaseModel):
    notes: Optional[str] = None
    color: Optional[str] = None
    sort_order: Optional[int] = None


class FavoriteReorder(BaseModel):
    symbol_order: List[str]


class PaperTradeCreate(BaseModel):
    symbol: str
    trade_type: str  # 'LONG' or 'SHORT'
    entry_price: float
    quantity: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_amount: Optional[float] = None
    risk_percentage: Optional[float] = None
    notes: Optional[str] = None
    alert_id: Optional[int] = None


class PaperTradeClose(BaseModel):
    exit_price: float
    exit_reason: Optional[str] = 'MANUAL'


class TradingSettingsUpdate(BaseModel):
    account_balance: Optional[float] = None
    max_risk_per_trade: Optional[float] = None
    max_open_trades: Optional[int] = None
    default_stop_loss_percentage: Optional[float] = None
    default_take_profit_percentage: Optional[float] = None
    auto_calculate_quantity: Optional[bool] = None


class RiskCalculatorRequest(BaseModel):
    entry_price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_amount: Optional[float] = None
    risk_percentage: Optional[float] = None
    account_balance: Optional[float] = None
    trade_type: str = 'LONG'


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global db_connection, db_queries, alert_manager, bybit_websocket, bybit_api
    global price_filter, telegram_bot, time_manager, connection_manager

    try:
        logger.info("🚀 Запуск системы анализа объемов...")

        # Инициализация менеджера WebSocket соединений
        connection_manager = ConnectionManager()

        # Инициализация синхронизации времени
        time_manager = TimeManager()
        await time_manager.start()
        logger.info("⏰ Синхронизация времени запущена")

        # Инициализация базы данных
        db_connection = DatabaseConnection()
        await db_connection.initialize()

        # Создание таблиц
        db_tables = DatabaseTables(db_connection)
        await db_tables.create_all_tables()

        # Инициализация запросов к БД
        db_queries = DatabaseQueries(db_connection)

        # Инициализация Telegram бота
        telegram_bot = TelegramBot()

        # Инициализация менеджера алертов
        alert_manager = AlertManager(db_queries, telegram_bot, connection_manager, time_manager)

        # Инициализация Bybit API
        bybit_api = BybitRestAPI()
        await bybit_api.start()

        # Инициализация фильтра цен
        price_filter = PriceFilter(db_queries)

        # Инициализация WebSocket менеджера Bybit
        bybit_websocket = BybitWebSocketManager(alert_manager, connection_manager)

        # Настраиваем callback для обновления пар
        async def on_pairs_updated(new_pairs, removed_pairs):
            """Callback для обновления пар в bybit_websocket"""
            if bybit_websocket:
                bybit_websocket.update_trading_pairs(new_pairs, removed_pairs)
                if new_pairs:
                    await bybit_websocket.subscribe_to_new_pairs(new_pairs)
                if removed_pairs:
                    await bybit_websocket.unsubscribe_from_pairs(removed_pairs)

        price_filter.set_pairs_updated_callback(on_pairs_updated)

        # Запуск всех сервисов
        logger.info("🔄 Запуск сервисов...")

        # Получаем начальный watchlist
        initial_watchlist = await db_queries.get_watchlist()
        if initial_watchlist:
            # Устанавливаем торговые пары в WebSocket менеджер
            bybit_websocket.trading_pairs = set(initial_watchlist)
            logger.info(f"📋 Установлен начальный watchlist: {len(initial_watchlist)} пар")

        # Запускаем фильтр цен
        if get_setting('WATCHLIST_AUTO_UPDATE', True):
            asyncio.create_task(price_filter.start())
        else:
            logger.info("🔍 Автоматическое обновление watchlist отключено")

        # Запускаем WebSocket клиент
        bybit_websocket.is_running = True
        asyncio.create_task(bybit_websocket_loop())

        asyncio.create_task(historical_data_loader())

        # Запуск периодической очистки данных
        asyncio.create_task(periodic_cleanup())

        # Запуск периодической очистки WebSocket соединений
        asyncio.create_task(connection_manager.start_periodic_cleanup())

        # Регистрируем callback для обновления настроек
        register_settings_callback(update_all_components_settings)

        # Запускаем мониторинг изменений .env файла
        start_settings_monitor()

        logger.info("✅ Система успешно запущена!")

    except Exception as e:
        logger.error(f"❌ Ошибка запуска системы: {e}")
        raise

    yield

    # Shutdown
    logger.info("🛑 Остановка системы...")

    # Останавливаем мониторинг настроек
    stop_settings_monitor()

    if time_manager:
        await time_manager.stop()
    if bybit_websocket:
        bybit_websocket.is_running = False
        await bybit_websocket.close()
    if bybit_api:
        await bybit_api.stop()
    if price_filter:
        await price_filter.stop()
    if db_connection:
        db_connection.close()


async def bybit_websocket_loop():
    """Цикл WebSocket соединения с переподключениями"""
    while bybit_websocket.is_running:
        try:
            await bybit_websocket.connect()
            # Если дошли сюда, соединение было успешным
            bybit_websocket.reconnect_attempts = 0

        except Exception as e:
            logger.error(f"❌ WebSocket ошибка: {e}")

            if bybit_websocket.is_running:
                bybit_websocket.reconnect_attempts += 1

                if bybit_websocket.reconnect_attempts <= bybit_websocket.max_reconnect_attempts:
                    delay = min(bybit_websocket.reconnect_delay * bybit_websocket.reconnect_attempts, 60)
                    logger.info(f"🔄 Переподключение через {delay} секунд... "
                                f"(попытка {bybit_websocket.reconnect_attempts}/{bybit_websocket.max_reconnect_attempts})")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"❌ Превышено максимальное количество попыток переподключения")
                    bybit_websocket.is_running = False
                    break


async def historical_data_loader():
    """Периодическая загрузка исторических данных"""
    while True:
        try:
            if db_queries and bybit_api:
                # Получаем текущий watchlist
                watchlist = await db_queries.get_watchlist()

                for symbol in watchlist:
                    try:
                        # Проверяем целостность данных
                        analysis_hours = get_setting('ANALYSIS_HOURS', 1)
                        offset_minutes = get_setting('OFFSET_MINUTES', 0)

                        # Рассчитываем период загрузки
                        total_hours = analysis_hours + (offset_minutes / 60)

                        integrity = await db_queries.check_data_integrity(symbol, int(total_hours * 60))  # в минутах

                        # Если целостность данных менее 90%, загружаем недостающие данные
                        if integrity['integrity_percentage'] < 90:
                            logger.info(
                                f"📊 Загрузка исторических данных для {symbol} (целостность: {integrity['integrity_percentage']:.1f}%)")

                            # Рассчитываем диапазон загрузки
                            current_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                            end_time_ms = current_time_ms - (offset_minutes * 60 * 1000)
                            start_time_ms = end_time_ms - (int(total_hours * 60) * 60 * 1000)

                            # Загружаем данные пакетами
                            batch_size_hours = 24  # 24 часа за раз
                            current_start = start_time_ms

                            while current_start < end_time_ms:
                                current_end = min(current_start + (batch_size_hours * 60 * 60 * 1000), end_time_ms)

                                try:
                                    klines = await bybit_api.get_kline_data(symbol, current_start, current_end)

                                    for kline in klines:
                                        # Сохраняем как закрытую свечу
                                        kline_data = {
                                            'start': kline['timestamp'],
                                            'end': kline['timestamp'] + 60000,  # +1 минута
                                            'open': kline['open'],
                                            'high': kline['high'],
                                            'low': kline['low'],
                                            'close': kline['close'],
                                            'volume': kline['volume']
                                        }

                                        await db_queries.save_historical_kline_data(symbol, kline_data)

                                    logger.debug(f"📊 Загружено {len(klines)} свечей для {symbol}")

                                except Exception as e:
                                    logger.error(
                                        f"❌ Ошибка загрузки данных для {symbol} в диапазоне {current_start}-{current_end}: {e}")

                                current_start = current_end
                                await asyncio.sleep(0.1)  # Небольшая задержка между запросами

                        await asyncio.sleep(0.5)  # Задержка между символами

                    except Exception as e:
                        logger.error(f"❌ Ошибка обработки исторических данных для {symbol}: {e}")
                        continue

            # Проверяем каждые 30 минут
            await asyncio.sleep(1800)

        except Exception as e:
            logger.error(f"❌ Ошибка загрузчика исторических данных: {e}")
            await asyncio.sleep(300)  # Повторить через 5 минут при ошибке


async def periodic_cleanup():
    """Периодическая очистка старых данных"""
    while True:
        try:
            await asyncio.sleep(3600)  # Каждый час
            if alert_manager:
                await alert_manager.cleanup_old_data()
            if db_queries:
                retention_hours = get_setting('DATA_RETENTION_HOURS', 2)
                # Здесь можно добавить очистку старых данных через db_queries
            logger.info("🧹 Периодическая очистка данных выполнена")
        except Exception as e:
            logger.error(f"❌ Ошибка периодической очистки: {e}")


app = FastAPI(title="Trading Volume Analyzer", lifespan=lifespan)


# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await connection_manager.connect(websocket)
    try:
        while True:
            # Ожидаем сообщения от клиента
            data = await websocket.receive_text()
            await connection_manager.handle_client_message(websocket, data)
    except WebSocketDisconnect:
        connection_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket ошибка: {e}")
        connection_manager.disconnect(websocket)


# API endpoints
@app.get("/api/stats")
async def get_stats():
    """Получить статистику системы"""
    try:
        if not db_queries:
            return {"error": "Database not initialized"}

        # Получаем статистику из базы данных
        watchlist = await db_queries.get_watchlist()
        # alerts_data = await db_queries.get_all_alerts(limit=1000)  # Будет реализовано
        # favorites = await db_queries.get_favorites()  # Будет реализовано
        # trading_stats = await db_queries.get_trading_statistics()  # Будет реализовано

        # Информация о синхронизации времени
        time_sync_info = {}
        if time_manager:
            time_sync_info = time_manager.get_sync_status()

        # Статистика подписок
        subscription_stats = {}
        if bybit_websocket:
            subscription_stats = bybit_websocket.get_connection_stats()

        return {
            "pairs_count": len(watchlist),
            "favorites_count": 0,  # Временно
            "alerts_count": 0,  # Временно
            "volume_alerts_count": 0,  # Временно
            "consecutive_alerts_count": 0,  # Временно
            "priority_alerts_count": 0,  # Временно
            "trading_stats": {},  # Временно
            "subscription_stats": subscription_stats,
            "last_update": datetime.now(timezone.utc).isoformat(),
            "system_status": "running",
            "time_sync": time_sync_info
        }
    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        return {"error": str(e)}


@app.get("/api/time")
async def get_time_info():
    """Получить информацию о времени биржи"""
    try:
        if time_manager:
            return time_manager.get_time_info()
        else:
            # Fallback на локальное UTC время
            current_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            return {
                "is_synced": False,
                "serverTime": current_time_ms,
                "local_time": datetime.now(timezone.utc).isoformat(),
                "utc_time": datetime.now(timezone.utc).isoformat(),
                "time_offset_ms": 0,
                "status": "not_synced"
            }
    except Exception as e:
        logger.error(f"Ошибка получения информации о времени: {e}")
        current_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        return {
            "is_synced": False,
            "serverTime": current_time_ms,
            "local_time": datetime.now(timezone.utc).isoformat(),
            "utc_time": datetime.now(timezone.utc).isoformat(),
            "time_offset_ms": 0,
            "status": "error",
            "error": str(e)
        }


@app.get("/api/watchlist")
async def get_watchlist():
    """Получить список торговых пар"""
    try:
        pairs = await db_queries.get_watchlist_details()
        return {"pairs": pairs}
    except Exception as e:
        logger.error(f"Ошибка получения watchlist: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/watchlist")
async def add_to_watchlist(item: WatchlistAdd):
    """Добавить торговую пару в watchlist"""
    try:
        await db_queries.add_to_watchlist(item.symbol)

        # Добавляем пару в WebSocket менеджер
        if bybit_websocket:
            bybit_websocket.trading_pairs.add(item.symbol)
            await bybit_websocket.subscribe_to_new_pairs({item.symbol})

        # Уведомляем клиентов об обновлении
        await connection_manager.broadcast_json({
            "type": "watchlist_updated",
            "action": "added",
            "symbol": item.symbol
        })

        return {"status": "success", "symbol": item.symbol}
    except Exception as e:
        logger.error(f"Ошибка добавления в watchlist: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/settings")
async def get_settings():
    """Получить текущие настройки анализатора"""
    try:
        # Информация о синхронизации времени
        time_sync_info = {}
        if time_manager:
            time_sync_info = time_manager.get_sync_status()

        settings = {
            "volume_analyzer": alert_manager.get_settings() if alert_manager else {},
            "price_filter": price_filter.get_settings() if price_filter else {},
            "watchlist": {
                "auto_update": get_setting('WATCHLIST_AUTO_UPDATE', True)
            },
            "alerts": {
                "volume_alerts_enabled": get_setting('VOLUME_ALERTS_ENABLED', True),
                "consecutive_alerts_enabled": get_setting('CONSECUTIVE_ALERTS_ENABLED', True),
                "priority_alerts_enabled": get_setting('PRIORITY_ALERTS_ENABLED', True)
            },
            "imbalance": {
                "fair_value_gap_enabled": get_setting('FAIR_VALUE_GAP_ENABLED', True),
                "order_block_enabled": get_setting('ORDER_BLOCK_ENABLED', True),
                "breaker_block_enabled": get_setting('BREAKER_BLOCK_ENABLED', True),
                "min_gap_percentage": get_setting('MIN_GAP_PERCENTAGE', 0.1),
                "min_strength": get_setting('MIN_STRENGTH', 0.5)
            },
            "orderbook": {
                "enabled": get_setting('ORDERBOOK_ENABLED', False),
                "snapshot_on_alert": get_setting('ORDERBOOK_SNAPSHOT_ON_ALERT', False)
            },
            "telegram": {
                "enabled": telegram_bot.enabled if telegram_bot else False
            },
            "time_sync": time_sync_info
        }

        return settings

    except Exception as e:
        logger.error(f"Ошибка получения настроек: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/settings")
async def update_settings(settings: dict):
    """Обновить настройки анализатора"""
    try:
        # Обновляем настройки в .env файле
        from settings import update_setting
        for key, value in settings.items():
            if isinstance(value, dict):
                # Для вложенных настроек
                for sub_key, sub_value in value.items():
                    env_key = sub_key.upper()
                    update_setting(env_key, sub_value)
            else:
                update_setting(key.upper(), value)

        # Обновляем настройки в компонентах (это произойдет автоматически через мониторинг файла)
        if alert_manager and 'volume_analyzer' in settings:
            alert_manager.update_settings(settings['volume_analyzer'])

        if alert_manager and 'alerts' in settings:
            alert_manager.update_settings(settings['alerts'])

        if alert_manager and 'imbalance' in settings:
            alert_manager.update_settings(settings['imbalance'])

        if price_filter and 'price_filter' in settings:
            price_filter.update_settings(settings['price_filter'])

        if price_filter and 'watchlist' in settings:
            price_filter.update_settings(settings['watchlist'])

        await connection_manager.broadcast_json({
            "type": "settings_updated",
            "data": settings
        })
        return {"status": "success", "settings": settings}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/watchlist/{symbol}")
@app.post("/api/settings/reload")
async def reload_settings_endpoint():
    """Принудительная перезагрузка настроек из .env файла"""
    try:
        from settings import reload_settings
        await reload_settings()

        return {"status": "success", "message": "Настройки перезагружены из .env файла"}
    except Exception as e:
        logger.error(f"Ошибка перезагрузки настроек: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def remove_from_watchlist(symbol: str):
    """Удалить торговую пару из watchlist"""
    try:
        await db_queries.remove_from_watchlist(symbol)

        # Удаляем пару из WebSocket менеджера
        if bybit_websocket:
            bybit_websocket.trading_pairs.discard(symbol)
            await bybit_websocket.unsubscribe_from_pairs({symbol})

        # Уведомляем клиентов об обновлении
        await connection_manager.broadcast_json({
            "type": "watchlist_updated",
            "action": "removed",
            "symbol": symbol
        })

        return {"status": "success", "symbol": symbol}
    except Exception as e:
        logger.error(f"Ошибка удаления из watchlist: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Проверяем существование директории dist перед монтированием
if os.path.exists("dist"):
    if os.path.exists("dist/assets"):
        app.mount("/assets", StaticFiles(directory="dist/assets"), name="assets")


    @app.get("/vite.svg")
    async def get_vite_svg():
        if os.path.exists("dist/vite.svg"):
            return FileResponse("dist/vite.svg")
        raise HTTPException(status_code=404, detail="File not found")


    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Обслуживание SPA для всех маршрутов"""
        if os.path.exists("dist/index.html"):
            return FileResponse("dist/index.html")
        raise HTTPException(status_code=404, detail="SPA not built")
else:
    @app.get("/")
    async def root():
        return {"message": "Frontend not built. Run 'npm run build' first."}

if __name__ == "__main__":
    # Настройки сервера из переменных окружения
    host = get_setting('SERVER_HOST', '0.0.0.0')
    port = get_setting('SERVER_PORT', 8000)

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=False,
        log_level="info"
    )