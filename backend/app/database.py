"""SQLite storage: users, api_keys (pool), jobs, images, usage_daily.

Старая таблица generations (видео) удалена из схемы; при наличии старой БД
она остаётся как есть (миграции только добавляют новые таблицы/колонки).
Миграции колонок — «на лету» через PRAGMA table_info, как в оригинале.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiosqlite

from app.config import settings


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Database:
    def __init__(self, db_path: str = settings.DATABASE_PATH):
        self.db_path = db_path
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            # users
            try:
                await db.execute("ALTER TABLE users ADD COLUMN is_allowed INTEGER NOT NULL DEFAULT 0")
                await db.commit()
            except Exception:
                pass
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    api_key TEXT,
                    tier TEXT DEFAULT 'free',
                    is_allowed INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            # api_keys (пул ключей юзера; общий ключ сюда не пишется — он в .env)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    label TEXT,
                    api_key TEXT NOT NULL,
                    status TEXT DEFAULT 'ok',
                    cooldown_until TIMESTAMP,
                    last_four TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )

            # jobs
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    prompt TEXT,
                    size TEXT,
                    quality TEXT,
                    n_requested INTEGER,
                    n_done INTEGER DEFAULT 0,
                    n_failed INTEGER DEFAULT 0,
                    references_count INTEGER DEFAULT 0,
                    seed INTEGER,
                    estimate_total REAL,
                    cost_real REAL,
                    usage_total_tokens INTEGER DEFAULT 0,
                    used_shared_key INTEGER DEFAULT 0,
                    error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )

            # images (результаты)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS images (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    local_path TEXT,
                    width INTEGER,
                    height INTEGER,
                    size_bytes INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (job_id) REFERENCES jobs(id)
                )
                """
            )

            # usage_daily — per-user дневной лимит на общем ключе
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_daily (
                    user_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    count INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, day),
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )

            # reference_assets (переиспользуется на Этапе 4)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS reference_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    content_type TEXT,
                    path TEXT NOT NULL,
                    remote_url TEXT,
                    size INTEGER DEFAULT 0,
                    sha256 TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )
            # Миграции колонок — ПОСЛЕ создания всех таблиц.
            await self._ensure_user_columns(db)
            await self._ensure_job_columns(db)
            await db.commit()

    async def _ensure_user_columns(self, db: aiosqlite.Connection):
        async with db.execute("PRAGMA table_info(users)") as cursor:
            existing = {row[1] for row in await cursor.fetchall()}
        for name, definition in {"tier": "TEXT DEFAULT 'free'", "is_allowed": "INTEGER NOT NULL DEFAULT 0"}.items():
            if name not in existing:
                await db.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")

    async def _ensure_job_columns(self, db: aiosqlite.Connection):
        """Миграции колонок jobs для существующих БД."""
        async with db.execute("PRAGMA table_info(jobs)") as cursor:
            existing = {row[1] for row in await cursor.fetchall()}
        for name, definition in {
            "n_failed": "INTEGER DEFAULT 0",
            "cost_real": "REAL",
            "references_count": "INTEGER DEFAULT 0",
        }.items():
            if name not in existing:
                await db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")

    # ---------------- users ----------------

    async def get_user(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def create_user(self, telegram_id: int, username: Optional[str], full_name: Optional[str]):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (telegram_id, username, full_name, tier) VALUES (?, ?, ?, ?)",
                (telegram_id, username, full_name, settings.DEFAULT_TIER),
            )
            await db.commit()

    async def set_user_api_key(self, telegram_id: int, api_key: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE users SET api_key = ? WHERE telegram_id = ?", (api_key, telegram_id))
            await db.commit()

    async def set_user_tier(self, telegram_id: int, tier: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE users SET tier = ? WHERE telegram_id = ?", (tier, telegram_id))
            await db.commit()

    async def set_user_allowed(self, telegram_id: int, allowed: bool):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE users SET is_allowed = ? WHERE telegram_id = ?",
                (1 if allowed else 0, telegram_id),
            )
            await db.commit()

    async def is_user_allowed(self, telegram_id: int) -> bool:
        user = await self.get_user(telegram_id)
        if not user:
            return False
        return bool(user.get("is_allowed"))

    async def get_all_users(self):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users ORDER BY created_at DESC") as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    # ---------------- usage / daily limit ----------------

    async def today_usage(self, user_db_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT count FROM usage_daily WHERE user_id = ? AND day = ?",
                (user_db_id, _utc_date()),
            ) as cursor:
                row = await cursor.fetchone()
                return int(row[0]) if row else 0

    async def add_usage(self, user_db_id: int, count: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO usage_daily (user_id, day, count) VALUES (?, ?, ?)
                ON CONFLICT(user_id, day) DO UPDATE SET count = count + excluded.count
                """,
                (user_db_id, _utc_date(), count),
            )
            await db.commit()

    # ---------------- jobs ----------------

    async def create_job(
        self,
        *,
        user_db_id: int,
        prompt: str,
        size: str,
        quality: str,
        n: int,
        seed: Optional[int],
        estimate_total: float,
        used_shared_key: bool,
        references_count: int = 0,
    ) -> str:
        job_id = uuid.uuid4().hex
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO jobs (id, user_id, status, prompt, size, quality, n_requested,
                                  n_done, seed, estimate_total, used_shared_key, references_count)
                VALUES (?, ?, 'queued', ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (job_id, user_db_id, prompt, size, quality, n, seed, estimate_total, 1 if used_shared_key else 0, references_count),
            )
            await db.commit()
        return job_id

    async def update_job_status(
        self,
        job_id: str,
        status: str,
        *,
        n_done: Optional[int] = None,
        n_failed: Optional[int] = None,
        usage_total_tokens: Optional[int] = None,
        cost_real: Optional[float] = None,
        error: Optional[str] = None,
    ):
        fields = ["status = ?"]
        values: List[Any] = [status]
        if n_done is not None:
            fields.append("n_done = ?")
            values.append(n_done)
        if n_failed is not None:
            fields.append("n_failed = ?")
            values.append(n_failed)
        if usage_total_tokens is not None:
            fields.append("usage_total_tokens = ?")
            values.append(usage_total_tokens)
        if cost_real is not None:
            fields.append("cost_real = ?")
            values.append(cost_real)
        if error is not None:
            fields.append("error = ?")
            values.append(error)
        if status in {"done", "failed", "partial", "cancelled"}:
            fields.append("completed_at = ?")
            values.append(_now_iso())
        values.append(job_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values)
            await db.commit()

    async def add_job_usage(self, job_id: str, *, add_n: int, add_tokens: int):
        """Инкремент n_done и usage_total_tokens по мере готовности чанков."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE jobs SET n_done = n_done + ?, usage_total_tokens = usage_total_tokens + ? WHERE id = ?",
                (add_n, add_tokens, job_id),
            )
            await db.commit()

    async def add_image(self, job_id: str, local_path: str, size_bytes: int) -> str:
        image_id = uuid.uuid4().hex
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO images (id, job_id, local_path, size_bytes) VALUES (?, ?, ?, ?)",
                (image_id, job_id, local_path, size_bytes),
            )
            await db.commit()
        return image_id

    async def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def get_job_images(self, job_id: str) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM images WHERE job_id = ? ORDER BY created_at ASC", (job_id,)
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def get_user_jobs(self, telegram_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT j.* FROM jobs j
                JOIN users u ON j.user_id = u.id
                WHERE u.telegram_id = ?
                ORDER BY j.created_at DESC
                LIMIT ?
                """,
                (telegram_id, limit),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def get_user_images(self, telegram_id: int, limit: int = 60) -> List[Dict[str, Any]]:
        """Все картинки юзера (для общей плитки истории) с инфой о джобе."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT i.id, i.job_id, i.local_path, i.size_bytes, i.created_at,
                       j.prompt, j.size, j.quality, j.status, j.cost_real
                FROM images i
                JOIN jobs j ON i.job_id = j.id
                JOIN users u ON j.user_id = u.id
                WHERE u.telegram_id = ? AND j.status IN ('done','partial')
                ORDER BY i.created_at DESC
                LIMIT ?
                """,
                (telegram_id, limit),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]


db = Database()
