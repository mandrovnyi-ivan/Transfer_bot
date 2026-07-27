from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parent


@dataclass(slots=True)
class XAccount:
    username: str
    tier: str
    lang: str = "en"
    scope: str = "global"
    independent: bool = True
    enabled: bool = True


@dataclass(slots=True)
class RSSFeed:
    name: str
    url: str
    tier: str
    lang: str = "en"
    independent: bool = True
    enabled: bool = True


@dataclass(slots=True)
class SourcesConfig:
    x_accounts: list[XAccount]
    rss_feeds: list[RSSFeed]
    nitter_instances: list[str]
    transfer_markers: list[str]
    target_leagues: list[str]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    owner_id: int = Field(alias="OWNER_ID")
    allowed_users_raw: str = Field(default="", alias="ALLOWED_USERS")
    anthropic_api_key: str = Field(alias="ANTHROPIC_API_KEY")
    twitter_auth_token: str = Field(default="", alias="TWITTER_AUTH_TOKEN")
    rsshub_url: str = Field(default="http://rsshub:1200", alias="RSSHUB_URL")
    database_url: str = Field(default="./data/bot.db", alias="DATABASE_URL")
    x_poll_interval_seconds: int = Field(default=60, alias="X_POLL_INTERVAL_SECONDS")
    rss_poll_interval_minutes: int = Field(default=3, alias="RSS_POLL_INTERVAL_MINUTES")
    generation_model: str = Field(default="claude-sonnet-4-6", alias="GENERATION_MODEL")
    extraction_model: str = Field(default="claude-haiku-4-5", alias="EXTRACTION_MODEL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    bot_timezone: str = Field(default="Europe/Prague", alias="BOT_TIMEZONE")
    only_today_news: bool = Field(default=True, alias="ONLY_TODAY_NEWS")
    target_channel_id: str = Field(default="", alias="TARGET_CHANNEL_ID")

    @property
    def database_path(self) -> Path:
        path = Path(self.database_url)
        return path if path.is_absolute() else (ROOT_DIR / path).resolve()

    @property
    def config_path(self) -> Path:
        return ROOT_DIR / "config.yaml"

    @property
    def allowed_user_ids(self) -> list[int]:
        if self.allowed_users_raw.strip():
            values = []
            for chunk in self.allowed_users_raw.split(","):
                text = chunk.strip()
                if not text:
                    continue
                values.append(int(text))
            unique: list[int] = []
            for user_id in values:
                if user_id not in unique:
                    unique.append(user_id)
            if unique:
                return unique
        return [self.owner_id]


def load_sources_config(path: Path | None = None) -> SourcesConfig:
    config_path = path or (ROOT_DIR / "config.yaml")
    with config_path.open("r", encoding="utf-8") as file:
        payload: dict[str, Any] = yaml.safe_load(file) or {}
    raw_markers = payload.get("transfer_markers", {})
    transfer_markers: list[str] = []
    if isinstance(raw_markers, dict):
        seen: set[str] = set()
        for values in raw_markers.values():
            for item in values or []:
                marker = str(item).strip().casefold()
                if marker and marker not in seen:
                    seen.add(marker)
                    transfer_markers.append(marker)
    else:
        transfer_markers = [str(item).strip().casefold() for item in raw_markers if str(item).strip()]
    return SourcesConfig(
        x_accounts=[XAccount(**item) for item in payload.get("x_accounts", [])],
        rss_feeds=[RSSFeed(**item) for item in payload.get("rss_feeds", [])],
        nitter_instances=[item.strip() for item in payload.get("nitter_instances", []) if item.strip()],
        transfer_markers=transfer_markers,
        target_leagues=[str(item).strip() for item in payload.get("target_leagues", []) if str(item).strip()],
    )
