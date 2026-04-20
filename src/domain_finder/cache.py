# File: domain_finder/src/domain_finder/cache.py

import time
import logging
from typing import Dict, Optional, Any
from threading import Lock
from domain_finder.config import TTL

logger = logging.getLogger(__name__)


# Потокобезопасный кэш с TTL: lazy expiration + плановая очистка для экономии памяти
# Бизнес-правило: кэш снижает нагрузку на WHOIS-серверы, ускоряет повторные запросы
class SimpleCache:

    # TTL из конфига — централизованное управление временем жизни записей
    def __init__(self, ttl: int = TTL):
        self._cache: Dict[str, tuple[Any, float]] = {}
        self._lock = Lock()
        self._ttl = ttl
        logger.debug(f"[CACHE:INIT] Created with TTL={ttl}s")

    # Lazy expiration: запись удаляется при чтении, если age > TTL — экономия памяти без фоновых задач
    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key not in self._cache:
                logger.debug(f"[CACHE:GET:MISS] '{key}' not in cache")
                return None
            
            value, timestamp = self._cache[key]
            age = time.time() - timestamp
            
            if age > self._ttl:
                del self._cache[key]
                logger.debug(f"[CACHE:GET:EXPIRED] '{key}' expired (age={age:.0f}s > TTL={self._ttl}s)")
                return None
            
            logger.debug(f"[CACHE:GET:HIT] '{key}' served (age={age:.0f}s)")
            return value

    # Атомарная запись под Lock — защита от race condition в многопоточной среде
    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._cache[key] = (value, time.time())
            logger.debug(f"[CACHE:SET] '{key}' stored")

    # Принудительный сброс кэша — для тестирования или при подозрении на устаревшие данные
    def clear(self) -> None:
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            logger.info(f"[CACHE:CLEAR] Removed {count} entries")

    # Плановая очистка «забытых» ключей — предотвращение раздувания памяти
    def cleanup_expired(self) -> int:
        with self._lock:
            now = time.time()
            expired = [k for k, (_, ts) in self._cache.items() if now - ts > self._ttl]
            for k in expired:
                del self._cache[k]
            if expired:
                logger.debug(f"[CACHE:CLEANUP] Removed {len(expired)} expired entries")
            return len(expired)
    
    # Статистика для мониторинга: оператор видит hit rate и потребность в очистке
    def stats(self) -> Dict[str, int]:
        with self._lock:
            now = time.time()
            total = len(self._cache)
            expired = sum(1 for _, ts in self._cache.values() if now - ts > self._ttl)
            return {"total": total, "expired": expired, "active": total - expired}


# Глобальный экземпляр: Singleton-паттерн через модульный уровень — один кэш на приложение
# Бизнес-правило: WHOIS-данные меняются редко — часовое кэширование безопасно
whois_cache = SimpleCache(ttl=3600)
logger.info(f"[CACHE:GLOBAL] WHOIS cache initialized with TTL=3600s")


# 💡 ПРИНЦИПЫ РАБОТЫ
# 🏗 АРХИТЕКТУРА: Потокобезопасный кэш с TTL (Infrastructure Layer — Caching)
# - Ответственность: кэширование внешних запросов, управление временем жизни записей
# - Паттерны: Lazy Expiration + Thread Lock + Global Singleton
# - Граница: не содержит бизнес-логики доменов — только универсальный механизм кэширования
#
# 1. ПОТОКОБЕЗОПАСНОСТЬ
#    • threading.Lock() на всех операциях — защита от race condition
#    • Бизнес-смысл: check_domains_parallel() запускает 10+ потоков — кэш должен быть thread-safe
#
# 2. УПРАВЛЕНИЕ ВРЕМЕНЕМ ЖИЗНИ
#    • get(): проверка возраста при чтении — запись удаляется, если age > TTL
#    • cleanup_expired(): плановая очистка «забытых» ключей — предотвращение утечек памяти
#    • whois_cache.ttl=3600: WHOIS-данные меняются редко — часовое кэширование безопасно
#
# 3. НАБЛЮДАЕМОСТЬ
#    • Операции кэша (get/set): DEBUG — не засоряют консоль, но доступны в файле
#    • События жизненного цикла (init/clear): INFO — оператор видит изменения состояния
#    • Метод stats(): возврат структуры для внешнего мониторинга без парсинга логов
#
# 🔒 БЕЗОПАСНОСТЬ И НАДЕЖНОСТЬ
#    • Lock гарантирует атомарность операций — нет частичных обновлений
#    • del self._cache[key] при expired: немедленная очистка, предотвращение утечек
#    • Возврат None при отсутствии/истечении: вызывающий код понимает, что нужен реальный запрос
#
# 🚀 ПОРЯДОК ВЫПОЛНЕНИЯ
#    [SimpleCache(ttl=3600)] -> [whois_cache = SimpleCache()]
#    -> [check_domain_availability(domain)] -> [whois_cache.get(domain)]
#    -> [Hit: возврат / Miss: None → реальный WHOIS-запрос]
#    -> [После ответа: whois_cache.set(domain, result)]
#    -> [Периодически: whois_cache.cleanup_expired() → освобождение памяти]
#
# 📊 МЕТРИКИ ДЛЯ БИЗНЕСА
#    • Cache hit rate: отношение кэш-хитов к общему числу запросов
#    • Average entry age: средний возраст записей при чтении (актуальность данных)
#    • Cleanup frequency: как часто вызывается cleanup_expired и сколько удаляет
#    • Active vs expired ratio: % полезных записей в кэше (качество подбора TTL)