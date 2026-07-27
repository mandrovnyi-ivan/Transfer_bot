from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

try:
    from anthropic import AsyncAnthropic
except ImportError:  # pragma: no cover - exercised only without runtime deps
    AsyncAnthropic = None  # type: ignore[assignment]

from db import Database, NewsRow, PostRow, utcnow_iso
from generator import CommentResult, PostGenerator, ShortPostResult
from reliability import BASE_STARS, calculate_reliability
from settings import Settings
from sources import RawNews


LOGGER = logging.getLogger(__name__)
TRANSFER_TYPES = {
    "official",
    "here_we_go",
    "medical",
    "talks",
    "bid",
    "interest",
    "rumor",
    "denial",
    "renewal",
    "loan",
}


@dataclass(slots=True)
class ExtractedNews:
    player: str
    player_slug: str
    from_club: str | None
    to_club: str | None
    fee: str | None
    type: str
    is_transfer_news: bool


@dataclass(slots=True)
class NotificationNews:
    news_id: int
    raw: RawNews
    extracted: ExtractedNews
    stars: int
    reasons: list[str]
    is_upgrade: bool = False
    previous_stars: int | None = None
    mention_count: int = 1
    source_names: list[str] | None = None


@dataclass(slots=True)
class DraftPost:
    post_id: int
    news_id: int
    text: str
    has_comment: bool
    is_published: bool


Notifier = Callable[[NotificationNews], Awaitable[None]]


def slugify(value: str) -> str:
    lowered = value.casefold().strip()
    lowered = re.sub(r"[^a-z0-9а-яё]+", "-", lowered)
    lowered = re.sub(r"-{2,}", "-", lowered).strip("-")
    return lowered or "unknown-player"


def normalize_club_name(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def slug_aliases(value: str) -> list[str]:
    parts = [part for part in value.split("-") if part]
    aliases = [value]
    if len(parts) > 1:
        aliases.append(parts[-1])
    return list(dict.fromkeys(alias for alias in aliases if alias))


def parse_source_names(value: str | None, fallback: str | None = None) -> list[str]:
    names: list[str] = []
    if value:
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                names.extend(str(item).strip() for item in parsed if str(item).strip())
        except Exception:
            pass
    if fallback and fallback not in names:
        names.append(fallback)
    return names


def merge_source_names(existing_json: str | None, new_source: str) -> list[str]:
    names = parse_source_names(existing_json)
    if new_source not in names:
        names.append(new_source)
    return names


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    cleaned = parts._replace(
        scheme=parts.scheme.lower(),
        netloc=parts.netloc.lower(),
        query="",
        fragment="",
    )
    normalized = urlunsplit(cleaned)
    return normalized[:-1] if normalized.endswith("/") else normalized


def build_news_hash(url: str, title: str) -> str:
    normalized_title = re.sub(r"\s+", " ", title).strip().casefold()
    digest = hashlib.sha256(f"{normalize_url(url)}::{normalized_title}".encode("utf-8"))
    return digest.hexdigest()


def parse_json_payload(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


class TransferPipeline:
    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        extraction_api_key: str,
        extraction_model: str,
        generator: PostGenerator,
        notifier: Notifier,
    ) -> None:
        self.database = database
        if AsyncAnthropic is None:
            raise RuntimeError("anthropic package is required to extract transfer entities")
        self.settings = settings
        self.extractor = AsyncAnthropic(api_key=extraction_api_key)
        self.extraction_model = extraction_model
        self.generator = generator
        self.notifier = notifier
        self._local_tz = ZoneInfo(settings.bot_timezone)

    async def process_batch(self, items: list[RawNews], *, deliver_notifications: bool = True) -> int:
        processed = 0
        for item in items:
            try:
                created = await self.process_news(item, deliver_notifications=deliver_notifications)
            except Exception:
                LOGGER.exception("Failed to process news from %s: %s", item.source_name, item.url)
                continue
            if created:
                processed += 1
        return processed

    async def process_news(self, item: RawNews, *, deliver_notifications: bool = True) -> bool:
        if not self._should_process_item(item):
            return False
        news_hash = build_news_hash(item.url, item.title)
        if await self.database.has_news_hash(news_hash):
            return False

        extracted = await self.extract_transfer(item)
        if not extracted.is_transfer_news:
            await self.database.mark_filtered_news(
                news_hash=news_hash,
                source_name=item.source_name,
                source_tier=item.source_tier,
                url=item.url,
                title=item.title,
                raw_text=item.raw_text,
                published_at=self._published_at(item.published_at),
                news_type="filtered_out",
            )
            return True

        extracted.player_slug = await self.resolve_player_slug(
            player=extracted.player,
            player_slug=extracted.player_slug,
            from_club=extracted.from_club,
            to_club=extracted.to_club,
        )

        stars, reasons = calculate_reliability(item.source_tier, extracted.type)
        cluster = await self.database.find_recent_cluster(
            player_slugs=slug_aliases(extracted.player_slug),
            to_club=extracted.to_club,
            hours=48,
        )
        if cluster is not None:
            merged_sources = merge_source_names(cluster.source_names_json, item.source_name)
            mention_count = max(int(cluster.mention_count or 1) + 1, len(merged_sources))
            previous_stars = int(cluster.stars or 1)
            cluster_source_tier = cluster.source_tier or "rumor"
            is_upgrade = self._is_higher_tier(item.source_tier, cluster_source_tier)
            cluster_update: dict[str, Any] = {
                "mention_count": mention_count,
                "source_names_json": json.dumps(merged_sources, ensure_ascii=False),
                "last_seen_at": self._published_at(item.published_at) or utcnow_iso(),
                "stars": max(previous_stars, stars),
            }
            await self.database.update_news(cluster.id, cluster_update)

            if deliver_notifications and is_upgrade:
                upgrade_news_id = await self.database.insert_news(
                    {
                        "hash": news_hash,
                        "source_name": item.source_name,
                        "source_tier": item.source_tier,
                        "url": item.url,
                        "title": item.title,
                        "raw_text": item.raw_text,
                        "player_name": extracted.player,
                        "player_slug": extracted.player_slug,
                        "from_club": extracted.from_club,
                        "to_club": extracted.to_club,
                        "fee": extracted.fee,
                        "news_type": extracted.type,
                        "stars": stars,
                        "mention_count": mention_count,
                        "source_names_json": json.dumps(merged_sources, ensure_ascii=False),
                        "last_seen_at": self._published_at(item.published_at) or utcnow_iso(),
                        "published_at": self._published_at(item.published_at),
                    }
                )
                if upgrade_news_id == 0:
                    LOGGER.warning("Upgrade notification skipped because news hash already exists: %s", item.url)
                    return True
                await self.notifier(
                    NotificationNews(
                        news_id=upgrade_news_id,
                        raw=item,
                        extracted=extracted,
                        stars=stars,
                        reasons=reasons,
                        is_upgrade=True,
                        previous_stars=previous_stars,
                        mention_count=mention_count,
                        source_names=merged_sources,
                    )
                )
            return True

        news_id = await self.database.insert_news(
            {
                "hash": news_hash,
                "source_name": item.source_name,
                "source_tier": item.source_tier,
                "url": item.url,
                "title": item.title,
                "raw_text": item.raw_text,
                "player_name": extracted.player,
                "player_slug": extracted.player_slug,
                "from_club": extracted.from_club,
                "to_club": extracted.to_club,
                "fee": extracted.fee,
                "news_type": extracted.type,
                "stars": stars,
                "mention_count": 1,
                "source_names_json": json.dumps([item.source_name], ensure_ascii=False),
                "last_seen_at": self._published_at(item.published_at) or utcnow_iso(),
                "published_at": self._published_at(item.published_at),
            }
        )
        if news_id == 0:
            return False

        if deliver_notifications:
            await self.notifier(
                NotificationNews(
                    news_id=news_id,
                    raw=item,
                    extracted=extracted,
                    stars=stars,
                    reasons=reasons,
                    mention_count=1,
                    source_names=[item.source_name],
                )
            )
        return True

    async def extract_transfer(self, item: RawNews) -> ExtractedNews:
        prompt = f"""
Извлеки из новости только данные о трансфере футболиста.
Верни строго JSON без пояснений:
{{"player":"Флориан Виртц","player_slug":"florian-wirtz","from_club":"Байер","to_club":"Ливерпуль","fee":"130 млн евро","type":"medical","is_transfer_news":true}}

type должен быть одним из:
official, here_we_go, medical, talks, bid, interest, rumor, denial, renewal, loan

Если это не трансферная новость про игрока, верни:
{{"player":"","player_slug":"","from_club":"","to_club":"","fee":"","type":"rumor","is_transfer_news":false}}

SOURCE: {item.source_name}
TITLE: {item.title}
TEXT: {item.raw_text}
""".strip()
        delay = 1.0
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self.extractor.messages.create(
                    model=self.extraction_model,
                    max_tokens=300,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
                payload = parse_json_payload(text)
                news_type = str(payload.get("type", "rumor")).strip()
                if news_type not in TRANSFER_TYPES:
                    news_type = "rumor"
                player = str(payload.get("player", "")).strip()
                player_slug = str(payload.get("player_slug", "")).strip() or slugify(player)
                return ExtractedNews(
                    player=player or "Неизвестный игрок",
                    player_slug=player_slug,
                    from_club=self._none_if_empty(payload.get("from_club")),
                    to_club=self._none_if_empty(payload.get("to_club")),
                    fee=self._none_if_empty(payload.get("fee")),
                    type=news_type,
                    is_transfer_news=bool(payload.get("is_transfer_news", False)),
                )
            except Exception as exc:
                last_error = exc
                if attempt == 2:
                    break
                await asyncio.sleep(delay)
                delay *= 2
        raise RuntimeError(f"transfer extraction failed: {last_error}")

    async def generate_short_post_for_news(self, news_id: int) -> DraftPost:
        news = await self.require_news(news_id)
        stars = int(news.stars or 1)
        result = await self.generator.generate_news(
            source_name=news.source_name or "Unknown source",
            source_url=news.url or "",
            source_tier=news.source_tier or "rumor",
            title=news.title or "",
            raw_text=news.raw_text or "",
            player_name=news.player_name or "Неизвестный игрок",
            from_club=news.from_club,
            to_club=news.to_club,
            fee=news.fee,
            news_type=news.news_type or "rumor",
            stars=stars,
        )
        post_id = await self.database.save_post(
            news_id=news.id,
            content_json={
                "news": result.news,
                "comment": None,
                "text": result.text,
                "has_comment": False,
            },
            insert_used="",
        )
        if result.problems:
            LOGGER.warning("Post validation fallback for news #%s: %s", news.id, ", ".join(result.problems))
        return DraftPost(
            post_id=post_id,
            news_id=news.id,
            text=result.text,
            has_comment=False,
            is_published=False,
        )

    async def add_comment_to_post(self, post_id: int) -> DraftPost:
        post = await self.require_post(post_id)
        payload = self._post_payload(post)
        if payload.get("has_comment"):
            return DraftPost(
                post_id=post.id,
                news_id=post.news_id,
                text=str(payload.get("text") or ""),
                has_comment=True,
                is_published=bool(post.published_at),
            )

        news = await self.require_news(post.news_id)
        recent_inserts = await self.database.recent_inserts(5, exclude_news_id=news.id)
        stars = int(news.stars or 1)
        reasons = calculate_reliability(news.source_tier or "rumor", news.news_type or "rumor")[1]
        result = await self.generator.generate_comment(
            source_name=news.source_name or "Unknown source",
            source_url=news.url or "",
            source_tier=news.source_tier or "rumor",
            title=news.title or "",
            raw_text=news.raw_text or "",
            player_name=news.player_name or "Неизвестный игрок",
            from_club=news.from_club,
            to_club=news.to_club,
            fee=news.fee,
            news_type=news.news_type or "rumor",
            stars=stars,
            reasons=reasons,
            news=str(payload.get("news") or ""),
            recent_inserts=recent_inserts,
        )
        updated_payload = {
            "news": payload.get("news") or "",
            "comment": result.comment,
            "text": result.text,
            "has_comment": True,
        }
        await self.database.update_post(post.id, content_json=updated_payload, insert_used=result.insert_used)
        if result.problems:
            LOGGER.warning("Comment validation fallback for post #%s: %s", post.id, ", ".join(result.problems))
        return DraftPost(
            post_id=post.id,
            news_id=post.news_id,
            text=result.text,
            has_comment=True,
            is_published=bool(post.published_at),
        )

    async def get_draft_post(self, post_id: int) -> DraftPost:
        post = await self.require_post(post_id)
        payload = self._post_payload(post)
        return DraftPost(
            post_id=post.id,
            news_id=post.news_id,
            text=str(payload.get("text") or ""),
            has_comment=bool(payload.get("has_comment")),
            is_published=bool(post.published_at),
        )

    async def require_news(self, news_id: int) -> NewsRow:
        news = await self.database.get_news(news_id)
        if news is None:
            raise LookupError(f"news #{news_id} not found")
        return news

    async def require_post(self, post_id: int) -> PostRow:
        post = await self.database.get_post(post_id)
        if post is None:
            raise LookupError(f"post #{post_id} not found")
        return post

    @staticmethod
    def _post_payload(post: PostRow) -> dict[str, Any]:
        try:
            payload = json.loads(post.content_json or "{}")
        except Exception:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _none_if_empty(value: Any) -> str | None:
        text = str(value).strip()
        return text or None

    @staticmethod
    def _published_at(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(timezone.utc).isoformat()

    def _should_process_item(self, item: RawNews) -> bool:
        if not self.settings.only_today_news:
            return True
        if item.published_at is None:
            return False
        return item.published_at.astimezone(self._local_tz).date() == datetime.now(self._local_tz).date()

    async def resolve_player_slug(
        self,
        *,
        player: str,
        player_slug: str,
        from_club: str | None,
        to_club: str | None,
    ) -> str:
        candidate = slugify(player_slug or player)
        aliases = slug_aliases(candidate)
        rows = await self.database.recent_player_rows(limit=500, days=180)
        current_from = normalize_club_name(from_club)
        current_to = normalize_club_name(to_club)

        for row in rows:
            existing_slug = row.player_slug or ""
            if not existing_slug or existing_slug == candidate:
                continue
            existing_aliases = slug_aliases(existing_slug)
            alias_overlap = set(aliases) & set(existing_aliases)
            if not alias_overlap:
                continue
            if not self._clubs_overlap(
                current_from=current_from,
                current_to=current_to,
                row_from=normalize_club_name(row.from_club),
                row_to=normalize_club_name(row.to_club),
            ):
                continue
            if len(existing_slug) > len(candidate):
                return existing_slug
        return candidate

    @staticmethod
    def _clubs_overlap(
        *,
        current_from: str,
        current_to: str,
        row_from: str,
        row_to: str,
    ) -> bool:
        current = {club for club in (current_from, current_to) if club}
        row = {club for club in (row_from, row_to) if club}
        if not current or not row:
            return False
        return not current.isdisjoint(row)

    @staticmethod
    def _is_higher_tier(candidate_tier: str, current_tier: str) -> bool:
        return BASE_STARS.get(candidate_tier, 1) > BASE_STARS.get(current_tier, 1)
