import json
from datetime import timedelta

from pim.agent.tools import ToolContext, run_tool
from pim.core.models import MemoryKind, MemorySource, utcnow
from pim.llm.base import Message
from pim.memory.reflection import Reflector, parse_reflection
from pim.reminders.scheduler import ReminderScheduler
from pim.telegram.keyboards import memory_update_text

from tests.conftest import TZ
from tests.test_agent import ScriptedLLM


def answer(add=(), update=(), wrap=False):
    text = json.dumps({"add": list(add), "update": list(update)}, ensure_ascii=False)
    return Message("assistant", f"Вот результат:\n```json\n{text}\n```" if wrap else text)


async def chat(repo, user, *lines, at=None):
    at = at or utcnow() - timedelta(minutes=30)
    await repo.add_messages(user.id, *[("user" if i % 2 == 0 else "assistant", t) for i, t in enumerate(lines)], at=at)


async def test_reflection_adds_updates_and_dedupes(repo, user):
    await repo.add_memory(user.id, "Мама — Ольга, ДР 12 марта", MemoryKind.PERSON)
    await chat(repo, user, "у мамы ДР 14 марта, а брата зовут Савва", "Понял")
    llm = ScriptedLLM([answer(
        add=[{"kind": "person", "content": "Брат — Савва"},
             {"kind": "person", "content": "мама — Ольга, ДР 12 марта!"},      # дубль существующего
             {"kind": "weird", "content": "Любит кофе"},                      # неизвестный kind → fact
             {"kind": "fact", "content": "В отпуске", "until_date": "2001-01-01"}],  # срок прошёл
        update=[{"number": 1, "content": "Мама — Ольга, ДР 14 марта"},
                {"number": 99, "content": "нет такого"}],
        wrap=True,
    )])
    result = await Reflector(llm, repo, max_memories=150).reflect(user)

    assert [(m.content, m.kind, m.source) for m in result.added] == [
        ("Брат — Савва", MemoryKind.PERSON, MemorySource.INFERRED),
        ("Любит кофе", MemoryKind.FACT, MemorySource.INFERRED),
    ]
    assert [m.content for m in result.updated] == ["Мама — Ольга, ДР 14 марта"]
    # переписка и память ушли модели
    prompt = llm.calls[0][0].content
    assert "брата зовут Савва" in prompt and "1. person: Мама — Ольга" in prompt
    # повторно тот же кусок не разбираем
    assert await repo.users_pending_reflection(utcnow()) == []


async def test_reflection_respects_memory_limit(repo, user):
    await repo.add_memory(user.id, "a")
    await chat(repo, user, "много фактов")
    llm = ScriptedLLM([answer(add=[{"kind": "fact", "content": c} for c in ("b", "c", "d")])])
    result = await Reflector(llm, repo, max_memories=2).reflect(user)
    assert [m.content for m in result.added] == ["b"]


async def test_unparsable_answer_is_marked_processed(repo, user):
    await chat(repo, user, "привет")
    llm = ScriptedLLM([Message("assistant", "Ничего важного нет.")])
    result = await Reflector(llm, repo, max_memories=150).reflect(user)
    assert result.empty
    assert await repo.users_pending_reflection(utcnow()) == []


def test_parse_reflection():
    assert parse_reflection('blah {"add": [], "update": []} blah') == {"add": [], "update": []}


async def test_pending_reflection_waits_for_silence_and_respects_toggle(repo, user):
    now = utcnow()
    await chat(repo, user, "свежее сообщение", at=now - timedelta(minutes=2))
    assert await repo.users_pending_reflection(idle_before=now - timedelta(minutes=10)) == []
    assert [u.id for u in await repo.users_pending_reflection(idle_before=now)] == [user.id]
    await run_tool(ToolContext(repo, user), "set_auto_memory", {"enabled": False})
    assert await repo.users_pending_reflection(idle_before=now) == []


async def test_scheduler_reflection_notifies_and_backs_off(repo, user):
    await chat(repo, user, "брата зовут Савва")
    sent = []

    class Notifier:
        async def send_memory_update(self, u, result):
            sent.append(memory_update_text(result, TZ))

    llm = ScriptedLLM([answer(add=[{"kind": "person", "content": "Брат — Савва"}])])
    sched = ReminderScheduler(repo, Notifier(), 30, reflector=Reflector(llm, repo, 150), reflection_idle_minutes=10)
    assert await sched.tick_reflection() == 1
    assert "Запомнил из разговора" in sent[0] and "1. Брат — Савва <i>(сам)</i>" in sent[0]
    assert await sched.tick_reflection() == 0  # уже разобрано

    # LLM упала — повторяем не на каждом тике, а после паузы
    await chat(repo, user, "ещё что-то")

    class Broken:
        async def chat(self, messages, tools):
            Broken.calls += 1
            raise RuntimeError("LLM down")
    Broken.calls = 0
    sched = ReminderScheduler(repo, Notifier(), 30, reflector=Reflector(Broken(), repo, 150), reflection_idle_minutes=10)
    await sched.tick_reflection()
    await sched.tick_reflection()
    assert Broken.calls == 1
