from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class NewsRow:
    id: int
    hash: str
    source_name: str | None
    source_tier: str | None
    url: str | None
    title: str | None
    raw_text: str | None
    player_name: str | None
    player_slug: str | None
    from_club: str | None
    to_club: str | None
    fee: str | None
    news_type: str | None
    stars: int | None
    mention_count: int | None
    source_names_json: str | None
    last_seen_at: str | None
    published_at: str | None
    created_at: str | None


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    async def connect(self) -> aiosqlite.Connection:
        database = await aiosqlite.connect(self.path)
        database.row_factory = aiosqlite.Row
        await database.execute("PRAGMA journal_mode=WAL;")
        await database.execute("PRAGMA foreign_keys=ON;")
        return database

    @asynccontextmanager
    async def connection(self) -> Any:
        db = await self.connect()
        try:
            yield db
        finally:
            await db.close()

    async def init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with self.connection() as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS news (
                    id INTEGER PRIMARY KEY,
                    hash TEXT UNIQUE NOT NULL,
                    source_name TEXT,
                    source_tier TEXT,
                    url TEXT,
                    title TEXT,
                    raw_text TEXT,
                    player_name TEXT,
                    player_slug TEXT,
                    from_club TEXT,
                    to_club TEXT,
                    fee TEXT,
                    news_type TEXT,
                    stars INTEGER,
                    mention_count INTEGER DEFAULT 1,
                    source_names_json TEXT DEFAULT '[]',
                    last_seen_at TIMESTAMP,
                    published_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY,
                    news_id INTEGER REFERENCES news(id),
                    content_json TEXT,
                    insert_used TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_news_player ON news(player_slug);
                """
            )
            await self._ensure_news_column(db, "mention_count", "INTEGER DEFAULT 1")
            await self._ensure_news_column(db, "source_names_json", "TEXT DEFAULT '[]'")
            await self._ensure_news_column(db, "last_seen_at", "TIMESTAMP")
            await db.commit()

    async def _ensure_news_column(self, db: aiosqlite.Connection, name: str, definition: str) -> None:
        async with db.execute("PRAGMA table_info(news)") as cursor:
            rows = await cursor.fetchall()
        existing = {row["name"] for row in rows}
        if name not in existing:
            await db.execute(f"ALTER TABLE news ADD COLUMN {name} {definition}")

    async def has_news_hash(self, news_hash: str) -> bool:
        async with self.connection() as db:
            async with db.execute("SELECT 1 FROM news WHERE hash = ? LIMIT 1", (news_hash,)) as cursor:
                return await cursor.fetchone() is not None

    async def insert_news(self, payload: dict[str, Any]) -> int:
        columns = ", ".join(payload.keys())
        placeholders = ", ".join("?" for _ in payload)
        values = tuple(payload.values())
        async with self.connection() as db:
            cursor = await db.execute(
                f"INSERT OR IGNORE INTO news ({columns}) VALUES ({placeholders})",
                values,
            )
            if cursor.lastrowid == 0:
                await db.commit()
                return 0
            await db.commit()
            return int(cursor.lastrowid)

    async def get_news(self, news_id: int) -> NewsRow | None:
        async with self.connection() as db:
            async with db.execute("SELECT * FROM news WHERE id = ?", (news_id,)) as cursor:
                row = await cursor.fetchone()
        return NewsRow(**dict(row)) if row else None

    async def latest_news(self, limit: int = 10) -> list[NewsRow]:
        async with self.connection() as db:
            async with db.execute(
                """
                SELECT *
                FROM news
                ORDER BY datetime(coalesce(last_seen_at, published_at, created_at)) DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [NewsRow(**dict(row)) for row in rows]

    async def find_recent_cluster(
        self,
        *,
        player_slugs: list[str],
        to_club: str | None,
        hours: int = 48,
    ) -> NewsRow | None:
        clean_slugs = [slug for slug in dict.fromkeys(player_slugs) if slug]
        if not clean_slugs:
            return None
        placeholders = ", ".join("?" for _ in clean_slugs)
        params = (
            *clean_slugs,
            (to_club or "").strip().casefold(),
            f"-{hours} hours",
        )
        async with self.connection() as db:
            async with db.execute(
                f"""
                SELECT *
                FROM news
                WHERE player_slug IN ({placeholders})
                  AND lower(trim(coalesce(to_club, ''))) = ?
                  AND datetime(coalesce(last_seen_at, published_at, created_at)) >= datetime('now', ?)
                  AND news_type != 'filtered_out'
                ORDER BY datetime(coalesce(last_seen_at, published_at, created_at)) DESC, id DESC
                LIMIT 1
                """,
                params,
            ) as cursor:
                row = await cursor.fetchone()
        return NewsRow(**dict(row)) if row else None

    async def update_news(self, news_id: int, payload: dict[str, Any]) -> None:
        if not payload:
            return
        assignments = ", ".join(f"{column} = ?" for column in payload)
        values = (*payload.values(), news_id)
        async with self.connection() as db:
            await db.execute(f"UPDATE news SET {assignments} WHERE id = ?", values)
            await db.commit()

    async def recent_player_rows(self, limit: int = 500, days: int = 180) -> list[NewsRow]:
        async with self.connection() as db:
            async with db.execute(
                """
                SELECT *
                FROM news
                WHERE player_slug IS NOT NULL
                  AND player_slug != ''
                  AND datetime(coalesce(last_seen_at, published_at, created_at)) >= datetime('now', ?)
                ORDER BY datetime(coalesce(last_seen_at, published_at, created_at)) DESC, id DESC
                LIMIT ?
                """,
                (f"-{days} days", limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [NewsRow(**dict(row)) for row in rows]

    async def recent_news_by_tiers(self, tiers: list[str], limit: int = 1000) -> list[NewsRow]:
        if not tiers:
            return []
        placeholders = ", ".join("?" for _ in tiers)
        params = (*tiers, limit)
        async with self.connection() as db:
            async with db.execute(
                f"""
                SELECT *
                FROM news
                WHERE source_tier IN ({placeholders})
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            ) as cursor:
                rows = await cursor.fetchall()
        return [NewsRow(**dict(row)) for row in rows]

    async def save_post(self, news_id: int, content_json: dict[str, Any], insert_used: str) -> int:
        async with self.connection() as db:
            cursor = await db.execute(
                "INSERT INTO posts (news_id, content_json, insert_used) VALUES (?, ?, ?)",
                (news_id, json.dumps(content_json, ensure_ascii=False), insert_used),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def recent_inserts(self, limit: int = 5, *, exclude_news_id: int | None = None) -> list[str]:
        where_clause = "WHERE insert_used IS NOT NULL AND insert_used != ''"
        params: list[Any] = []
        if exclude_news_id is not None:
            where_clause += " AND news_id != ?"
            params.append(exclude_news_id)
        params.append(limit)
        async with self.connection() as db:
            async with db.execute(
                f"""
                SELECT insert_used
                FROM posts
                {where_clause}
                ORDER BY id DESC
                LIMIT ?
                """,
                tuple(params),
            ) as cursor:
                rows = await cursor.fetchall()
        ignored = {"", "none", "null", "нет"}
        return [
            row["insert_used"]
            for row in rows
            if str(row["insert_used"]).strip().casefold() not in ignored
        ]

    async def last_posts(self, limit: int = 5) -> list[aiosqlite.Row]:
        async with self.connection() as db:
            async with db.execute(
                """
                SELECT posts.id, posts.news_id, posts.insert_used, posts.content_json, posts.created_at
                FROM posts
                ORDER BY posts.id DESC
                LIMIT ?
                """,
                (limit,),
            ) as cursor:
                return await cursor.fetchall()

    async def get_post_count(self) -> int:
        async with self.connection() as db:
            async with db.execute("SELECT COUNT(*) AS count FROM posts") as cursor:
                row = await cursor.fetchone()
        return int(row["count"])

    async def mark_filtered_news(
        self,
        *,
        news_hash: str,
        source_name: str,
        source_tier: str,
        url: str,
        title: str,
        raw_text: str,
        published_at: str | None,
        player_name: str | None = None,
        player_slug: str | None = None,
        from_club: str | None = None,
        to_club: str | None = None,
        fee: str | None = None,
        news_type: str | None = None,
        stars: int | None = None,
    ) -> int:
        return await self.insert_news(
            {
                "hash": news_hash,
                "source_name": source_name,
                "source_tier": source_tier,
                "url": url,
                "title": title,
                "raw_text": raw_text,
                "player_name": player_name,
                "player_slug": player_slug,
                "from_club": from_club,
                "to_club": to_club,
                "fee": fee,
                "news_type": news_type,
                "stars": stars,
                "published_at": published_at,
            }
        )
