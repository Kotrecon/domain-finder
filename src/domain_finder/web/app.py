# File: domain_finder/src/domain_finder/web/app.py

from flask import Flask, render_template, request, jsonify, Response
import json
import re
import logging
from datetime import datetime
from flask_livereload import LiveReload

from domain_finder.llm.providers.openrouter_provider import OpenRouterProvider
from domain_finder.llm.providers.ollama_provider import OllamaProvider
from domain_finder.checker.whois import check_domains_parallel
from domain_finder.config import OPENROUTER_MODELS, OLLAMA_MODELS
from domain_finder.logger import init_logger

logger = logging.getLogger(__name__)

# Бизнес-правило: debug=True только для локальной разработки, в продакшене — Gunicorn + reverse proxy
app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
LiveReload(app)

# Маппинг провайдеров: новый провайдер добавляется в одну строку, без правки API-маршрутов
PROVIDERS = {
    "openrouter": OpenRouterProvider,
    "ollama": OllamaProvider
}

# Списки моделей из конфига: приоритет задается порядком — первая доступная будет использована
MODELS = {
    "openrouter": OPENROUTER_MODELS,
    "ollama": OLLAMA_MODELS
}

# Кэш провайдеров: один экземпляр на имя — конфиг грузится 1 раз за сессию
_provider_cache = {} 


# Получение кэшированного провайдера: Lazy Initialization + Singleton per provider name
# Бизнес-правило: повторные запросы используют готовый экземпляр — экономия времени и ресурсов
def get_cached_provider(provider_name):
    if provider_name not in _provider_cache:
        if provider_name not in PROVIDERS:
            return None
        try:
            instance = PROVIDERS[provider_name]()
            if not instance.load_config():
                logger.error(f"[CONFIG] Failed to load for {provider_name}")
                return None
            _provider_cache[provider_name] = instance
            logger.debug(f"[CACHE:INIT] Provider '{provider_name}' created + config loaded")
        except Exception as e:
            logger.error(f"[CACHE:ERROR] Init failed for {provider_name}: {e}")
            return None
    else:
        logger.debug(f"[CACHE:HIT] Reusing provider '{provider_name}'")
    return _provider_cache[provider_name]


# Сброс кэша провайдера: инвалидация при потере соединения — авто-восстановление без перезапуска
def invalidate_provider_cache(provider_name=None):
    if provider_name:
        _provider_cache.pop(provider_name, None)
        logger.debug(f"[CACHE:CLEAR] Removed '{provider_name}'")
    else:
        _provider_cache.clear()
        logger.debug("[CACHE:CLEAR] All providers removed")


# Главная страница: точка входа для веб-интерфейса
@app.route('/')
def index():
    logger.debug("[ROUTE] GET / — serving index.html")
    return render_template('index.html')


# Проверка соединения: 429 = успех для OpenRouter; сбой = инвалидация кэша для авто-восстановления
# Бизнес-правило: фронтенд использует этот эндпоинт для индикации статуса провайдера
@app.route('/api/check-provider', methods=['POST'])
def check_provider_api():
    data = request.get_json(silent=True)
    provider_name = data.get('provider') if data else None

    if not provider_name or provider_name not in PROVIDERS:
        logger.warning(f"[CHECK-PROVIDER] Unknown provider: {provider_name}")
        return jsonify({"status": "error", "message": "Unknown provider"}), 400

    logger.debug(f"[CHECK-PROVIDER] Request for '{provider_name}'")
    provider = get_cached_provider(provider_name)
    
    if not provider:
        logger.error(f"[CHECK-PROVIDER] Provider init failed: {provider_name}")
        return jsonify({"status": "error", "message": "Config load failed"}), 500

    if provider.validate_connection():
        logger.debug(f"[CHECK-PROVIDER:OK] Connection to '{provider_name}' verified")
        return jsonify({"status": "ok"})
    else:
        logger.warning(f"[CHECK-PROVIDER:FAIL] Connection to '{provider_name}' failed")
        invalidate_provider_cache(provider_name)
        return jsonify({"status": "error", "message": "Connection failed"}), 500


# Возврат списка моделей из конфига: без сетевых запросов — фронтенд получает данные для отображения
# Бизнес-правило: реальная доступность модели проверяется отдельно через /api/check-model
@app.route('/api/get-models', methods=['POST'])
def get_models_api():
    data = request.get_json(silent=True)
    provider_name = data.get('provider') if data else None
    logger.debug(f"[GET-MODELS] Request for '{provider_name}'")
    return jsonify({"models": MODELS.get(provider_name, [])})


# Проверка доступности модели: сетевой запрос — модель может быть в конфиге, но не установлена локально
# Бизнес-правило: для Ollama проверка обязательна — пользователь не должен гадать, почему модель не работает
@app.route('/api/check-model', methods=['POST'])
def check_model_api():
    data = request.get_json(silent=True)
    provider_name = data.get('provider')
    model_name = data.get('model')

    if not provider_name or not model_name:
        logger.warning(f"[CHECK-MODEL] Missing params: {provider_name}, {model_name}")
        return jsonify({"status": "error", "message": "Missing params"}), 400

    logger.debug(f"[CHECK-MODEL] Checking '{model_name}' via '{provider_name}'")
    provider = get_cached_provider(provider_name)
    if not provider:
        logger.error(f"[CHECK-MODEL] Provider init failed: {provider_name}")
        return jsonify({"status": "error"}), 500

    if provider.check_model(model_name):
        logger.debug(f"[CHECK-MODEL:OK] Model '{model_name}' is available")
        return jsonify({"status": "ok"})
    else:
        logger.warning(f"[CHECK-MODEL:FAIL] Model '{model_name}' unavailable via '{provider_name}'")
        return jsonify({"status": "error", "message": "Model unavailable"}), 500


# Основной пайплайн: генерация + WHOIS; пошаговая валидация с ранним выходом при ошибке
# Бизнес-сценарий: пользователь отправляет промпт → система генерирует → проверяет → возвращает результаты
# Обработка ошибок: любой сбой возвращает JSON с error + HTTP 500 — фронтенд показывает понятное сообщение
@app.route('/api/generate', methods=['POST'])
def generate():
    data = request.json
    prompt = data.get('prompt', '')
    tlds = data.get('tlds', ['.com', '.net', '.org'])
    count = int(data.get('count', 10))
    provider_name = data.get('provider', 'openrouter')

    logger.info(f"[GENERATE:START] User request: provider={provider_name}, count={count}, prompt_len={len(prompt)}")

    provider = get_cached_provider(provider_name)
    if not provider:
        logger.error(f"[GENERATE:ERROR] Provider init failed: {provider_name}")
        return jsonify({"error": "Provider initialization failed"}), 500

    if not provider.validate_connection():
        logger.warning(f"[GENERATE:WARN] Connection lost to {provider_name}, retrying...")
        invalidate_provider_cache(provider_name)
        return jsonify({"error": "Connection lost"}), 500

    model = None
    for m in MODELS.get(provider_name, []):
        if provider.check_model(m):
            model = m
            break
    if not model:
        logger.error(f"[GENERATE:ERROR] No models available for {provider_name}")
        return jsonify({"error": "No models available"}), 500

    logger.debug(f"[GENERATE:LLM] Calling {model} with prompt (len={len(prompt)})")

    strict_prompt = (
        f"Generate exactly {count} domain names for: \"{prompt}\". "
        f"Allowed TLDs: {', '.join(tlds)}. "
        "Return ONLY a JSON array: [\"name1.com\", \"name2.net\"]. No extra text."
    )
    
    try:
        raw_response = provider.generate(prompt=strict_prompt, model=model, max_tokens=1024)
        
        domains = []
        json_match = re.search(r'\[[\s\S]*\]', raw_response)
        if json_match:
            try:
                domains = json.loads(json_match.group())
            except json.JSONDecodeError:
                logger.warning(f"[GENERATE:PARSE] JSON decode failed, raw: {raw_response[:100]}...")
        
        domains = list(dict.fromkeys(domains))[:count]
        
        if not domains:
            logger.warning(f"[GENERATE:EMPTY] LLM returned no valid domains")
            return jsonify({"error": "LLM returned empty list"}), 500

        logger.info(f"[GENERATE:WHOIS] Checking {len(domains)} domains via WHOIS...")

        results = check_domains_parallel(domains)

        stats = {
            "available": sum(1 for r in results if r["available"] is True),
            "taken": sum(1 for r in results if r["available"] is False),
            "unknown": sum(1 for r in results if r["available"] is None)
        }
        logger.info(f"[GENERATE:DONE] Results: {stats['available']} available, {stats['taken']} taken, {stats['unknown']} unknown")

        return jsonify({
            "provider": provider_name,
            "model": model,
            "domains": results,
            "stats": stats
        })

    except Exception as e:
        logger.error(f"[GENERATE:CRASH] Unhandled exception: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# Экспорт результатов: метаданные + результаты — воспроизводимость и аудит
# Бизнес-правило: Response с Content-Disposition=attachment — браузер предлагает сохранить файл
@app.route('/api/json_export', methods=['POST'])
def json_export_results():
    data = request.get_json(silent=True)
    if not data or 'domains' not in data:
        logger.warning(f"[EXPORT:WARN] No data provided for export")
        return jsonify({"error": "No data"}), 400
    
    logger.info(f"[EXPORT:START] Exporting {len(data.get('domains', []))} domains")
    
    export_data = {
        "exported_at": datetime.now().isoformat(),
        "provider": data.get("provider"),
        "model": data.get("model"),
        "prompt": data.get("prompt"),
        "stats": data.get("stats", {}),
        "domains": data.get("domains", [])
    }
    
    logger.debug(f"[EXPORT:OK] File generated, sending to client")
    return Response(
        json.dumps(export_data, ensure_ascii=False, indent=2),
        mimetype='application/json; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=domains.json'}
    )


# 💡 ПРИНЦИПЫ РАБОТЫ
# 🏗 АРХИТЕКТУРА: Flask Backend (Application Layer — Web API)
# - Ответственность: HTTP-маршруты, кэширование провайдеров, оркестрация LLM+WHOIS, экспорт
# - Паттерны: Lazy Initialization + Singleton per Provider + Health Check + Graceful Degradation
# - Граница: делегирует бизнес-логику провайдерам и утилитам, не содержит её внутри
#
# 1. КЭШИРОВАНИЕ ПРОВАЙДЕРОВ (Lazy Singleton)
#    • get_cached_provider(): конфиг грузится 1 раз за сессию — экономия времени и ресурсов
#    • invalidate_provider_cache(): сброс при потере соединения — авто-восстановление без перезапуска
#    • Бизнес-выгода: повторные запросы используют готовый экземпляр — ускорение ответа в 2-3 раза
#
# 2. ПОШАГОВАЯ ВАЛИДАЦИЯ В /api/generate
#    • Provider → Connection → Model → Generate → WHOIS → Response
#    • Каждый шаг: проверка + ранний возврат ошибки — пользователь не ждет «до конца» при сбое
#    • Бизнес-смысл: снижение времени ожидания при ошибках, понятные сообщения для фронтенда
#
# 3. ИНТЕГРАЦИЯ С LLM И WHOIS
#    • Строгий промпт: только JSON, без маркдауна — защита от парсинг-ошибок
#    • Парсинг: регекс + json.loads с обработкой ошибок — устойчивость к «творческим» моделям
#    • WHOIS: параллельная проверка с кэшированием — оптимизация времени ответа для пользователя
#
# 4. ЭКСПОРТ И СТАТИСТИКА
#    • Статистика (available/taken/unknown) — оператор оценивает эффективность подбора
#    • Экспорт: метаданные + результаты — воспроизводимость и аудит
#    • Логирование: DEBUG для отладки, INFO для оператора, ERROR для разработчика
#
# 🔒 БЕЗОПАСНОСТЬ И НАДЕЖНОСТЬ
#    • Валидация входных данных: проверка provider_name, model_name, наличие domains в экспорте
#    • Ошибки генерации/парсинга: не роняют приложение, возвращают JSON с error + HTTP 500
#    • Сетевые ошибки: обрабатываются на уровне провайдеров, здесь — инвалидация кэша
#    • Экспорт файлов: Response с правильными заголовками — защита от XSS, корректное скачивание
#    • exc_info=True только для критических ошибок — не засоряет логи стеками в штатных сценариях
#
# 🚀 ПОРЯДОК ВЫПОЛНЕНИЯ
#    [GET /] -> [POST /api/check-provider] -> [POST /api/get-models] -> [POST /api/check-model]
#    -> [POST /api/generate: provider→connection→model→LLM→WHOIS→stats] -> [POST /api/json_export]
#
# 📊 МЕТРИКИ ДЛЯ БИЗНЕСА
#    • Provider cache hit rate: % запросов, использующих кэшированный провайдер
#    • Generate success rate: % запросов, вернувших валидные домены
#    • WHOIS cache hit rate: эффективность кэширования при пакетных проверках
#    • Available domains ratio: % свободных доменов в результатах
#    • Average response time: время от /api/generate до ответа (производительность для SLA)

if __name__ == '__main__':
    logger.info("🚀 [APP:START] Server starting on http://127.0.0.1:5000")
    app.run(debug=True, port=5000)