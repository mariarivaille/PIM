import asyncio
import logging

from pim.agent.agent import Agent, History
from pim.config import get_settings
from pim.core.db import init_db, make_engine, make_sessionmaker
from pim.core.repository import Repository
from pim.llm.factory import make_provider
from pim.memory.reflection import Reflector
from pim.planning.day import DayPlanner
from pim.reminders.scheduler import ReminderScheduler
from pim.stt.factory import make_transcriber
from pim.telegram.bot import TelegramNotifier, make_bot, make_dispatcher, setup_commands

log = logging.getLogger(__name__)


async def _warmup(transcriber) -> None:
    try:
        await transcriber.warmup()
    except Exception:
        log.exception("STT warmup failed, voice messages will retry on first use")


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    engine = make_engine(settings.database_url)
    await init_db(engine)
    repo = Repository(make_sessionmaker(engine))
    if found := await repo.backfill_memory_dates():
        log.info("found yearly dates in %d existing memories", found)

    history = History(repo, settings.history_size, ttl_seconds=settings.history_ttl_minutes * 60)
    llm = make_provider(settings)
    planner = DayPlanner(llm, repo)
    agent = Agent(llm, repo, history, max_memories=settings.max_memories, planner=planner)
    transcriber = make_transcriber(settings)
    bot = make_bot(settings)
    dp = make_dispatcher(agent, repo, settings, transcriber)
    dp.startup.register(setup_commands)
    scheduler = ReminderScheduler(
        repo, TelegramNotifier(bot, repo), settings.reminder_poll_seconds,
        reflector=Reflector(llm, repo, settings.max_memories),
        reflection_idle_minutes=settings.reflection_idle_minutes,
    )

    background = [asyncio.create_task(scheduler.run_forever())]
    if transcriber is not None:
        # модель грузится (и при первом запуске скачивается) в фоне, бот уже отвечает на текст
        background.append(asyncio.create_task(_warmup(transcriber)))
    try:
        await dp.start_polling(bot)
    finally:
        for task in background:
            task.cancel()
        await bot.session.close()
        await engine.dispose()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
