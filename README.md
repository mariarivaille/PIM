# Pim

<img src="assets/avatar.png" alt="Pim" width="96">

Pim — личный LLM-ассистент: Telegram-бот, которому пишешь или наговариваешь дела — он их записывает и напоминает.
Дальше — выполнение части дел и отдельное приложение.

## Архитектура

```
telegram/  ─┐                      (позже: api/ для приложения — ещё один клиент того же ядра)
            ▼
agent/     Agent: LLM + цикл tool calling (create/list/update/complete/cancel task, set_timezone)
            │
llm/       LLMProvider: OpenRouter (OpenAI-совместимый) | GigaChat — общий формат Message/ToolCall
            │
core/      Repository + SQLAlchemy модели (User, Task), работа со временем и RRULE
            ▲
reminders/ ReminderScheduler: поллинг БД → Notifier (сейчас TelegramNotifier)
```

Принципы:
- **LLM только разбирает речь в структуру**, напоминания шлёт детерминированный планировщик по `due_at` из БД.
- **Память в два слоя:** явное «запомни» сохраняется сразу инструментом агента, а после 10 минут тишины
  `memory/reflection.py` отдельным вызовом LLM разбирает разговор и добавляет/уточняет факты. Вся память идёт в промпт.
- **Даты считает код, а не модель:** LLM передаёт `day=fri, time=19:00` или `in_minutes=120`,
  а `core/timeutils.resolve_day_time` превращает это в дату. Модели регулярно ошибаются с днями недели.
- Время в БД — UTC; LLM отдаёт локальное время пользователя, перевод — в `core/timeutils.py`.
- `core/`, `agent/`, `reminders/` не знают про Telegram — новый интерфейс подключается без изменений ядра.

## Запуск

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env   # заполнить токены
.venv/bin/pim
```

Тесты: `.venv/bin/pytest`

### Переключение модели

`LLM_PROVIDER=gigachat` или `LLM_PROVIDER=openrouter` + `OPENROUTER_MODEL` (например `qwen/qwen3-235b-a22b-2507`,
`google/gemini-2.5-flash`). Модель на OpenRouter должна поддерживать tool calling.

### Деплой (docker)

```bash
docker compose up -d --build
```

БД лежит в `./data/pim.db` на хосте.

## Как добавить новое умение

1. Обработчик + `ToolSpec` в `agent/tools.py`, регистрация в `TOOLS`.
2. Схему параметров держать простой (string/integer/enum, без `anyOf`/`null`) — иначе GigaChat её не съест.
3. Тест со `ScriptedLLM` в `tests/test_agent.py`.

## Roadmap

- [x] Запись дел, напоминания, повторы (RRULE), «сделано / отложить», /tasks
- [x] Голосовые сообщения и кружки (локальный Whisper `large-v3-turbo`, модель качается при первом запуске)
- [x] Утренняя сводка (время настраивается фразой боту, `/digest` — показать сейчас)
- [x] Память о пользователе по команде («запомни…», `/memory`, «удали факт N»), история диалога в БД, очередь на пользователя
- [x] Автопамять: после разговора бот сам запоминает важное и сообщает об этом (выключается фразой)
- [ ] Многопользовательский режим: инвайты, онбординг, лимиты, `/forget_me`
- [ ] Семантическая память (эмбеддинги, Postgres + pgvector) — когда фактов станет много
- [ ] Вечерний «что переносим?»
- [ ] alembic вместо автодобавления колонок
- [ ] Интеграции: календарь, почта; подтверждение необратимых действий
- [ ] API (FastAPI) + приложение / Telegram Mini App
