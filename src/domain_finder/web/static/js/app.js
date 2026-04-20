// File: domain_finder/src/domain_finder/web/static/js/app.js

(function () {
  "use strict";

  const form = document.getElementById("finder-form");
  const submitBtn = document.getElementById("submit-btn");
  const statusEl = document.getElementById("status");
  const resultsEl = document.getElementById("results");
  const providerSelect = document.getElementById("provider");
  const modelSelect = document.getElementById("model");

  const provStatusIcon = document
    .getElementById("provider-status")
    .querySelector(".status-icon");
  const modelStatusIcon = document
    .getElementById("model-status")
    .querySelector(".status-icon");

  // Бизнес-правило: дефолтные TLD и таймаут 2 минуты — защита от зависаний при долгих LLM/WHOIS-запросах
  const DEFAULT_TLDS = [".com", ".net", ".org"];
  const FETCH_TIMEOUT = 120000;

  let lastResults = null;

  // Установка иконки статуса: loading/success/error, скрытие при idle
  function setStatusIcon(iconEl, state) {
    iconEl.className = "status-icon";
    iconEl.style.display = state === "idle" ? "none" : "block";
    if (state !== "idle") iconEl.classList.add(state);
  }

  // XSS-защита: экранирование любого динамического контента перед вставкой в DOM
  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  // Fetch с таймаутом: AbortController прерывает запрос при превышении лимита
  // Бизнес-правило: пользователь не должен ждать «вечно» — явное сообщение о таймауте
  async function fetchWithTimeout(url, options = {}, timeout = FETCH_TIMEOUT) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);

    try {
      const response = await fetch(url, {
        ...options,
        signal: controller.signal,
      });
      clearTimeout(timer);
      return response;
    } catch (error) {
      clearTimeout(timer);
      if (error.name === "AbortError") {
        throw new Error("Таймаут запроса — сервер отвечает слишком долго");
      }
      throw error;
    }
  }

  // Проверка подключения провайдера: при ошибке — сброс модели, чтобы не отправлять с невалидной конфигурацией
  async function checkProviderConnectivity() {
    const provider = providerSelect.value;
    setStatusIcon(provStatusIcon, "loading");
    providerSelect.disabled = true;

    try {
      const response = await fetchWithTimeout("/api/check-provider", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider }),
      });
      const data = await response.json();

      if (response.ok && data.status === "ok") {
        clearStatus();
        setStatusIcon(provStatusIcon, "success");
        await loadModelsForProvider(provider);
      } else {
        setStatusIcon(provStatusIcon, "error");
        showStatus("error", data.message || "Ошибка подключения к провайдеру");
        resetModelSelect();
      }
    } catch (error) {
      console.error("[Provider Check]", error);
      setStatusIcon(provStatusIcon, "error");
      showStatus("error", "Не удалось проверить провайдера (сеть/сервер)");
      resetModelSelect();
    } finally {
      providerSelect.disabled = false;
    }
  }

  // Сброс селекта моделей: визуальная индикация недоступности — пользователь не выбирает несуществующую модель
  function resetModelSelect() {
    modelSelect.innerHTML = '<option value="">Недоступно</option>';
    modelSelect.disabled = true;
    setStatusIcon(modelStatusIcon, "idle");
  }

  // Загрузка списка моделей: авто-выбор первой при успехе, явная ошибка если список пуст
  async function loadModelsForProvider(providerName) {
    modelSelect.innerHTML = '<option value="">Загрузка списка...</option>';
    modelSelect.disabled = true;
    setStatusIcon(modelStatusIcon, "idle");

    try {
      const res = await fetchWithTimeout("/api/get-models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: providerName }),
      });
      const data = await res.json();
      if (!res.ok || !data.models?.length) throw new Error("Empty model list");

      modelSelect.innerHTML = "";
      data.models.forEach((m) => {
        const opt = document.createElement("option");
        opt.value = m;
        opt.textContent = m;
        modelSelect.appendChild(opt);
      });
      modelSelect.disabled = false;

      if (modelSelect.options.length > 0) {
        modelSelect.selectedIndex = 0;
        await checkSelectedModel();
      }
    } catch (e) {
      console.error("[Load Models]", e);
      resetModelSelect();
    }
  }

  // Проверка модели: модель может быть в списке, но не установлена локально (Ollama) — проверка обязательна
  async function checkSelectedModel() {
    const provider = providerSelect.value;
    const model = modelSelect.value;
    if (!provider || !model) {
      setStatusIcon(modelStatusIcon, "idle");
      return;
    }

    setStatusIcon(modelStatusIcon, "loading");
    modelSelect.disabled = true;

    try {
      const res = await fetchWithTimeout("/api/check-model", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, model }),
      });
      const data = await res.json();
      setStatusIcon(
        modelStatusIcon,
        res.ok && data.status === "ok" ? "success" : "error",
      );
    } catch (e) {
      console.error("[Check Model]", e);
      setStatusIcon(modelStatusIcon, "error");
    } finally {
      modelSelect.disabled = false;
    }
  }

  function clearStatus() {
    statusEl.style.display = "none";
    statusEl.textContent = "";
    statusEl.className = "";
  }

  // Бизнес-правило: сообщение всегда видимо в UI — не логируем в консоль без дублирования для пользователя
  function showStatus(type, message) {
    statusEl.className = type;
    statusEl.textContent = message;
    statusEl.style.display = "block";
  }

  // Обработка формы: сбор данных, запрос к API, рендер; блокировка UI — защита от повторных отправок
  async function handleSubmit(event) {
    event.preventDefault();
    clearStatus();

    const exportBtn = document.getElementById("export-btn");
    if (exportBtn) {
      exportBtn.disabled = true;
      exportBtn.textContent = "Экспорт в JSON";
    }
    lastResults = null;

    const tldCheckboxes = document.querySelectorAll(
      '#tld-group input[type="checkbox"]:checked',
    );
    const tlds = Array.from(tldCheckboxes).map((cb) => cb.value);

    const payload = {
      prompt: document.getElementById("prompt").value.trim(),
      provider: providerSelect.value,
      model: modelSelect.value,
      tlds: tlds.length ? tlds : DEFAULT_TLDS,
      count: parseInt(document.getElementById("count").value, 10) || 10,
    };

    submitBtn.disabled = true;
    submitBtn.textContent = "Генерация и проверка...";
    showStatus("loading", "⏳ Отправка запроса к LLM и проверка WHOIS...");
    resultsEl.innerHTML = "";

    try {
      const response = await fetchWithTimeout("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Ошибка сервера");

      clearStatus();
      if (data.domains?.length) {
        renderResults(data);
      } else {
        resultsEl.innerHTML = '<p class="no-results">Домены не найдены.</p>';
      }
    } catch (error) {
      showStatus("error", `❌ ${error.message}`);
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "Найти домены";
    }
  }

  // Рендер результатов: статистика + список с анимацией; доступные домены выделяются визуально
  // XSS-защита: все динамические данные экранируются через escapeHtml()
  function renderResults(data) {
    const availableCount = data.stats.available || 0;
    const takenCount = data.stats.taken || 0;
    const unknownCount = data.stats.unknown || 0;
    const total = data.domains.length;

    const statsHtml = `
      <div class="results-header">
        <h3 class="results-title">Результаты</h3>
        <span class="results-count">${total} вариантов</span>
      </div>
      <div class="results-stats">
        <div class="stat-badge available">${availableCount} свободн${availableCount === 1 ? "ый" : "ых"}</div>
        <div class="stat-badge taken">${takenCount} занят${takenCount === 1 ? "ый" : "ых"}</div>
        <div class="stat-badge unknown">${unknownCount} неизвестно</div>
      </div>
    `;

    const domainsHtml = data.domains
      .map((d, index) => {
        let cls, statusText;
        if (d.available === true) {
          cls = "available";
          statusText = "СВОБОДЕН";
        } else if (d.available === false) {
          cls = "taken";
          statusText = "ЗАНЯТ";
        } else {
          cls = "unknown";
          statusText = "НЕИЗВЕСТНО";
        }
        const delay = index * 0.05;
        return `
        <div class="result-item ${cls}" style="animation-delay: ${delay}s">
          <span class="domain-name">${escapeHtml(d.domain)}</span>
          <span class="status-badge ${cls}">${statusText}</span>
        </div>
      `;
      })
      .join("");

    resultsEl.innerHTML = statsHtml + domainsHtml;

    // Сохранение промпта + результатов для экспорта — воспроизводимость: что сгенерировало эти домены
    lastResults = {
      ...data,
      prompt: document.getElementById("prompt").value.trim(),
    };

    const exportBtn = document.getElementById("export-btn");
    if (exportBtn) {
      exportBtn.disabled = false;
    }
  }

  // Инициализация: навешивание обработчиков, первичная проверка провайдера — приложение готово к работе сразу
  document.addEventListener("DOMContentLoaded", () => {
    form.addEventListener("submit", handleSubmit);
    providerSelect.addEventListener("change", checkProviderConnectivity);
    modelSelect.addEventListener("change", checkSelectedModel);

    const clearTldsBtn = document.getElementById("clear-tlds");
    if (clearTldsBtn) {
      clearTldsBtn.addEventListener("click", () => {
        document
          .querySelectorAll('#tld-group input[type="checkbox"]')
          .forEach((cb) => {
            cb.checked = false;
          });
      });
    }

    const countInput = document.getElementById("count");
    const countMinus = document.getElementById("count-minus");
    const countPlus = document.getElementById("count-plus");
    if (countMinus && countPlus) {
      countMinus.addEventListener("click", () => {
        let val = parseInt(countInput.value) || 10;
        if (val > 1) countInput.value = val - 1;
      });
      countPlus.addEventListener("click", () => {
        let val = parseInt(countInput.value) || 10;
        if (val < 20) countInput.value = val + 1;
      });
    }

    const exportBtn = document.getElementById("export-btn");
    if (exportBtn) {
      exportBtn.addEventListener("click", async () => {
        if (!lastResults) return;
        const originalText = exportBtn.textContent;
        exportBtn.disabled = true;
        exportBtn.textContent = "Генерация файла...";

        try {
          const response = await fetchWithTimeout("/api/json_export", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(lastResults),
          });
          if (!response.ok) throw new Error("Ошибка генерации");

          const blob = await response.blob();
          const url = window.URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          const timestamp = new Date()
            .toISOString()
            .slice(0, 19)
            .replace(/[:T]/g, "-");
          a.download = `domains-${timestamp}.json`;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          window.URL.revokeObjectURL(url);
        } catch (error) {
          console.error("[Export]", error);
          alert("❌ Не удалось скачать файл");
        } finally {
          exportBtn.disabled = !lastResults;
          exportBtn.textContent = originalText;
        }
      });
    }

    checkProviderConnectivity();
  });

  // 💡 ПРИНЦИПЫ РАБОТЫ
  // 🏗 АРХИТЕКТУРА: Frontend Client (Presentation Layer — UI + API Integration)
  // - Ответственность: обработка форм, API-запросы, рендер результатов, экспорт
  // - Паттерны: IIFE Module + AbortController Timeout + XSS-Safe Rendering + State Management
  // - Граница: не содержит бизнес-логики генерации — делегирует backend API
  //
  // 1. УПРАВЛЕНИЕ СОСТОЯНИЕМ (State-Driven UI)
  //    • lastResults: хранение данных для экспорта вне DOM — защита от потери при ре-рендере
  //    • Блокировка UI-элементов во время запросов — предотвращение повторных отправок
  //    • Бизнес-смысл: пользователь не может «сломать» поток двойным кликом или быстрым переключением
  //
  // 2. БЕЗОПАСНОСТЬ (XSS + Network Hardening)
  //    • escapeHtml(): экранирование любого пользовательского/LLM-контента перед вставкой в DOM
  //    • fetchWithTimeout(): AbortController + таймаут 120с — защита от зависаний и DoS
  //    • Бизнес-правило: доверять можно только своему коду — весь внешний ввод считается враждебным
  //
  // 3. ПОЛЬЗОВАТЕЛЬСКИЙ ОПЫТ (Progressive Feedback)
  //    • Статус-иконки (loading/success/error) — визуальная навигация без чтения текста
  //    • Поэтапная анимация результатов — снижение когнитивной нагрузки при большом списке
  //    • Явные сообщения об ошибках — оператор понимает, что делать, а не гадает
  //
  // 4. ЭКСПОРТ И ВОСПРОИЗВОДИМОСТЬ
  //    • Сохранение промпта + результатов в lastResults — аудит «что сгенерировало эти домены»
  //    • Blob + временный URL для скачивания — безопасная передача данных без серверного кэша
  //    • Таймстамп в имени файла — уникальность и хронология экспортов
  //
  // 🔒 БЕЗОПАСНОСТЬ И НАДЕЖНОСТЬ
  //    • XSS-защита: escapeHtml() для всего динамического контента
  //    • Таймауты: 120с для генерации, стандартные для остальных — баланс ожидания и отзывчивости
  //    • Обработка ошибок: каждое fetch в try/catch + понятное сообщение в UI
  //    • Сброс состояния: при ошибке провайдера — сброс модели, чтобы не отправлять с невалидной конфигурацией
  //    • IIFE + "use strict": изоляция области видимости, защита от глобальных конфликтов
  //
  // 🚀 ПОРЯДОК ВЫПОЛНЕНИЯ
  //    [DOMContentLoaded] -> [checkProviderConnectivity]
  //    -> [Provider OK] -> [loadModelsForProvider] -> [checkSelectedModel]
  //    -> [Form Submit] -> [handleSubmit: сбор данных → fetch /api/generate]
  //    -> [renderResults: статистика + список + активация экспорта]
  //    -> [Export Click] -> [fetch /api/json_export → Blob download]
  //
  // 📊 МЕТРИКИ ДЛЯ БИЗНЕСА
  //    • Provider connectivity success rate: % успешных проверок подключения
  //    • Model availability: % моделей, прошедших check_model
  //    • Generate success rate: % запросов /api/generate, вернувших домены
  //    • Export usage: как часто пользователи скачивают результаты
  //    • Average time to results: от клика «Найти» до рендера
})();
