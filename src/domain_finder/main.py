# File: domain_finder/src/domain_finder/main.py

import logging
import json
import re
from datetime import datetime

from domain_finder.llm.providers.openrouter_provider import OpenRouterProvider
from domain_finder.llm.providers.ollama_provider import OllamaProvider
from domain_finder.config import OPENROUTER_MODELS, OLLAMA_MODELS
from domain_finder.checker.whois import check_domains_parallel
from domain_finder.logger import init_logger

# Логгер для CLI-модуля: наследует настройки из init_logger() — файл + консоль с эмодзи
logger = logging.getLogger("domain_finder.cli")

# Инициализация логгера: единая точка настройки для всех модулей
init_logger()


# Функция: Интерактивный выбор провайдера (OpenRouter / Ollama)
# Бизнес-правило: неверный ввод не прерывает поток — пользователь пробует снова
def select_provider() -> OpenRouterProvider | OllamaProvider:
    providers = {
        "1": {"name": "OpenRouter", "class": OpenRouterProvider},
        "2": {"name": "Ollama", "class": OllamaProvider},
    }
    
    print("\n🔌 Доступные провайдеры:")
    for key, val in providers.items():
        print(f"   {key}. {val['name']}")
    
    while True:
        choice = input("\nВыберите провайдера (номер): ").strip()
        if choice in providers:
            logger.info(f"[CLI:PROVIDER] Selected: {providers[choice]['name']}")
            print(f"✅ Выбран: {providers[choice]['name']}")
            return providers[choice]["class"]()
        logger.warning(f"[CLI:PROVIDER] Invalid choice: '{choice}'")
        print("❌ Неверный выбор, попробуйте снова")


# Функция: Перебор моделей из конфига, возврат первой доступной
# Бизнес-правило: пользователь не выбирает модель вручную — система находит рабочую автоматически
def select_model(provider) -> str:
    print("\n🔍 Проверка моделей...")
    logger.debug(f"[CLI:MODEL] Starting model selection for provider '{provider.name}'")
    
    models_list = OPENROUTER_MODELS if provider.name == "openrouter" else OLLAMA_MODELS
        
    for model in models_list:
        if provider.check_model(model):
            logger.info(f"[CLI:MODEL] Selected: '{model}'")
            return model
            
    logger.error(f"[CLI:MODEL] No models available for '{provider.name}'")
    raise Exception("❌ Ни одна модель из списка не доступна")


# Функция: Точка входа — оркестрация консольного потока
# Бизнес-сценарий: ввод → генерация → WHOIS → экспорт; любой сбой не роняет приложение
def main():
    print("🚀 Domain Finder — консольный запуск")
    logger.info("[CLI:START] Application launched")

    provider = select_provider()
    
    logger.debug(f"[CLI:CONFIG] Loading config for '{provider.name}'")
    if not provider.load_config():
        logger.error(f"[CLI:CONFIG] Failed to load config for '{provider.name}'")
        print("❌ Не удалось загрузить конфигурацию провайдера")
        return
    
    logger.debug(f"[CLI:CONNECT] Checking connection to '{provider.name}'")
    if not provider.validate_connection():
        logger.error(f"[CLI:CONNECT] Connection failed for '{provider.name}'")
        print("❌ Нет соединения с провайдером")
        return
    
    try:
        model = select_model(provider)
        print(f"✅ Модель выбрана: {model}")
    except Exception as e:
        print(e)
        return
    
    print("\n📝 Введите описание проекта:")
    prompt = input("> ").strip()
    if not prompt:
        logger.warning("[CLI:INPUT] Empty prompt provided")
        print("❌ Описание не может быть пустым")
        return
    
    print("\n🌐 Доменные зоны (через запятую, Enter для .com .net .org):")
    tlds_input = input("> ").strip()
    tlds = [t.strip() for t in tlds_input.split(",") if t.strip()] if tlds_input else [".com", ".net", ".org"]
    tlds = [t if t.startswith(".") else f".{t}" for t in tlds]
    
    print("\n🔢 Сколько вариантов (1-20)?")
    try:
        count = int(input("> "))
        count = max(1, min(20, count))
    except ValueError:
        logger.warning("[CLI:INPUT] Invalid count, using default 10")
        count = 10
    
    logger.info(f"[CLI:GENERATE] Request: prompt_len={len(prompt)}, tlds={tlds}, count={count}, model={model}")
    print(f"\n⏳ Генерация {count} доменов через {model}...")
    
    try:
        strict_prompt = (
            f"Task: Generate exactly {count} domain names based on: \"{prompt}\"\n\n"
            f"Constraints:\n"
            f"- TLDs allowed: {', '.join(tlds)}\n"
            f"- Format: JSON array ONLY, no explanations, no markdown, no backticks\n"
            f"- Example output: [\"name1.com\", \"name2.net\"]\n\n"
            f"Output ONLY the JSON array. Start with [ and end with ]."
        )
        
        raw_response = provider.generate(prompt=strict_prompt, model=model, max_tokens=1024)
        
        domains = []
        json_match = re.search(r'\[[\s\S]*\]', raw_response)
        if json_match:
            try:
                domains = json.loads(json_match.group())
                logger.debug(f"[CLI:PARSE] Extracted {len(domains)} domains from LLM response")
            except json.JSONDecodeError:
                logger.warning(f"[CLI:PARSE] JSON decode failed, raw: {raw_response[:100]}...")
        
        domains = list(dict.fromkeys(domains))[:count]

        if domains:
            print(f"\n📦 Сгенерировано {len(domains)} доменов:")
            for d in domains:
                print(f"   • {d}")
            
            print(f"\n🔍 Проверяем доступность {len(domains)} доменов через WHOIS...")
            results = check_domains_parallel(domains)
            
            available = [r for r in results if r["available"] is True]
            taken = [r for r in results if r["available"] is False]
            unknown = [r for r in results if r["available"] is None]
            
            print(f"\n📊 Результаты проверки:")
            print(f"   ✅ Свободных: {len(available)}")
            print(f"   ❌ Занятых: {len(taken)}")
            if unknown:
                print(f"   ❓ Неизвестно: {len(unknown)}")
            
            logger.info(f"[CLI:RESULTS] {len(available)} available, {len(taken)} taken, {len(unknown)} unknown")
            
            if available:
                print(f"\n🎉 Можно зарегистрировать:")
                for r in available:
                    print(f"   → {r['domain']}")
            else:
                logger.warning("[CLI:RESULTS] No available domains found")
                print(f"\n😔 В этой пачке свободных доменов не найдено")
            
            print("\n💾 Сохранить результаты проверки в JSON? (y/n)")
            save_choice = input("> ").strip().lower()
            if save_choice in ("y", "yes", "д"):
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                default_file = f"domain_results_{timestamp}.json"
                print(f"📄 Имя файла (Enter для {default_file}):")
                user_file = input("> ").strip()
                export_file = user_file if user_file else default_file

                try:
                    with open(export_file, "w", encoding="utf-8") as f:
                        json.dump(results, f, ensure_ascii=False, indent=2)
                    logger.info(f"[CLI:EXPORT] Saved to '{export_file}'")
                    print(f"✅ Сохранено: {export_file}")
                except Exception as e:
                    logger.error(f"[CLI:EXPORT] Failed to save: {e}")
                    print(f"❌ Ошибка записи файла: {e}")
        else:
            logger.warning("[CLI:GENERATE] No domains generated")
            print(f"❌ Не удалось получить домены")
            
    except Exception as e:
        logger.error(f"[CLI:CRASH] Unhandled exception: {e}", exc_info=True)
        print(f"❌ Ошибка генерации: {e}")


# 💡 ПРИНЦИПЫ РАБОТЫ
# 🏗 АРХИТЕКТУРА: CLI Orchestrator (Application Layer)
# - Ответственность: координация пользовательского потока, интеграция LLM + WHOIS, экспорт
# - Паттерны: Step-by-Step Validation + Fail-Fast Model Selection + Graceful Degradation
# - Граница: делегирует бизнес-логику провайдерам и утилитам, не содержит её внутри
#
# 1. ПОШАГОВАЯ ВАЛИДАЦИЯ
#    • Provider → Config → Connection → Model → Input → Generate → Whois → Export
#    • Каждый шаг: проверка + понятное сообщение + ранний выход при критической ошибке
#    • Бизнес-смысл: снижает фрустрацию пользователя, ускоряет диагностику проблем
#
# 2. ИНТЕГРАЦИЯ С LLM
#    • Строгий промпт: только JSON-массив, без маркдауна — машиночитаемый вывод
#    • Парсинг: регекс + json.loads с обработкой ошибок — защита от «творческих» моделей
#    • Дедупликация: dict.fromkeys() сохраняет порядок + удаляет дубли
#
# 3. WHOIS-ПРОВЕРКА
#    • check_domains_parallel: ThreadPoolExecutor + сохранение порядка для отчётности
#    • Кэш: снижает нагрузку на WHOIS-серверы, ускоряет повторные проверки
#    • Статусы: available/taken/unknown — полная картина для принятия решений
#
# 4. ЭКСПОРТ И СТАТИСТИКА
#    • Статистика по статусам: оператор оценивает эффективность подбора
#    • Экспорт в JSON: дефолтное имя по таймстампу + кастомизация
#    • Логирование: DEBUG для отладки, INFO для оператора, ERROR для разработчика
#
# 🔒 БЕЗОПАСНОСТЬ И НАДЕЖНОСТЬ
#    • Ввод пользователя: валидация диапазона (1-20), нормализация TLD
#    • Ошибки генерации/парсинга: не роняют приложение, понятное сообщение пользователю
#    • Сетевые ошибки: обрабатываются на уровне провайдеров, здесь — информирование
#    • Экспорт файлов: обработка IOError, чтобы сбой записи не ломал весь поток
#    • exc_info=True только для критических ошибок — не засоряет логи стеками
#
# 🚀 ПОРЯДОК ВЫПОЛНЕНИЯ
#    [init_logger] -> [select_provider] -> [load_config] -> [validate_connection]
#    -> [select_model] -> [input collection] -> [strict prompt] -> [LLM generate]
#    -> [JSON parse] -> [dedupe] -> [WHOIS parallel check] -> [stats] -> [optional export]
#
# 📊 МЕТРИКИ ДЛЯ БИЗНЕСА
#    • Provider selection rate: какой провайдер чаще выбирают
#    • Model availability: % успешного выбора модели
#    • Generation success rate: % запросов с валидными доменами
#    • WHOIS cache hit rate: эффективность кэширования
#    • Available domains ratio: % свободных доменов в результатах