from __future__ import annotations

import asyncio
import logging
import re
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from time import mktime
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import aiohttp
import certifi
import feedparser

from settings import RSSFeed, Settings, SourcesConfig, XAccount


LOGGER = logging.getLogger(__name__)
REPLY_PATTERNS = (" replying to @", " in reply to @", "r to @")


@dataclass(slots=True)
class RawNews:
    source_name: str
    source_tier: str
    url: str
    title: str
    raw_text: str
    published_at: datetime | None
    source_kind: str


@dataclass(slots=True)
class SourceStatus:
    key: str
    display_name: str
    source_kind: str
    enabled: bool = True
    consecutive_failures: int = 0
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None


@dataclass(slots=True)
class BackendCheckResult:
    ok: bool
    error: str | None = None


async def request_with_backoff(
    factory: Callable[[], Awaitable[aiohttp.ClientResponse]],
    *,
    retries: int = 3,
    base_delay: float = 1.0,
) -> aiohttp.ClientResponse:
    delay = base_delay
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = await factory()
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            if attempt == retries - 1:
                break
            await asyncio.sleep(delay)
            delay *= 2
    raise RuntimeError(str(last_error) if last_error else "request failed")


def parse_entry_datetime(entry: dict[str, Any]) -> datetime | None:
    if entry.get("published_parsed"):
        return datetime.fromtimestamp(mktime(entry["published_parsed"]), tz=timezone.utc)
    if entry.get("updated_parsed"):
        return datetime.fromtimestamp(mktime(entry["updated_parsed"]), tz=timezone.utc)
    for field_name in ("published", "updated"):
        if entry.get(field_name):
            try:
                return parsedate_to_datetime(entry[field_name]).astimezone(timezone.utc)
            except Exception:
                continue
    return None


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


def shorten_text(text: str, limit: int) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


class SourcesClient:
    def __init__(self, settings: Settings, config: SourcesConfig) -> None:
        self.settings = settings
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._transfer_markers = tuple(config.transfer_markers)
        self._local_tz = ZoneInfo(settings.bot_timezone)
        self._x_backend_cache: BackendCheckResult | None = None
        self._x_backend_cache_until: datetime | None = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def build_statuses(self) -> dict[str, SourceStatus]:
        statuses: dict[str, SourceStatus] = {}
        for account in self.config.x_accounts:
            if not account.enabled:
                continue
            key = self._source_key("x", account.username)
            statuses[key] = SourceStatus(key=key, display_name=f"X: @{account.username}", source_kind="x")
        for feed in self.config.rss_feeds:
            if not feed.enabled:
                continue
            key = self._source_key("rss", feed.name)
            statuses[key] = SourceStatus(key=key, display_name=f"RSS: {feed.name}", source_kind="rss")
        return statuses

    async def check_x_backend(self) -> BackendCheckResult:
        now = datetime.now(timezone.utc)
        if self._x_backend_cache and self._x_backend_cache_until and now < self._x_backend_cache_until:
            return self._x_backend_cache

        session = await self.session()
        errors: list[str] = []
        sample_username = self.config.x_accounts[0].username if self.config.x_accounts else "FabrizioRomano"

        if self.settings.twitter_auth_token:
            try:
                response = await request_with_backoff(
                    lambda: session.get(
                        f"{self.settings.rsshub_url.rstrip('/')}/twitter/user/{quote(sample_username)}",
                        headers={"Cookie": f"auth_token={self.settings.twitter_auth_token}"},
                    ),
                    retries=1,
                )
                async with response:
                    await response.read()
                result = BackendCheckResult(ok=True)
                self._x_backend_cache = result
                self._x_backend_cache_until = now + timedelta(minutes=10)
                return result
            except Exception as exc:
                errors.append(f"RSSHub: {exc}")

        for instance in self.config.nitter_instances:
            try:
                response = await request_with_backoff(
                    lambda: session.get(f"{instance.rstrip('/')}/{quote(sample_username)}/rss"),
                    retries=1,
                )
                async with response:
                    await response.read()
                result = BackendCheckResult(ok=True)
                self._x_backend_cache = result
                self._x_backend_cache_until = now + timedelta(minutes=10)
                return result
            except Exception as exc:
                errors.append(f"{instance}: {exc}")

        result = BackendCheckResult(ok=False, error="; ".join(errors) if errors else "No X backend available")
        self._x_backend_cache = result
        self._x_backend_cache_until = now + timedelta(minutes=3)
        return result

    async def fetch_x_news(self, account: XAccount) -> list[RawNews]:
        session = await self.session()
        errors: list[str] = []
        rsshub_url = f"{self.settings.rsshub_url.rstrip('/')}/twitter/user/{quote(account.username)}"
        if self.settings.twitter_auth_token:
            try:
                response = await request_with_backoff(
                    lambda: session.get(
                        rsshub_url,
                        headers={"Cookie": f"auth_token={self.settings.twitter_auth_token}"},
                    )
                )
                async with response:
                    payload = await response.read()
                return self._parse_x_feed_bytes(payload, account)
            except Exception as exc:
                errors.append(f"RSSHub: {exc}")
                LOGGER.warning("RSSHub failed for @%s: %s", account.username, exc)

        for instance in self.config.nitter_instances:
            try:
                response = await request_with_backoff(
                    lambda: session.get(f"{instance.rstrip('/')}/{quote(account.username)}/rss")
                )
                async with response:
                    payload = await response.read()
                return self._parse_x_feed_bytes(payload, account)
            except Exception as exc:
                errors.append(f"{instance}: {exc}")
                LOGGER.warning("Nitter failed for @%s on %s: %s", account.username, instance, exc)

        raise RuntimeError("; ".join(errors) if errors else f"no X source available for @{account.username}")

    async def fetch_rss_news(self, feed: RSSFeed) -> list[RawNews]:
        session = await self.session()
        response = await request_with_backoff(lambda: session.get(feed.url))
        async with response:
            payload = await response.read()
        parsed = feedparser.parse(payload)
        items: list[RawNews] = []
        for entry in parsed.entries:
            title = unescape((entry.get("title") or "").strip())
            summary = unescape((entry.get("summary") or entry.get("description") or title).strip())
            url = normalize_url(entry.get("link") or "")
            published_at = parse_entry_datetime(entry)
            if not title or not url:
                continue
            if not self._should_keep_item(published_at):
                continue
            items.append(
                RawNews(
                    source_name=feed.name,
                    source_tier=feed.tier,
                    url=url,
                    title=title,
                    raw_text=shorten_text(summary, 1200),
                    published_at=published_at,
                    source_kind="rss",
                )
            )
        return items

    def _parse_x_feed_bytes(self, payload: bytes, account: XAccount) -> list[RawNews]:
        parsed = feedparser.parse(payload)
        items: list[RawNews] = []
        for entry in parsed.entries:
            title = unescape((entry.get("title") or "").strip())
            summary = unescape((entry.get("summary") or entry.get("description") or title).strip())
            text = f"{title} {summary}".strip()
            published_at = parse_entry_datetime(entry)
            if not title or not entry.get("link"):
                continue
            if self._is_retweet_or_reply(text):
                continue
            if not self._has_transfer_marker(text):
                continue
            if not self._should_keep_item(published_at):
                continue
            items.append(
                RawNews(
                    source_name=account.username,
                    source_tier=account.tier,
                    url=normalize_url(entry["link"]),
                    title=shorten_text(title, 280),
                    raw_text=shorten_text(summary or title, 1200),
                    published_at=published_at,
                    source_kind="x",
                )
            )
        return items

    def _has_transfer_marker(self, text: str) -> bool:
        normalized = text.casefold()
        return any(marker in normalized for marker in self._transfer_markers)

    def _should_keep_item(self, published_at: datetime | None) -> bool:
        if not self.settings.only_today_news:
            return True
        if published_at is None:
            return False
        return published_at.astimezone(self._local_tz).date() == datetime.now(self._local_tz).date()

    @staticmethod
    def _is_retweet_or_reply(text: str) -> bool:
        normalized = text.casefold()
        if normalized.startswith("rt @"):
            return True
        return any(pattern in normalized for pattern in REPLY_PATTERNS)

    @staticmethod
    def _source_key(source_kind: str, name: str) -> str:
        return f"{source_kind}:{name.casefold()}"


def format_source_status(statuses: dict[str, SourceStatus]) -> str:
    lines = []
    for key in sorted(statuses):
        status = statuses[key]
        if not status.enabled:
            state = "отключён"
        elif status.last_error and status.consecutive_failures:
            state = "ошибка"
        else:
            state = "работает"
        success = status.last_success_at.isoformat(timespec="seconds") if status.last_success_at else "—"
        error = status.last_error or "—"
        lines.append(
            f"{status.display_name}\n"
            f"Статус: {state}\n"
            f"Последний успех: {success}\n"
            f"Ошибки подряд: {status.consecutive_failures}\n"
            f"Последняя ошибка: {error}"
        )
    return "\n\n".join(lines) if lines else "Источники ещё не инициализированы."
