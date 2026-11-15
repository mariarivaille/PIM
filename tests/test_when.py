from datetime import time, timedelta

import pytest

from pim.agent.tools import ToolContext, run_tool
from pim.core.models import utcnow
from pim.core.timeutils import parse_local, resolve_day_time, to_local

from tests.conftest import TZ

MONDAY_EVENING = parse_local("2026-11-09T22:40", TZ)


@pytest.mark.parametrize("day, expected", [
    ("today", "2026-11-09"),
    ("tomorrow", "2026-11-10"),
    ("day_after_tomorrow", "2026-11-11"),
    ("thu", "2026-11-12"),
    ("fri", "2026-11-13"),
    ("sat", "2026-11-14"),
    ("sun", "2026-11-15"),
    ("mon", "2026-11-16"),  # сегодняшний день недели — следующая неделя
])
def test_weekdays(day, expected):
    dt = to_local(resolve_day_time(MONDAY_EVENING, TZ, day, time(19, 0)), TZ)
    assert dt.date().isoformat() == expected
    assert (dt.hour, dt.minute) == (19, 0)


def test_time_only_in_past_moves_to_tomorrow():
    dt = to_local(resolve_day_time(MONDAY_EVENING, TZ, None, time(9, 0)), TZ)
    assert dt.date().isoformat() == "2026-11-10"
    dt = to_local(resolve_day_time(MONDAY_EVENING, TZ, None, time(23, 30)), TZ)
    assert dt.date().isoformat() == "2026-11-09"


def test_day_without_time_defaults_to_10():
    dt = to_local(resolve_day_time(MONDAY_EVENING, TZ, "fri", None), TZ)
    assert (dt.hour, dt.minute) == (10, 0)


async def test_tool_in_minutes(repo, user):
    ctx = ToolContext(repo, user)
    await run_tool(ctx, "create_task", {"title": "x", "in_minutes": 120})
    task = ctx.touched[0]
    assert abs(task.due_at - (utcnow() + timedelta(minutes=120))) < timedelta(seconds=5)


async def test_tool_validates_day_and_time(repo, user):
    ctx = ToolContext(repo, user)
    assert "error" in await run_tool(ctx, "create_task", {"title": "x", "day": "пятница"})
    assert "error" in await run_tool(ctx, "create_task", {"title": "x", "day": "fri", "time": "вечером"})


async def test_update_time_only_keeps_day(repo, user):
    ctx = ToolContext(repo, user)
    task = await repo.create_task(user.id, "банк", due_at=parse_local("2030-01-10T10:00", TZ))
    await run_tool(ctx, "update_task", {"task_id": task.id, "time": "12:00"})
    updated = to_local((await repo.get_task(user.id, task.id)).due_at, TZ)
    assert updated.isoformat().startswith("2030-01-10T12:00")


async def test_due_at_without_time_defaults_to_10(repo, user):
    ctx = ToolContext(repo, user, asked_for_time=True)  # подсказку модели уже давали — сразу запасное время
    for value in ("2030-03-11", "2030-03-11T00:00:00"):
        await run_tool(ctx, "create_task", {"title": value, "due_at": value})
    explicit = await run_tool(ctx, "create_task", {"title": "x", "due_at": "2030-03-11T18:30"})
    hours = [to_local(t.due_at, TZ).strftime("%H:%M") for t in ctx.touched]
    assert hours == ["10:00", "10:00", "18:30"]
    assert explicit["created"]["due"].endswith("18:30")


def test_humanize_until():
    from pim.core.timeutils import humanize_until
    now = parse_local("2026-11-10T00:20", TZ)
    assert humanize_until(now + timedelta(minutes=25), now, TZ) == "через 25 мин"
    assert humanize_until(now + timedelta(hours=9, minutes=40), now, TZ) == "через 9 ч 40 мин"
    assert humanize_until(now + timedelta(hours=2), now, TZ) == "через 2 ч"
    assert humanize_until(parse_local("2026-11-12T10:00", TZ), now, TZ) == "через 2 дня"
    assert humanize_until(parse_local("2026-11-15T10:00", TZ), now, TZ) == "через 5 дней"
    assert humanize_until(parse_local("2026-11-11T10:00", TZ) + timedelta(days=20), now, TZ) == "через 21 день"
    assert humanize_until(now - timedelta(minutes=1), now, TZ) == ""


@pytest.mark.parametrize("now_local, expected", [
    ("2026-11-10T14:10", "2026-11-10T15:30"),  # через час, округление до :00/:30
    ("2026-11-10T15:00", "2026-11-10T16:00"),
    ("2026-11-10T21:30", "2026-11-11T10:00"),  # попадает на ночь — завтра утром
    ("2026-11-10T02:00", "2026-11-10T10:00"),  # глубокая ночь — сегодня утром
])
def test_nearest_reasonable_time(now_local, expected):
    from pim.core.timeutils import nearest_reasonable_time
    got = nearest_reasonable_time(parse_local(now_local, TZ), TZ)
    assert to_local(got, TZ).strftime("%Y-%m-%dT%H:%M") == expected


async def test_auto_time_is_flagged_and_never_in_past(repo, user, monkeypatch):
    now = parse_local("2026-11-10T15:00", TZ)
    monkeypatch.setattr("pim.agent.tools.utcnow", lambda: now)
    ctx = ToolContext(repo, user)
    # модель выбрала время сама
    guessed = await run_tool(ctx, "create_task", {"title": "позвонить в банк", "day": "tomorrow",
                                                  "time": "11:00", "time_guessed": True})
    assert guessed["created"]["time_chosen_by_assistant"] and guessed["created"]["in"] == "через 20 ч"
    # пользователь назвал время — не помечаем
    named = await run_tool(ctx, "create_task", {"title": "x", "day": "tomorrow", "time": "11:00"})
    assert "time_chosen_by_assistant" not in named["created"]
    # «сегодня» без времени: сначала просим модель выбрать время сама
    nudge = await run_tool(ctx, "create_task", {"title": "купить хлеб", "day": "today"})
    assert "error" in nudge and "time_guessed=true" in nudge["error"]
    # модель снова без времени — запасные 10:00 уже прошли, ставим на ближайшее разумное
    today = await run_tool(ctx, "create_task", {"title": "купить хлеб", "day": "today"})
    assert today["created"]["time_chosen_by_assistant"] and today["created"]["due"] == "вт 10.11 16:00"
    assert ctx.auto_time == {ctx.touched[0].id, ctx.touched[2].id}


async def test_remind_before_event(repo, user):
    ctx = ToolContext(repo, user)
    res = await run_tool(ctx, "create_task", {"title": "собрание", "due_at": "2030-11-13T18:00",
                                               "remind_before_minutes": 1440, "notes": "каб. 214"})
    task = ctx.touched[0]
    assert to_local(task.due_at, TZ).strftime("%d.%m %H:%M") == "13.11 18:00"  # время дела не трогаем
    assert to_local(task.remind_at, TZ).strftime("%d.%m %H:%M") == "12.11 18:00"
    assert task.notes == "каб. 214"
    assert res["created"]["remind_at"].endswith("12.11 18:00") and res["created"]["remind_before"] == "1 день"


async def test_going_out_gets_lead_time(repo, user):
    from datetime import timedelta
    ctx = ToolContext(repo, user)
    args = {"title": "забрать посылку на почте", "due_at": "2030-11-23T12:00"}
    assert "дороги" in (await run_tool(ctx, "create_task", args))["error"]  # просим подумать о дороге
    await run_tool(ctx, "create_task", {**args, "remind_before_minutes": 45})
    task = ctx.touched[0]
    assert to_local(task.remind_at, TZ).strftime("%H:%M") == "11:15"
    # звенит в 11:15, а не в 12:00
    assert [t.id for t, _ in await repo.due_tasks(task.remind_at)] == [task.id]
    assert await repo.due_tasks(task.remind_at - timedelta(minutes=1)) == []
    # звонок — без запаса и без вопросов
    ctx2 = ToolContext(repo, user)
    await run_tool(ctx2, "create_task", {"title": "позвонить маме", "due_at": "2030-11-23T12:00"})
    assert ctx2.touched[0].remind_at is None


async def test_lead_time_moves_with_task(repo, user):
    from datetime import timedelta
    task = await repo.create_task(user.id, "маникюр", due_at=parse_local("2030-11-23T12:00", TZ),
                                  remind_at=parse_local("2030-11-23T11:15", TZ))
    # перенос дела — напоминание едет следом с тем же запасом
    moved = await run_tool(ToolContext(repo, user), "update_task", {"task_id": task.id, "time": "15:00"})
    assert moved["updated"]["remind_at"].endswith("14:15")
    # «отложить на 15 мин» под напоминанием — двигается только напоминание
    snoozed = await repo.snooze_task(user.id, task.id, parse_local("2030-11-23T14:30", TZ))
    assert to_local(snoozed.due_at, TZ).strftime("%H:%M") == "15:00"
    assert to_local(snoozed.remind_at, TZ).strftime("%H:%M") == "14:30"
    # отложили дальше самого дела — переносится дело
    later = await repo.snooze_task(user.id, task.id, parse_local("2030-11-24T10:00", TZ))
    assert to_local(later.due_at, TZ).strftime("%d.%m %H:%M") == "24.11 10:00" and later.remind_at is None
    # пользователь просит другой запас
    res = await run_tool(ToolContext(repo, user), "update_task", {"task_id": task.id, "remind_before_minutes": 60})
    assert res["updated"]["remind_at"].endswith("09:00")
    res = await run_tool(ToolContext(repo, user), "update_task", {"task_id": task.id, "remind_before_minutes": 0})
    assert "remind_at" not in res["updated"]


async def test_recurring_keeps_lead_time(repo, user):
    task = await repo.create_task(user.id, "спортзал", due_at=parse_local("2026-11-16T19:00", TZ),
                                  rrule="FREQ=WEEKLY", remind_at=parse_local("2026-11-16T18:15", TZ))
    nxt = await repo.complete_task(user.id, task.id, TZ)
    assert to_local(nxt.remind_at, TZ).strftime("%d.%m %H:%M") == "23.11 18:15"


def test_lead_text():
    from datetime import timedelta
    from pim.agent.tools import lead_text
    assert lead_text(timedelta(minutes=45)) == "45 мин"
    assert lead_text(timedelta(minutes=90)) == "1 ч 30 мин"
    assert lead_text(timedelta(days=2)) == "2 дня"


def test_strip_time_words():
    from pim.core.timeutils import strip_time_words as f
    assert f("обнять милую в обед") == "обнять милую"
    assert f("позвонить маме завтра в 19") == "позвонить маме"
    assert f("встать в 7 утра на пробежку") == "встать на пробежку"
    assert f("сдать отчёт к 10:00") == "сдать отчёт"
    assert f("встреча в 2 этаже") == "встреча в 2 этаже"  # не время
    assert f("пообедать с Андреем") == "пообедать с Андреем"
    assert f("в обед") == "в обед"  # пусто не оставляем


async def test_title_has_no_time_after_create_or_move(repo, user):
    ctx = ToolContext(repo, user)
    res = await run_tool(ctx, "create_task", {"title": "обнять милую в обед", "day": "today", "time": "13:00"})
    assert res["created"]["title"] == "обнять милую"
    ctx.user_text = "подумать про отпуск в выходные, без срока"
    no_due = await run_tool(ctx, "create_task", {"title": "подумать про отпуск в выходные"})
    assert no_due["created"]["title"] == "подумать про отпуск в выходные"  # без срока время — единственное место
    # перенос дела, в названии которого осталось время, — название чистится
    task = await repo.create_task(user.id, "позвонить маме вечером", due_at=parse_local("2030-01-10T19:00", TZ))
    res = await run_tool(ctx, "update_task", {"task_id": task.id, "time": "10:00"})
    assert res["updated"]["title"] == "позвонить маме"


async def test_task_without_any_time_gets_one(repo, user):
    ctx = ToolContext(repo, user, user_text="купить таблетки от укачивания")
    res = await run_tool(ctx, "create_task", {"title": "купить таблетки от укачивания"})
    assert "срок не указан" in res["error"]  # сначала просим модель выбрать время
    res = await run_tool(ctx, "create_task", {"title": "купить таблетки от укачивания"})
    assert res["created"]["due"] and res["created"]["time_chosen_by_assistant"]  # упрямится — ставит код
    assert ctx.touched[0].due_at > utcnow()


async def test_explicit_no_deadline_is_respected(repo, user):
    ctx = ToolContext(repo, user, user_text="запиши когда-нибудь выучить испанский")
    res = await run_tool(ctx, "create_task", {"title": "выучить испанский"})
    assert res["created"]["due"] is None


async def test_repeated_task_is_not_duplicated(repo, user):
    ctx = ToolContext(repo, user)
    await run_tool(ctx, "create_task", {"title": "забрать права", "due_at": "2030-11-17T10:00",
                                        "remind_before_minutes": 0})
    res = await run_tool(ToolContext(repo, user), "create_task", {"title": "Забрать права", "due_at": "2030-11-17T12:00",
                                                                  "remind_before_minutes": 0})
    assert "already_exists" in res and len(await repo.list_open_tasks(user.id)) == 1
    await run_tool(ToolContext(repo, user), "create_task", {"title": "забрать права", "due_at": "2030-11-24T10:00",
                                                            "remind_before_minutes": 0})
    assert len(await repo.list_open_tasks(user.id)) == 2  # другой день — отдельное дело


async def test_named_date_beats_nearest_weekday(repo, user):
    from pim.core.timeutils import explicit_dates
    from datetime import date
    today = date(2026, 11, 15)
    assert explicit_dates("Маникюр 26 ноября четверг в 19", today) == [date(2026, 11, 26)]
    assert explicit_dates("до 5.01 сдать, в 19.30 созвон", today) == [date(2027, 1, 5)]  # 19.30 — время
    # модель передала day=thu, а пользователь назвал 26 ноября (тоже четверг) — берём дату
    ctx = ToolContext(repo, user, user_text=".",
                      message_text="[В ответ на своё сообщение: «Маникюр 26 ноября четверг в 19»]\n.")
    weekday_of_26 = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][explicit_dates("26 ноября", to_local(
        utcnow(), TZ).date())[0].weekday()]
    res = await run_tool(ctx, "create_event", {"title": "маникюр", "day": weekday_of_26, "start": "19:00",
                                               "minutes": 90, "remind_before_minutes": 90})
    assert "26.11 19:00" in res["created_event"]["when"]
    # другая дата в тексте с другим днём недели не мешает: «до 22 ноября подать, напомни в пятницу»
    ctx = ToolContext(repo, user, message_text="до 22 ноября подать, напомни в пятницу")
    target = explicit_dates("22 ноября", to_local(utcnow(), TZ).date())[0]
    other = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][(target.weekday() + 1) % 7]
    res = await run_tool(ctx, "create_task", {"title": "подать", "day": other, "time": "19:00"})
    assert f"{target:%d.%m}" not in res["created"]["due"]


async def test_appointment_as_task_suggests_event(repo, user):
    ctx = ToolContext(repo, user)
    res = await run_tool(ctx, "create_task", {"title": "сделать маникюр", "due_at": "2030-11-26T19:00",
                                              "remind_before_minutes": 90})
    assert "create_event" in res["error"]
    # «записаться на маникюр» — это дело, не событие
    res = await run_tool(ToolContext(repo, user), "create_task", {"title": "записаться на маникюр",
                                                                  "due_at": "2030-11-20T12:00"})
    assert "created" in res
