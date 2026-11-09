import asyncio
import logging

from aid.agent.agent import Agent, History
from aid.config import get_settings
from aid.core.db import init_db, make_engine, make_sessionmaker
from aid.core.repository import Repository
from aid.llm.factory import make_provider
from aid.reminders.scheduler import ReminderScheduler
from aid.telegram.bot import TelegramNotifier, make_bot, make_dispatcher


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    engine = make_engine(settings.database_url)
    await init_db(engine)
    repo = Repository(make_sessionmaker(engine))

    history = History(settings.history_size, ttl_seconds=settings.history_ttl_minutes * 60)
    agent = Agent(make_provider(settings), repo, history)
    bot = make_bot(settings)
    dp = make_dispatcher(agent, repo, settings)
    scheduler = ReminderScheduler(repo, TelegramNotifier(bot), settings.reminder_poll_seconds)

    scheduler_task = asyncio.create_task(scheduler.run_forever())
    try:
        await dp.start_polling(bot)
    finally:
        scheduler_task.cancel()
        await bot.session.close()
        await engine.dispose()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
