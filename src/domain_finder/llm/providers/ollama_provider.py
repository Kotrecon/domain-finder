# File: domain_finder/src/domain_finder/llm/providers/ollama_provider.py

import requests
import logging
from typing import Optional
from domain_finder.config import OLLAMA_BASE_URL
from ..provider_base import LLMProvider

# Логгер для этого модуля: наследует настройки из init_logger() — файл + консоль с эмодзи
logger = logging.getLogger(__name__)

# Провайдер для локального LLM-сервера Ollama: без ключа, с проверкой соединения и моделей
# Бизнес-правило: локальный сервер = быстрее, приватнее, но требует ручной установки моделей
class OllamaProvider(LLMProvider):
    """Провайдер для локального LLM-сервера Ollama."""

    # Инициализация: вызов базового конструктора с именем "ollama", подготовка полей для URL
    # Бизнес-смысл: base_url/chat_url/tags_url вычисляются в load_config() — не в конструкторе
    def __init__(self):
        super().__init__("ollama")
        self.base_url: Optional[str] = None
        self.chat_url: Optional[str] = None
        self.tags_url: Optional[str] = None

    # Загрузка конфигурации: формирование URL из OLLAMA_BASE_URL, установка is_key_valid=True
    # Бизнес-правило: локальный сервер не требует API-ключа — упрощение настройки, но требует проверки доступности
    def load_config(self) -> bool:
        self.base_url = OLLAMA_BASE_URL.rstrip("/")
        self.chat_url = f"{self.base_url}/api/chat"
        self.tags_url = f"{self.base_url}/api/tags"
        
        self.is_key_valid = True  # Локальный сервер, ключ не требуется — всегда валиден
        
        # Логируем только на DEBUG: загрузка конфига — рутинная операция при старте
        logger.debug(f"[{self.name}:CONFIG:OK] Config loaded: {self.base_url}")
        return True

    # Проверка соединения: GET-запрос к /api/tags с таймаутом 5 секунд
    # Бизнес-правило: 200 = сервер доступен, ConnectionError = Ollama не запущен — дружественное сообщение пользователю
    def validate_connection(self) -> bool:
        if not self.base_url:
            logger.warning(f"[{self.name}:CONNECT:SKIP] Base URL not set")
            return False
        try:
            resp = requests.get(self.tags_url, timeout=5)
            if resp.status_code == 200:
                self.is_connected = True
                # DEBUG: проверка соединения может вызываться часто — не засоряем консоль
                logger.debug(f"[{self.name}:CONNECT:OK] Server reachable at {self.base_url}")
                return True
            
            # Нестандартный статус: логируем предупреждение, но не крашим приложение
            logger.warning(f"[{self.name}:CONNECT:WARN] Server returned status {resp.status_code}")
            return False
        except requests.exceptions.ConnectionError:
            # Самая частая ошибка в локальной разработке: Ollama не запущен
            # Бизнес-смысл: явное сообщение "is Ollama running?" снижает порог входа для пользователя
            logger.warning(f"[{self.name}:CONNECT:ERROR] Connection refused (is Ollama running?)")
            return False
        except Exception as e:
            # Другие сетевые ошибки: таймаут, DNS, SSL — логируем, возвращаем False
            logger.warning(f"[{self.name}:CONNECT:ERROR] Network error: {e}")
            return False

    # Проверка наличия модели: запрос списка моделей, извлечение базовых имён (без тегов)
    # Бизнес-правило: пользователь указывает "llama3.2", не думая о "llama3.2:latest" — упрощение UX
    def check_model(self, model_name: str) -> bool:
        if not self.is_connected:
            logger.warning(f"[{self.name}:MODEL:SKIP] Not connected")
            return False
        try:
            resp = requests.get(self.tags_url, timeout=5)
            if resp.status_code == 200:
                # Извлекаем базовые имена: "llama3.2:latest" → "llama3.2"
                # Бизнес-смысл: сравнение по базовому имени — пользователь не должен знать о тегах
                available = [m.get("name", "").split(":")[0] for m in resp.json().get("models", [])]
                if model_name in available:
                    # DEBUG: проверка модели — часть процесса подбора, не действие пользователя
                    logger.debug(f"[{self.name}:MODEL:OK] '{model_name}' found locally")
                    return True
            
            # Модель не найдена: предупреждение с подсказкой, как её установить
            # Бизнес-правило: дружественное сообщение снижает фрустрацию при первой настройке
            logger.warning(f"[{self.name}:MODEL:WARN] '{model_name}' not found (try: ollama pull {model_name})")
            return False
        except Exception as e:
            # Ошибка при проверке: логируем, но не крашим приложение — возврат False
            # Бизнес-смысл: лучше пропустить проверку, чем остановить весь процесс генерации
            logger.warning(f"[{self.name}:MODEL:ERROR] Check failed: {e}")
            return False

    # Генерация ответа: полноценный POST-запрос к /api/chat с промптом и ограничением токенов
    # Бизнес-правило: таймаут 120 секунд — локальные модели могут отвечать медленно, но не бесконечно
    def generate(self, prompt: str, model: str, max_tokens: int = 1024) -> Optional[str]:
        if not self.is_ready():
            # ERROR: вызов неинициализированного провайдера — критическая ошибка, выбрасываем исключение
            logger.error(f"[{self.name}:GENERATE:ERROR] Provider not ready")
            raise RuntimeError(f"[{self.name}] Provider not ready")
        
        try:
            # DEBUG: детали запроса (модель, длина промпта) — для отладки, не для консоли
            logger.debug(f"[{self.name}:REQUEST] Calling '{model}' (tokens={max_tokens}, prompt_len={len(prompt)})")
            
            # Формирование payload в формате Ollama API: модель, сообщения, стриминг, опции
            # Бизнес-смысл: stream=False — синхронный ответ, проще для интеграции в веб-пайплайн
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"num_predict": max_tokens}  # Ограничение длины ответа
            }
            
            resp = requests.post(self.chat_url, json=payload, timeout=120)
            resp.raise_for_status()  # Выбросит исключение при 4xx/5xx — единая точка обработки
            
            # Безопасное извлечение контента: .get().get().strip() — защита от KeyError и лишних пробелов
            # Бизнес-правило: пустой ответ = пустая строка, не None — упрощает обработку у вызывающего кода
            data = resp.json()
            content = data.get("message", {}).get("content", "").strip()
            
            # INFO: успешная генерация — действие пользователя, логируем в консоль + файл
            # Бизнес-смысл: оператор видит прогресс: "сгенерировано 256 символов через llama3.2"
            logger.info(f"[{self.name}:GENERATE:OK] Generated {len(content)} chars via '{model}'")
            return content
            
        except requests.exceptions.Timeout:
            # Локальные модели могут быть медленными — таймаут = важная информация для диагностики
            # Бизнес-правило: явное сообщение "model too slow?" помогает пользователю выбрать другую модель
            logger.warning(f"[{self.name}:GENERATE:WARN] Timeout (model '{model}' too slow?)")
            raise RuntimeError(f"[{self.name}] Generation timeout")
        except Exception as e:
            # CRITICAL ERROR: непредвиденная ошибка — логируем с traceback, выбрасываем RuntimeError
            # Бизнес-смысл: вызывающий код может поймать RuntimeError и переключиться на другой провайдер
            logger.error(f"[{self.name}:GENERATE:CRASH] Exception: {e}", exc_info=True)
            raise RuntimeError(f"[{self.name}] Generation failed: {e}")


# 💡 ПРИНЦИПЫ РАБОТЫ
# 🏗 АРХИТЕКТУРА: Провайдер локального LLM (Ollama Integration Layer)
# - Ответственность: проверка соединения, наличие моделей, генерация через локальный Ollama
# - Паттерны: Configuration-Driven + Health Check + Graceful Degradation
#
# 1. КОНФИГУРАЦИЯ И ПОДКЛЮЧЕНИЕ
#    • load_config(): формирование URL из OLLAMA_BASE_URL — централизованное управление адресом
#    • validate_connection(): быстрый GET /api/tags с таймаутом 5с — проверка доступности сервера
#    • is_key_valid = True: локальный сервер не требует API-ключа — упрощение настройки
#
# 2. ПРОВЕРКА МОДЕЛЕЙ
#    • check_model(): запрос списка моделей, извлечение базовых имён (без тегов)
#    • Бизнес-смысл: пользователь указывает "llama3.2", не думая о "llama3.2:latest" — упрощение UX
#    • Подсказка при отсутствии: "try: ollama pull {model_name}" — снижение порога входа
#
# 3. ГЕНЕРАЦИЯ ОТВЕТА
#    • Формат payload: совместим с Ollama API (model, messages, stream, options)
#    • stream=False: синхронный ответ — проще для интеграции в веб-пайплайн
#    • Таймаут 120с: баланс между ожиданием медленной локальной модели и защитой от зависаний
#    • Безопасный парсинг: .get().get().strip() — защита от KeyError и лишних пробелов
#
# 🔒 БЕЗОПАСНОСТЬ И НАДЕЖНОСТЬ
#    • is_ready() проверка перед генерацией: защита от вызова неинициализированного провайдера
#    • Логирование по уровням: DEBUG для рутины, INFO для действий пользователя, WARNING для ожидаемых проблем
#    • Исключения: RuntimeError при ошибке генерации — сигнал для retry/fallback у вызывающего кода
#    • Таймауты: 5с для проверки, 120с для генерации — защита от бесконечных ожиданий
#
# 🚀 ПОРЯДОК ВЫПОЛНЕНИЯ
#    [OllamaProvider()] -> [load_config(): формирование URL]
#    -> [validate_connection(): GET /api/tags → is_connected=True]
#    -> [check_model("llama3.2"): проверка наличия → лог/подсказка]
#    -> [generate(prompt): POST /api/chat → парсинг ответа → возврат текста]
#    -> [При ошибке: лог + RuntimeError → вызывающий код решает: retry / fallback / abort]