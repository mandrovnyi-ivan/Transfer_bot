from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot import AppState, OwnerNotifier, setup_handlers
from db import Database
from generator import PostGenerator
from pipeline import TransferPipeline
from settings import Settings, load_sources_config
from sources import SourceStatus, SourcesClient


LOGGER = logging.getLogger(__name__)


async def run_with_status(
    *,
    status: SourceStatus,
    fetcher: Callable[[], Awaitable[list]],
    process: Callable[[list], Awaitable[int]],
    notifier: OwnerNotifier,
    log_name: str,
) -> tuple[int, int]:
    if not status.enabled:
        return (0, 0)
    try:
        items = await fetcher()
        status.consecutive_failures = 0
        status.last_error = None
        status.last_success_at = datetime.now(timezone.utc)
        saved = await process(items)
        return (len(items), saved)
    except Exception as exc:
        LOGGER.exception("Polling failed for %s", log_name)
        status.consecutive_failures += 1
        status.last_error = str(exc)
        status.last_error_at = datetime.now(timezone.utc)
        if status.consecutive_failures >= 5:
            status.enabled = False
            await notifier.send_source_disabled(status)
        return (0, 0)


async def poll_x_sources(
    *,
    client: SourcesClient,
    pipeline: TransferPipeline,
    statuses: dict[str, SourceStatus],
    notifier: OwnerNotifier,
    paused: Callable[[], bool],
) -> None:
    enabled_accounts = [account for account in client.config.x_accounts if account.enabled]
    LOGGER.info("poll_x_sources: старт, аккаунтов=%s", len(enabled_accounts))
    backend = await client.check_x_backend()
    if not backend.ok:
        LOGGER.warning("poll_x_sources: backend error: %s", backend.error or "unknown error")
        status = statuses.setdefault(
            "x:backend",
            SourceStatus(
                key="x:backend",
                display_name="X backend (RSSHub/Nitter)",
                source_kind="x",
            ),
        )
        status.consecutive_failures += 1
        status.last_error = backend.error
        status.last_error_at = datetime.now(timezone.utc)
        if status.consecutive_failures >= 5 and status.enabled:
            status.enabled = False
            await notifier.send_source_disabled(status)
        return

    backend_status = statuses.setdefault(
        "x:backend",
        SourceStatus(
            key="x:backend",
            display_name="X backend (RSSHub/Nitter)",
            source_kind="x",
        ),
    )
    backend_status.enabled = True
    backend_status.consecutive_failures = 0
    backend_status.last_error = None
    backend_status.last_success_at = datetime.now(timezone.utc)

    async def poll_account(account) -> tuple[int, int]:
        key = f"x:{account.username.casefold()}"
        status = statuses[key]
        raw_count, saved_count = await run_with_status(
            status=status,
            fetcher=lambda account=account: client.fetch_x_news(account),
            process=lambda items: pipeline.process_batch(items, deliver_notifications=not paused()),
            notifier=notifier,
            log_name=f"@{account.username}",
        )
        if raw_count == 0:
            LOGGER.warning("X %s: получено 0 сырых, причина=%s", account.username, status.last_error or "пустой фид")
        else:
            LOGGER.info("X %s: получено %s сырых, прошло фильтр %s", account.username, raw_count, saved_count)
        return raw_count, saved_count

    tasks = [poll_account(account) for account in enabled_accounts]
    if tasks:
        results = await asyncio.gather(*tasks)
        total_raw = sum(raw for raw, _ in results)
        total_saved = sum(saved for _, saved in results)
    else:
        total_raw = 0
        total_saved = 0
    LOGGER.info("poll_x_sources: итого получено %s, в базу ушло %s", total_raw, total_saved)


async def poll_rss_sources(
    *,
    client: SourcesClient,
    pipeline: TransferPipeline,
    statuses: dict[str, SourceStatus],
    notifier: OwnerNotifier,
    paused: Callable[[], bool],
) -> None:
    tasks = []
    for feed in client.config.rss_feeds:
        if not feed.enabled:
            continue
        key = f"rss:{feed.name.casefold()}"
        status = statuses[key]
        tasks.append(
            run_with_status(
                status=status,
                fetcher=lambda feed=feed: client.fetch_rss_news(feed),
                process=lambda items: pipeline.process_batch(items, deliver_notifications=not paused()),
                notifier=notifier,
                log_name=feed.name,
            )
        )
    if tasks:
        await asyncio.gather(*tasks)


async def main() -> None:
    settings = Settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_sources_config(settings.config_path)
    database = Database(settings.database_path)
    await database.init()

    telegram_bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dispatcher = Dispatcher()

    post_generator = PostGenerator(settings.anthropic_api_key, settings.generation_model)
    state = AppState(
        owner_id=settings.owner_id,
        database=database,
        pipeline=None,  # type: ignore[arg-type]
        source_statuses={},
        bot_timezone=settings.bot_timezone,
        target_channel_id=settings.target_channel_id,
    )
    owner_notifier = OwnerNotifier(telegram_bot, state)
    pipeline = TransferPipeline(
        database=database,
        settings=settings,
        extraction_api_key=settings.anthropic_api_key,
        extraction_model=settings.extraction_model,
        generator=post_generator,
        notifier=owner_notifier.send_news,
    )
    state.pipeline = pipeline

    sources_client = SourcesClient(settings, config)
    state.source_statuses = sources_client.build_statuses()

    setup_handlers(dispatcher, telegram_bot, state)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        poll_x_sources,
        trigger="interval",
        seconds=settings.x_poll_interval_seconds,
        next_run_time=datetime.now(timezone.utc),
        kwargs={
            "client": sources_client,
            "pipeline": pipeline,
            "statuses": state.source_statuses,
            "notifier": owner_notifier,
            "paused": lambda: state.notifications_paused,
        },
    )
    scheduler.add_job(
        poll_rss_sources,
        trigger="interval",
        minutes=settings.rss_poll_interval_minutes,
        next_run_time=datetime.now(timezone.utc),
        kwargs={
            "client": sources_client,
            "pipeline": pipeline,
            "statuses": state.source_statuses,
            "notifier": owner_notifier,
            "paused": lambda: state.notifications_paused,
        },
    )
    scheduler.start()

    try:
        await dispatcher.start_polling(telegram_bot)
    finally:
        scheduler.shutdown(wait=False)
        await sources_client.close()
        await telegram_bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
