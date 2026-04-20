# File: domain_finder/src/domain_finder/llm/providers/openrouter_provider.py

import requests
import logging
from typing import Optional
from domain_finder.config import OPENROUTER_API_KEY, OPENROUTER_URL, DEFAULT_HEADERS
from ..provider_base import LLMProvider

# Логгер для модуля: наследует настройки из init_logger() — файл + консоль с эмодзи
# Бизнес-смысл: оператор видит согласованные логи независимо от источника события
logger = logging.getLogger(__name__)


# Провайдер для облачного LLM-сервиса OpenRouter.ai: интеграция через REST API с авторизацией Bearer
# Бизнес-правило: ключ валидируется по префиксу sk-or- до любых сетевых вызовов — защита от ошибок конфигурации
class OpenRouterProvider(LLMProvider):

    # Инициализация: вызов базового конструктора, подготовка полей для config/connection
    # Бизнес-смысл: api_key/api_url заполняются в load_config() — разделение инициализации и конфигурации
    def __init__(self):
        super().__init__("openrouter")
        self.api_key: Optional[str] = None
        self.api_url: Optional[str] = None
        self.headers: dict = {}

    # Загрузка конфигурации: валидация API-ключа по формату OpenRouter (префикс sk-or-), настройка авторизации
    # Бизнес-правило: неверный ключ = ранний возврат False, чтобы не выполнять бесполезные сетевые запросы
    def load_config(self) -> bool:
        self.api_key = OPENROUTER_API_KEY
        self.api_url = OPENROUTER_URL
        
        # Валидация формата ключа: бизнес-правило — только ключи sk-or- считаются валидными
        if not self.api_key or not self.api_key.startswith("sk-or-"):
            logger.error(f"[{self.name}:CONFIG:ERROR] Key missing or invalid format")
            return False

        self.headers = {**DEFAULT_HEADERS, "Authorization": f"Bearer {self.api_key}"}
        self.is_key_valid = True
        
        # DEBUG: загрузка конфига — рутинная операция при старте, не засоряем консоль
        logger.debug(f"[{self.name}:CONFIG:OK] Key loaded and valid")
        return True

    # Проверка соединения: «пинг» эндпоинта, статус 429 (rate limit) считается успешным — сервис доступен
    # Бизнес-правило: 429 ≠ ошибка, это сигнал что API жив, просто лимит исчерпан — приложение продолжает работу
    def validate_connection(self) -> bool:
        if not self.is_key_valid or not self.api_url:
            logger.warning(f"[{self.name}:CONNECT:SKIP] Key not valid")
            return False
        try:
            response = requests.post(
                self.api_url, headers=self.headers,
                json={"model": "openrouter/auto", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5},
                timeout=15
            )
            # Бизнес-смысл: 200 = ОК, 429 = rate limit (сервис жив) — оба статуса = соединение установлено
            if response.status_code in (200, 429):
                self.is_connected = True
                logger.debug(f"[{self.name}:CONNECT:OK] Connection verified (status={response.status_code})")
                return True
            
            # Нестандартный статус: логируем предупреждение, но не крашим приложение — возможна временная проблема
            logger.warning(f"[{self.name}:CONNECT:WARN] Unexpected status: {response.status_code}")
            return False
        except Exception as e:
            # Сетевая ошибка: WARNING, т.к. приложение может продолжить работу (retry/fallback)
            logger.warning(f"[{self.name}:CONNECT:ERROR] Network error: {e}")
            return False

    # Проверка модели: тестовый запрос, не-200 — предупреждение (модель может быть временно недоступна)
    # Бизнес-правило: недоступность модели ≠ критическая ошибка, просто пробуем следующую из списка
    def check_model(self, model_name: str) -> bool:
        if not self.is_connected or not self.api_url: 
            logger.warning(f"[{self.name}:MODEL:SKIP] No connection")
            return False
        try:
            resp = requests.post(
                self.api_url, headers=self.headers,
                json={"model": model_name, "messages": [{"role": "user", "content": "OK"}], "max_tokens": 5},
                timeout=20
            )
            if resp.status_code == 200:
                # DEBUG: проверка модели — часть процесса подбора, не действие пользователя
                logger.debug(f"[{self.name}:MODEL:OK] '{model_name}' is available")
                return True
            
            # Модель недоступна: извлекаем сообщение об ошибке для понятного предупреждения оператору
            error_msg = resp.json().get('error', {}).get('message', 'Unknown error')
            logger.warning(f"[{self.name}:MODEL:WARN] '{model_name}' unavailable: {error_msg}")
            return False
        except Exception as e:
            # Ошибка проверки: логируем, но не крашим приложение — возврат False для перебора следующей модели
            logger.warning(f"[{self.name}:MODEL:ERROR] Check failed for '{model_name}': {e}")
            return False

    # Генерация ответа: основной пользовательский сценарий, ошибка — критичная (RuntimeError), т.к. блокирует результат
    # Бизнес-правило: любой сбой генерации = исключение, чтобы вызывающий код мог применить retry/fallback стратегию
    def generate(self, prompt: str, model: str, max_tokens: int = 1024) -> Optional[str]:
        if not self.is_ready() or not self.api_url:
            # ERROR: вызов неинициализированного провайдера — критическая ошибка, выбрасываем исключение
            logger.error(f"[{self.name}:GENERATE:ERROR] Provider not ready")
            raise RuntimeError(f"[{self.name}] Provider not ready")
        
        try:
            # DEBUG: детали запроса (модель, длина промпта) — для отладки, не для консоли
            logger.debug(f"[{self.name}:REQUEST] Calling '{model}' (tokens={max_tokens}, prompt_len={len(prompt)})")
            
            resp = requests.post(
                self.api_url, headers=self.headers,
                json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens},
                timeout=60
            )
            resp.raise_for_status()  # Выбросит исключение при 4xx/5xx — единая точка обработки ошибок
            
            # Безопасное извлечение контента: .strip() удаляет лишние пробелы/переносы от LLM
            # Бизнес-правило: пустой ответ = пустая строка, не None — упрощает обработку у вызывающего кода
            content = resp.json()["choices"][0]["message"]["content"].strip()
            
            # INFO: успешная генерация — действие пользователя, логируем в консоль + файл для аудита
            logger.info(f"[{self.name}:GENERATE:OK] Generated {len(content)} chars via '{model}'")
            return content
            
        except Exception as e:
            # CRITICAL ERROR: непредвиденная ошибка — логируем с traceback, выбрасываем RuntimeError
            # Бизнес-смысл: вызывающий код может поймать RuntimeError и переключиться на другой провайдер
            logger.error(f"[{self.name}:GENERATE:CRASH] Exception: {e}", exc_info=True)
            raise RuntimeError(f"[{self.name}] Generation failed: {e}")



# 💡 ПРИНЦИПЫ РАБОТЫ СКРИПТА
#
# 🏗 АРХИТЕКТУРА: Провайдер-паттерн, слой интеграции с внешним LLM-сервисом (OpenRouter API)
#   - Ответственность: аутентификация, health-check, генерация текста через REST API
#   - Паттерны: Configuration-Driven + Health Check + Graceful Degradation
#   - Граница: не содержит бизнес-логики генерации доменов — только транспорт к LLM
#
# 1. КОНФИГУРАЦИЯ И АУТЕНТИФИКАЦИЯ
#    • load_config(): валидация ключа по префиксу sk-or- до сетевых вызовов — ранний отказ при ошибке
#    • Бизнес-смысл: экономия времени и ресурсов — не делаем запросы с заведомо неверным ключом
#    • Авторизация: Bearer-токен в заголовках — стандарт OAuth 2.0, совместимость с прокси/балансировщиками
#
# 2. ПРОВЕРКА СОЕДИНЕНИЯ (Health Check)
#    • validate_connection(): POST-запрос с минимальным payload (ping) и таймаутом 15с
#    • Бизнес-правило: статус 429 (rate limit) = сервис доступен, не ошибка — приложение продолжает работу
#    • Почему так: облачные API часто лимитируют запросы, но это не означает сбой инфраструктуры
#
# 3. ПРОВЕРКА МОДЕЛЕЙ (Fail-Fast Strategy)
#    • check_model(): тестовый запрос к конкретной модели, не-200 = предупреждение, не исключение
#    • Бизнес-смысл: модели могут быть временно недоступны — перебираем список из конфига до первой рабочей
#    • Логирование: извлечение error.message из ответа — оператор видит причину, а не просто «ошибка»
#
# 4. ГЕНЕРАЦИЯ ОТВЕТА (Основной пользовательский сценарий)
#    • Таймаут 60с: баланс между ожиданием ответа от облака и защитой от зависаний
#    • resp.raise_for_status(): единая точка обработки 4xx/5xx — упрощает тестирование и логирование
#    • Бизнес-правило: любой сбой генерации = RuntimeError — сигнал для retry/fallback у вызывающего кода
#
# 🔒 БЕЗОПАСНОСТЬ И НАДЕЖНОСТЬ
#    • is_ready() проверка перед генерацией: защита от вызова неинициализированного провайдера
#    • Ключ валидируется до сетевых вызовов: предотвращение утечек неверных кредов в логи/сети
#    • Логирование по уровням: DEBUG для рутины, INFO для действий пользователя, ERROR для сбоев
#    • Исключения: RuntimeError при ошибке генерации — явный контракт для вызывающего кода
#    • Таймауты: 15с для проверки, 20с для модели, 60с для генерации — защита от бесконечных ожиданий
#
# 🚀 ПОРЯДОК ВЫПОЛНЕНИЯ
#    [OpenRouterProvider()] -> [load_config(): валидация ключа → is_key_valid=True]
#    -> [validate_connection(): POST ping → is_connected=True при 200/429]
#    -> [check_model("model-name"): тестовый запрос → лог/предупреждение]
#    -> [generate(prompt): POST chat → парсинг ответа → возврат текста]
#    -> [При ошибке: лог + RuntimeError → вызывающий код решает: retry / fallback / abort]
#
# 📊 МЕТРИКИ ДЛЯ БИЗНЕСА (что логгируется для анализа)
#    • Config load success rate: % успешных загрузок конфигурации (качество .env/настроек)
#    • Connection success rate: % успешных проверок соединения (надёжность сети/провайдера)
#    • Model availability: % моделей из списка, доступных для генерации (актуальность конфига)
#    • Generation latency: время от запроса до ответа (производительность для SLA)
#    • Error distribution: соотношение 4xx/5xx/network errors (диагностика проблем провайдера)
# ==============================================================================