# AIDAssistant

Личный LLM-ассистент в Telegram (запись дел + напоминания). Архитектура и запуск — в README.md.

## Правила

- Слои: `telegram/` → `agent/` → `llm/`, `core/`; `reminders/` → `core/`. Ядро (`core`, `agent`, `reminders`) не импортирует aiogram.
- Время напоминания решает БД + `ReminderScheduler`, а не LLM.
- Все datetime — aware; в БД UTC (`UTCDateTime`). От LLM — локальное ISO без смещения → `parse_local`.
- Новые инструменты агента — в `agent/tools.py`; JSON Schema без `anyOf`/`null` (ограничение GigaChat).
- Ошибки, которые модель может исправить, — `ToolError` (уходят модели как `{"error": ...}`).
- Тестируется на GigaChat, в проде — модели через OpenRouter; изменения в `llm/` проверять на обоих адаптерах.
- Окружение: `.venv` (Python 3.11), тесты `.venv/bin/pytest`.
