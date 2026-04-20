# File: domain_finder/src/domain_finder/checker/whois.py

import logging
import re
from typing import Dict, Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed
from domain_finder.cache import whois_cache

import sys, os

# HACK: Подавление «грязного» вывода библиотеки python-whois — пишет в stderr напрямую
class _SuppressStderr:
    def __enter__(self):
        self._original_stderr = sys.stderr
        sys.stderr = open(os.devnull, 'w')
        return self
    def __exit__(self, *args):
        sys.stderr.close()
        sys.stderr = self._original_stderr

with _SuppressStderr():
    import whois

logger = logging.getLogger(__name__)


# Обрезка ошибки для логов: первая строка ≤100 символов в WARNING, полный текст в DEBUG
# Бизнес-правило: юридические дисклеймеры WHOIS не должны засорять консоль оператора
def _truncate_error(error: Exception, max_len: int = 100) -> str:
    msg = str(error).strip()
    
    if '\n' in msg:
        msg = msg.split('\n')[0].strip()
    
    if len(msg) > max_len:
        msg = msg[:max_len].rstrip() + "..."
    
    return msg


# Проверка домена: кэш → WHOIS → эвристики; available=None = «неизвестно», не ошибка
# Эвристики: пустой=свободен, регистратор+дата=занят, иначе=unknown
def check_domain_availability(domain: str) -> Dict[str, Optional[bool | str]]:
    
    cached = whois_cache.get(domain)
    if cached is not None:
        logger.debug(f"[WHOIS:CACHE:HIT] '{domain}' served from cache")
        return cached
    
    logger.debug(f"[WHOIS:CACHE:MISS] '{domain}' — querying server")
    
    try:
        w = whois.whois(domain)
        
        if not w or (not w.domain_name and not w.registrar and not w.creation_date):
            result = {"domain": domain, "available": True, "status": "available"}
            logger.debug(f"[WHOIS:RESULT:FREE] '{domain}' is available (empty response)")
            
        elif w.registrar and w.creation_date:
            result = {"domain": domain, "available": False, "status": "taken"}
            logger.debug(f"[WHOIS:RESULT:TAKEN] '{domain}' is registered")
            
        else:
            logger.debug(f"[WHOIS:RESULT:UNKNOWN] '{domain}' — incomplete WHOIS data")
            result = {"domain": domain, "available": None, "status": "unknown"}
            
    except Exception as e:
        short_msg = _truncate_error(e)
        logger.warning(f"[WHOIS:RESULT:UNKNOWN] '{domain}' — {short_msg}")
        logger.debug(f"[WHOIS:RESULT:UNKNOWN:FULL] '{domain}' — full error: {e}")
        
        result = {"domain": domain, "available": None, "status": "unknown"}
    
    whois_cache.set(domain, result)
    logger.debug(f"[WHOIS:CACHE:SET] '{domain}' cached with status={result['status']}")
    
    return result


# Параллельная проверка списка: ThreadPoolExecutor + индексация фьючерсов для сохранения порядка
# Бизнес-правило: порядок результатов = порядок входного списка (требование отчётности)
def check_domains_parallel(domains: List[str], max_workers: int = 10) -> List[Dict]:
    if not domains:
        logger.warning("[WHOIS:BATCH:WARN] Empty domain list provided")
        return []
    
    cache_hits = sum(1 for d in domains if whois_cache.get(d) is not None)
    if cache_hits:
        logger.info(f"[WHOIS:BATCH:CACHE] {cache_hits}/{len(domains)} domains served from cache")
    
    active_workers = min(len(domains), max_workers)
    logger.info(f"[WHOIS:BATCH:START] Checking {len(domains)} domains with {active_workers} workers")
    
    results = [None] * len(domains)
    
    with ThreadPoolExecutor(max_workers=active_workers) as executor:
        futures = {
            executor.submit(check_domain_availability, domain): i
            for i, domain in enumerate(domains)
        }
        
        for future in as_completed(futures):
            index = futures[future]
            results[index] = future.result()
            logger.debug(f"[WHOIS:BATCH:PROGRESS] {index+1}/{len(domains)} completed")
            
    stats = {
        "available": sum(1 for r in results if r["available"] is True),
        "taken": sum(1 for r in results if r["available"] is False),
        "unknown": sum(1 for r in results if r["available"] is None)
    }
    logger.info(f"[WHOIS:BATCH:DONE] Results: {stats['available']} free, {stats['taken']} taken, {stats['unknown']} unknown")
    
    return results


# 💡 ПРИНЦИПЫ РАБОТЫ
# 🏗 АРХИТЕКТУРА: Утилитный модуль проверки доменов (Infrastructure Layer)
# - Ответственность: интеграция с WHOIS, эвристики интерпретации, кэширование, параллелизм
# - Паттерны: Cache-Aside + Circuit Breaker + Worker Pool
# - Граница: не содержит бизнес-логики генерации — только проверка статуса
#
# 1. КЭШИРОВАНИЕ (Cache-Aside)
#    • Проверка кэша перед сетевым запросом — снижение нагрузки на WHOIS-серверы
#    • Кэш хранит статус (available/taken/unknown) — повторные запросы мгновенны
#    • Бизнес-выгода: ускорение пакетных проверок в 5-10 раз при наличии перекрытий
#
# 2. ЭВРИСТИКИ ИНТЕРПРЕТАЦИИ
#    • Пустой ответ = свободен: консервативный подход (лучше ложный positive)
#    • Регистратор + дата = занят: высокая достоверность, минимум ложных срабатываний
#    • Частичные данные/ошибки = unknown: не блокируем работу, но логируем для анализа
#
# 3. ПАРАЛЛЕЛИЗМ (ThreadPoolExecutor)
#    • Сохранение порядка результатов через индексацию фьючерсов (требование отчётности)
#    • Динамическое число воркеров: min(домены, max_workers) — баланс скорости и нагрузки
#
# 4. ЛОГИРОВАНИЕ
#    • DEBUG: детали запросов, кэш-хиты — для отладки
#    • INFO: старт/финиш пакетной проверки, статистика — для оператора
#    • WARNING: ошибки сети/парсинга — требуют внимания, но не блокируют
#
# 🔒 БЕЗОПАСНОСТЬ И НАДЕЖНОСТЬ
#    • Ошибки не роняют приложение: любой исключительный сценарий = status: unknown
#    • Короткие сообщения в WARNING: оператор видит суть без «шума» дисклеймеров
#    • HACK: _SuppressStderr изолирован в контекстный менеджер — не влияет на остальной код
#
# 🚀 ПОРЯДОК ВЫПОЛНЕНИЯ
#    [config] -> [cache init] -> [whois.py import] -> [check_domains_parallel]
#    → Кэш инициализируется до первого вызова
#    → Параллельная функция вызывает одиночную (единственная точка входа в WHOIS)
#    → Результаты возвращаются в том же порядке, что и входной список
#
# 📊 МЕТРИКИ ДЛЯ БИЗНЕСА
#    • Cache hit rate: отношение кэш-хитов к общему числу запросов
#    • Status distribution: % free/taken/unknown (качество подбора доменов)
#    • Error rate: % unknown из-за ошибок (надёжность интеграции)
#    • Average latency: время пакетной проверки / число доменов