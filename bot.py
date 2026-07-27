from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db import Database
from generator import render_stars
from pipeline import NotificationNews, TransferPipeline
from sources import SourceStatus, format_source_status


LOGGER = logging.getLogger(__name__)
TEST_TIERS = ("official", "tier1", "tier2", "yellow", "rumor")

TYPE_EMOJI = {
    "official": "🟢",
    "tier1": "🔵",
    "tier2": "🟡",
    "yellow": "🟠",
    "rumor": "⚪",
}


@dataclass(slots=True)
class AppState:
    owner_id: int
    database: Database
    pipeline: TransferPipeline
    source_statuses: dict[str, SourceStatus]
    bot_timezone: str
    target_channel_id: str
    notifications_paused: bool = False


def escape_lines(values: list[str]) -> str:
    return "\n".join(f"• {html.escape(value)}" for value in values)


def notification_keyboard(news_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📝 Сделать пост", callback_data=f"post:{news_id}"))
    return builder.as_markup()


def draft_post_keyboard(post_id: int, *, has_comment: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    buttons = [InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"publish:{post_id}")]
    if not has_comment:
        buttons.append(InlineKeyboardButton(text="💬 Добавить комментарий", callback_data=f"addcomment:{post_id}"))
    builder.row(*buttons)
    return builder.as_markup()


def format_publication_time(published_at: datetime | None, timezone_name: str) -> str | None:
    if published_at is None:
        return None
    local_dt = published_at.astimezone(ZoneInfo(timezone_name))
    return local_dt.strftime("%d.%m.%Y %H:%M")


def parse_db_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                parsed = datetime.strptime(value, fmt)
                parsed = parsed.replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def news_activity_time(published_at: str | None, created_at: str | None) -> datetime | None:
    published = parse_db_datetime(published_at)
    created = parse_db_datetime(created_at)
    if published and created:
        return max(published, created)
    return published or created


def format_age(value: datetime | None) -> str:
    if value is None:
        return "неизвестно"
    delta = max(datetime.now(timezone.utc) - value, timedelta())
    if delta < timedelta(minutes=1):
        return "только что"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)} мин назад"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)} ч назад"
    return f"{delta.days} д назад"


def render_debug_block(news) -> str:
    emoji = TYPE_EMOJI.get(news.source_tier or "rumor", TYPE_EMOJI["rumor"])
    player = html.escape(news.player_name or "—")
    from_club = html.escape(news.from_club or "—")
    to_club = html.escape(news.to_club or "—")
    fee = html.escape(news.fee) if news.fee else ""
    news_type = html.escape(news.news_type or "—")
    stars = render_stars(int(news.stars or 1))
    source_name = html.escape(news.source_name or "—")
    title = html.escape(news.title or "—")
    url = html.escape(news.url or "—")
    age = format_age(news_activity_time(news.published_at, news.created_at))

    lines = [
        f"{emoji} <b>{html.escape(news.source_tier or 'rumor')}</b>",
        f"{player} · {from_club} → {to_club}",
    ]
    if fee:
        lines.append(fee)
    lines.extend(
        [
            f"Тип: {news_type} · ⭐ {stars}",
            f"📡 {source_name}",
            f"Заголовок: {title}",
            f"🔗 {url}",
            f"⏱ {age}",
        ]
    )
    return "\n".join(lines)


def render_test_post_pick(news) -> str:
    emoji = TYPE_EMOJI.get(news.source_tier or "rumor", TYPE_EMOJI["rumor"])
    player = news.player_name or "Игрок не определён"
    from_club = news.from_club or "—"
    to_club = news.to_club or "—"
    source_name = news.source_name or "Источник неизвестен"
    stars = int(news.stars or 0)
    return f"[{news.id}] {emoji} {player} · {from_club} → {to_club} · {source_name} · ⭐{stars}"


def _truncate_button_text(value: str | None, limit: int) -> str:
    text = (value or "—").strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def test_post_keyboard(news_items: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for news in news_items:
        emoji = TYPE_EMOJI.get(news.source_tier or "rumor", TYPE_EMOJI["rumor"])
        player = _truncate_button_text(news.player_name, 18)
        to_club = _truncate_button_text(news.to_club, 14)
        stars = int(news.stars or 0)
        builder.row(
            InlineKeyboardButton(
                text=f"{emoji} {player} · {to_club} · ⭐{stars}",
                callback_data=f"testpost:{news.id}",
            )
        )
    return builder.as_markup()


async def send_text_chunks(bot: Bot, chat_id: int, blocks: list[str], *, parse_mode: str = "HTML") -> None:
    current = ""
    for block in blocks:
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) > 4096 and current:
            await bot.send_message(chat_id, current, parse_mode=parse_mode, disable_web_page_preview=True)
            current = block
        else:
            current = candidate
    if current:
        await bot.send_message(chat_id, current, parse_mode=parse_mode, disable_web_page_preview=True)


def format_notification(payload: NotificationNews, timezone_name: str) -> str:
    emoji = TYPE_EMOJI.get(payload.raw.source_tier, TYPE_EMOJI["rumor"])
    first_line = (
        f"{emoji} <b>{html.escape(payload.extracted.player)}</b> · "
        f"{html.escape(payload.extracted.from_club or '—')} → {html.escape(payload.extracted.to_club or '—')}"
    )
    fee_line = html.escape(payload.extracted.fee) if payload.extracted.fee else ""
    body = html.escape((payload.raw.raw_text or payload.raw.title).strip()[:300])
    link = html.escape(payload.raw.url)
    source_name = html.escape(payload.raw.source_name)
    stars = render_stars(payload.stars)
    publication_time = format_publication_time(payload.raw.published_at, timezone_name)

    parts = [first_line]
    if fee_line:
        parts.append(fee_line)
    if payload.is_upgrade and payload.previous_stars is not None:
        parts.append(f"⬆️ Надёжность выросла: {render_stars(payload.previous_stars)} → {stars}")
    parts.extend(
        [
            "",
            body,
            "",
            f"📡 {source_name}",
            *( [f"🕒 {publication_time}"] if publication_time else [] ),
            f"⭐ {stars}",
            *([f"Подтверждений: {payload.mention_count}"] if payload.mention_count > 1 else []),
            f"🔗 {link}",
        ]
    )
    return "\n".join(parts)


class OwnerNotifier:
    def __init__(self, bot: Bot, state: AppState) -> None:
        self.bot = bot
        self.state = state

    async def send_news(self, payload: NotificationNews) -> None:
        await self.bot.send_message(
            self.state.owner_id,
            format_notification(payload, self.state.bot_timezone),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=notification_keyboard(payload.news_id),
        )

    async def send_source_disabled(self, status: SourceStatus) -> None:
        await self.bot.send_message(
            self.state.owner_id,
            (
                f"Источник отключён после 5 ошибок подряд.\n"
                f"{status.display_name}\n"
                f"Последняя ошибка: {status.last_error or '—'}"
            ),
        )


def setup_handlers(dispatcher: Dispatcher, bot: Bot, state: AppState) -> Router:
    router = Router()

    async def generate_and_send_post(
        *,
        news_id: int,
        callback: CallbackQuery | None = None,
        message: Message | None = None,
    ) -> None:
        try:
            result = await state.pipeline.generate_short_post_for_news(news_id)
        except LookupError:
            if callback is not None:
                await callback.answer("Для этой карточки пост недоступен.", show_alert=True)
            elif message is not None:
                await message.answer(
                    f"Новость с id {news_id} не найдена, напиши /test_post чтобы увидеть список"
                )
            return
        except Exception:
            LOGGER.exception("Failed to generate post for news #%s", news_id)
            if callback is not None:
                await callback.answer("Не удалось собрать пост. Попробуйте ещё раз через минуту.", show_alert=True)
            elif message is not None:
                await message.answer("Не удалось собрать пост. Попробуйте ещё раз через минуту.")
            return

        text = result.text
        await bot.send_message(
            state.owner_id,
            text,
            disable_web_page_preview=True,
            reply_markup=draft_post_keyboard(result.post_id, has_comment=result.has_comment),
        )

    async def ensure_owner(message: Message | CallbackQuery) -> bool:
        user = message.from_user
        if user and user.id == state.owner_id:
            return True
        if isinstance(message, CallbackQuery):
            await message.answer("Доступ только владельцу", show_alert=True)
        else:
            await message.answer("Доступ только владельцу.")
        return False

    @router.message(Command("start"))
    async def start_handler(message: Message) -> None:
        if not await ensure_owner(message):
            return
        await message.answer(
            "Бот активен.\n"
            "Команды: /sources, /pause, /resume, /test, /test_post"
        )

    @router.message(Command("pause"))
    async def pause_handler(message: Message) -> None:
        if not await ensure_owner(message):
            return
        state.notifications_paused = True
        await message.answer("Уведомления остановлены.")

    @router.message(Command("resume"))
    async def resume_handler(message: Message) -> None:
        if not await ensure_owner(message):
            return
        state.notifications_paused = False
        await message.answer("Уведомления возобновлены.")

    @router.message(Command("sources"))
    async def sources_handler(message: Message) -> None:
        if not await ensure_owner(message):
            return
        await message.answer(format_source_status(state.source_statuses))

    @router.message(Command("test"))
    async def test_handler(message: Message) -> None:
        if not await ensure_owner(message):
            return

        recent_news = await state.database.recent_news_by_tiers(list(TEST_TIERS), limit=1000)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        recent_filtered = [
            news
            for news in recent_news
            if (activity_time := news_activity_time(news.published_at, news.created_at)) and activity_time >= cutoff
        ]

        latest_by_tier = {}
        counts = {tier: 0 for tier in TEST_TIERS}
        for news in recent_filtered:
            tier = news.source_tier or "rumor"
            if tier not in counts:
                continue
            counts[tier] += 1
            current_best = latest_by_tier.get(tier)
            if current_best is None:
                latest_by_tier[tier] = news
                continue
            current_time = news_activity_time(current_best.published_at, current_best.created_at)
            candidate_time = news_activity_time(news.published_at, news.created_at)
            if candidate_time and (current_time is None or candidate_time > current_time):
                latest_by_tier[tier] = news

        blocks: list[str] = []
        for tier in TEST_TIERS:
            if tier in latest_by_tier:
                blocks.append(render_debug_block(latest_by_tier[tier]))
            else:
                blocks.append(f"{TYPE_EMOJI[tier]} {tier} — нет новостей за 24 ч")

        total = sum(counts.values())
        blocks.append(
            "Всего за 24 ч: "
            f"{total} новостей · "
            f"tier1: {counts['tier1']} · "
            f"tier2: {counts['tier2']} · "
            f"yellow: {counts['yellow']} · "
            f"rumor: {counts['rumor']} · "
            f"official: {counts['official']}"
        )
        await send_text_chunks(bot, state.owner_id, blocks)

    @router.message(Command("test_post"))
    async def test_post_handler(message: Message, command: CommandObject) -> None:
        if not await ensure_owner(message):
            return
        if not command.args:
            latest_news = await state.database.latest_news(3)
            if not latest_news:
                await message.answer("В базе пока нет новостей.")
                return
            await message.answer(
                "Выбери новость для теста:",
                reply_markup=test_post_keyboard(latest_news),
            )
            return
        try:
            news_id = int(command.args.strip())
        except ValueError:
            await message.answer("id новости должен быть числом.")
            return
        await message.answer(f"Генерирую пост для news_id={news_id}…")
        await generate_and_send_post(news_id=news_id, message=message)

    @router.callback_query(F.data.startswith("testpost:"))
    async def test_post_pick_handler(callback: CallbackQuery) -> None:
        if not await ensure_owner(callback):
            return
        news_id = int(callback.data.split(":", 1)[1])
        await callback.answer("Генерирую пост…")
        await generate_and_send_post(news_id=news_id, callback=callback)

    @router.callback_query(F.data.startswith("post:"))
    async def post_handler(callback: CallbackQuery) -> None:
        if not await ensure_owner(callback):
            return
        news_id = int(callback.data.split(":", 1)[1])
        await callback.answer("Генерирую пост…")
        await generate_and_send_post(news_id=news_id, callback=callback)

    @router.callback_query(F.data.startswith("addcomment:"))
    async def add_comment_handler(callback: CallbackQuery) -> None:
        if not await ensure_owner(callback):
            return
        post_id = int(callback.data.split(":", 1)[1])
        try:
            result = await state.pipeline.add_comment_to_post(post_id)
        except LookupError:
            await callback.answer("Черновик не найден.", show_alert=True)
            return
        except Exception:
            LOGGER.exception("Failed to add comment for post #%s", post_id)
            await callback.answer("Не удалось добавить комментарий. Попробуйте ещё раз через минуту.", show_alert=True)
            return
        await callback.message.edit_text(
            result.text,
            disable_web_page_preview=True,
            reply_markup=draft_post_keyboard(result.post_id, has_comment=True),
        )
        await callback.answer("Комментарий добавлен.")

    @router.callback_query(F.data.startswith("publish:"))
    async def publish_handler(callback: CallbackQuery) -> None:
        if not await ensure_owner(callback):
            return
        post_id = int(callback.data.split(":", 1)[1])
        if not state.target_channel_id:
            await callback.answer("TARGET_CHANNEL_ID не настроен.", show_alert=True)
            return
        try:
            draft = await state.pipeline.get_draft_post(post_id)
        except LookupError:
            await callback.answer("Черновик не найден.", show_alert=True)
            return
        if draft.is_published:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer("Этот пост уже опубликован.")
            return
        try:
            published_message = await bot.send_message(
                state.target_channel_id,
                draft.text,
                disable_web_page_preview=True,
            )
            await state.database.mark_post_published(post_id, channel_message_id=published_message.message_id)
        except Exception:
            LOGGER.exception("Failed to publish post #%s", post_id)
            await callback.answer("Не удалось опубликовать пост.", show_alert=True)
            return
        published_text = f"{draft.text}\n\n✅ Опубликовано"
        await callback.message.edit_text(
            published_text,
            disable_web_page_preview=True,
            reply_markup=None,
        )
        await callback.answer("Опубликовано.")

    dispatcher.include_router(router)
    return router
