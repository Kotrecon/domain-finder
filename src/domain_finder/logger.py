# File: domain_finder/src/domain_finder/logger.py

import logging
from pathlib import Path
from domain_finder.config import LOG_LEVEL  # Импорт уровня логирования из централизованного конфига

# Кастомный форматтер: добавляет эмодзи к уровням логов для визуальной навигации в консоли
# Бизнес-правило: эмодзи только для console-хендлера, файл остаётся машиночитаемым без декораций
class EmojiFormatter(logging.Formatter):
    """Форматтер, который добавляет эмодзи в зависимости от уровня лога."""

    # Маппинг числовых констант logging → эмодзи для быстрой визуальной диагностики
    # Бизнес-смысл: оператор сканирует логи глазами — ❌ и ⚠️ заметнее, чем текст "ERROR"/"WARNING"
    EMOJIS = {
        logging.DEBUG:    "🔍",
        logging.INFO:     "ℹ️",
        logging.WARNING:  "⚠️",
        logging.ERROR:    "❌",
        logging.CRITICAL: "🚨",
    }

    # Переопределение format(): временная подмена levelname для вставки эмодзи
    # Бизнес-правило: восстановление original_levelname после форматирования — защита от побочных эффектов в других хендлерах
    def format(self, record: logging.LogRecord) -> str:
        original_levelname = record.levelname
        emoji = self.EMOJIS.get(record.levelno, "📝")
        record.levelname = f"{emoji} {original_levelname}"
        result = super().format(record)
        record.levelname = original_levelname  # Restore: важно для файловых хендлеров и парсинга
        return result

# Инициализация логгера: создание директории, настройка хендлеров, применение форматтеров
# Бизнес-правило: вызывается один раз в main.py — повторный вызов возвращает настроенный экземпляр без дублирования хендлеров
def init_logger():
    """Инициализирует логгер. Вызывать один раз в начале main.py."""
    # Создание директории для логов: exist_ok=True — безопасный запуск при повторной инициализации
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    # Получение именованного логгера: "domain_finder" — единая точка для всего приложения
    logger = logging.getLogger("domain_finder")
    if logger.handlers:
        return logger  # Уже настроен: защита от дублирования хендлеров при горячей перезагрузке

    # Базовый уровень логгера: DEBUG — фильтр применяется на уровне хендлеров, не здесь
    # Бизнес-смысл: в файл пишем всё, в консоль — только то, что задано в LOG_LEVEL из конфига
    logger.setLevel(logging.DEBUG)

    # Файловый хендлер: запись всех логов (DEBUG+) в UTF-8 для последующего анализа/аудита
    # Бизнес-правило: формат с %(module)s:%(funcName)s — быстрая навигация по коду при отладке инцидентов
    fh = logging.FileHandler(log_dir / "domain_finder.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(module)s:%(funcName)s | %(message)s"
    ))

    # Консольный хендлер: фильтрация по LOG_LEVEL из config.py — гибкое управление шумом без пересборки
    # Бизнес-смысл: в dev — DEBUG для отладки, в prod — WARNING для мониторинга, меняем только в .env
    ch = logging.StreamHandler()
    ch.setLevel(LOG_LEVEL)
    ch.setFormatter(EmojiFormatter("%(levelname)s: %(message)s"))  # Эмодзи только для консоли — человек читает

    # Регистрация хендлеров: порядок не важен, но файл первым — привычка для предсказуемости
    logger.addHandler(fh)
    logger.addHandler(ch)

    return logger

# 💡 ПРИНЦИПЫ РАБОТЫ
# 🏗 АРХИТЕКТУРА: Централизованное логирование с разделением потоков (Dual-Stream Logging)
# - Ответственность: визуальная навигация в консоли + машиночитаемые логи в файле
# - Паттерны: Formatter Decoration + Handler Filtering + Singleton Logger Initialization
#
# 1. РАЗДЕЛЕНИЕ ПОТокОВ (Console vs File)
#    • Файл: полный лог (DEBUG+), строгий формат с временем/модулем/функцией — для аудита и пост-анализа
#    • Консоль: фильтр по LOG_LEVEL из конфига, эмодзи для уровней — для оперативной диагностики глазами
#    • Бизнес-смысл: оператор видит только важное в консоли, но при инциденте есть полный лог в файле
#
# 2. ВИЗУАЛЬНАЯ НАВИГАЦИЯ (EmojiFormatter)
#    • Эмодзи добавляются только на этапе форматирования для консоли — файл остаётся чистым для парсинга
#    • Восстановление record.levelname после format() — защита от побочных эффектов в других хендлерах
#    • Бизнес-правило: человек сканирует логи глазами — ❌ заметнее, чем "ERROR", ускоряет реакцию
#
# 3. ГИБКОСТЬ ОКРУЖЕНИЯ
#    • LOG_LEVEL из config.py: меняем в .env — не правим код, не пересобираем приложение
#    • Path("logs").mkdir(exist_ok=True): безопасный запуск в любой среде, без предварительной подготовки
#    • Проверка logger.handlers: защита от дублирования при горячей перезагрузке или повторном импорте
#
# 🔒 БЕЗОПАСНОСТЬ И НАДЕЖНОСТЬ
#    • encoding="utf-8" в FileHandler: корректная запись кириллицы/эмодзи/спецсимволов без крашей
#    • Формат с %(asctime)s: временная привязка событий — критично для расследования инцидентов
#    • %(module)s:%(funcName)s: точная локализация источника лога — ускоряет отладку в большом коде
#    • Возврат настроенного logger при повторном вызове: идемпотентность инициализации
#
# 🚀 ПОРЯДОК ВЫПОЛНЕНИЯ
#    [main.py] -> [init_logger()]
#    -> [mkdir("logs")] -> [getLogger("domain_finder")]
#    -> [Проверка handlers: если есть → возврат]
#    -> [setLevel(DEBUG): логгер принимает всё, фильтруют хендлеры]
#    -> [FileHandler: DEBUG+, строгий формат → domain_finder.log]
#    -> [StreamHandler: LOG_LEVEL из конфига, EmojiFormatter → консоль]
#    -> [Возврат logger] -> [Дальнейшие вызовы logging.getLogger("domain_finder") получают настроенный экземпляр]