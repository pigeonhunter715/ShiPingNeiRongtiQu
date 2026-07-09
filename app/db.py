from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .text_utils import simplify_segments, to_simplified

DB_PATH = Path(__file__).resolve().parent.parent / "data.sqlite3"
SUMMARY_DEFAULT_MODEL = "gpt-5.4-mini"
TRANSCRIBE_DEFAULT_MODEL = "whisper-1"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bvid TEXT NOT NULL,
                cid INTEGER NOT NULL,
                page INTEGER NOT NULL,
                title TEXT NOT NULL,
                part_title TEXT NOT NULL DEFAULT '',
                owner TEXT NOT NULL DEFAULT '',
                duration INTEGER NOT NULL DEFAULT 0,
                url TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                transcript_source TEXT NOT NULL DEFAULT '',
                asr_status TEXT NOT NULL DEFAULT 'not_started',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (bvid, cid)
            );

            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                start REAL NOT NULL,
                end REAL NOT NULL,
                text TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_segments_text ON segments(text);
            CREATE INDEX IF NOT EXISTS idx_segments_video_start ON segments(video_id, start);

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT 'import',
                status TEXT NOT NULL,
                total INTEGER NOT NULL DEFAULT 0,
                processed INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                logs TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS video_summaries (
                video_id INTEGER PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
                summary TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        ensure_column(conn, "jobs", "kind", "TEXT NOT NULL DEFAULT 'import'")
        ensure_column(conn, "jobs", "skipped", "INTEGER NOT NULL DEFAULT 0")


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def create_job(total: int, kind: str = "import") -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (kind, status, total) VALUES (?, ?, ?)",
            (kind, "queued", total),
        )
        return int(cur.lastrowid)


def update_job(
    job_id: int,
    *,
    status: str | None = None,
    total_delta: int = 0,
    processed_delta: int = 0,
    success_delta: int = 0,
    failed_delta: int = 0,
    skipped_delta: int = 0,
    log: dict[str, Any] | None = None,
) -> None:
    with connect() as conn:
        row = conn.execute("SELECT logs FROM jobs WHERE id = ?", (job_id,)).fetchone()
        logs = json.loads(row["logs"]) if row else []
        if log is not None:
            logs.append(log)

        assignments = [
            "total = total + ?",
            "processed = processed + ?",
            "success = success + ?",
            "failed = failed + ?",
            "skipped = skipped + ?",
            "logs = ?",
            "updated_at = CURRENT_TIMESTAMP",
        ]
        params: list[Any] = [
            total_delta,
            processed_delta,
            success_delta,
            failed_delta,
            skipped_delta,
            json.dumps(logs, ensure_ascii=False),
        ]
        if status is not None:
            assignments.insert(0, "status = ?")
            params.insert(0, status)
        params.append(job_id)
        conn.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", params)


def get_job(job_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    data = row_to_dict(row)
    if data:
        data["logs"] = json.loads(data["logs"])
    return data


def upsert_video(video: dict[str, Any], segments: list[dict[str, Any]]) -> int:
    segments = simplify_segments(segments)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO videos (
                bvid, cid, page, title, part_title, owner, duration, url, status,
                error, transcript_source, asr_status, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(bvid, cid) DO UPDATE SET
                page = excluded.page,
                title = excluded.title,
                part_title = excluded.part_title,
                owner = excluded.owner,
                duration = excluded.duration,
                url = excluded.url,
                status = excluded.status,
                error = excluded.error,
                transcript_source = excluded.transcript_source,
                asr_status = excluded.asr_status,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                video["bvid"],
                video["cid"],
                video["page"],
                video["title"],
                video.get("part_title", ""),
                video.get("owner", ""),
                video.get("duration", 0),
                video["url"],
                video["status"],
                video.get("error", ""),
                video.get("transcript_source", ""),
                video.get("asr_status", "not_started"),
            ),
        )
        row = conn.execute(
            "SELECT id FROM videos WHERE bvid = ? AND cid = ?",
            (video["bvid"], video["cid"]),
        ).fetchone()
        video_id = int(row["id"])
        conn.execute("DELETE FROM segments WHERE video_id = ?", (video_id,))
        conn.executemany(
            "INSERT INTO segments (video_id, start, end, text) VALUES (?, ?, ?, ?)",
            [(video_id, item["start"], item["end"], item["text"]) for item in segments],
        )
        return video_id


def get_video(video_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone())


def get_video_by_bvid_cid(bvid: str, cid: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM videos WHERE bvid = ? AND cid = ?",
            (bvid, cid),
        ).fetchone()
        return row_to_dict(row)


def video_exists(bvid: str, cid: int) -> bool:
    return get_video_by_bvid_cid(bvid, cid) is not None


def delete_video(video_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
        return cur.rowcount > 0


def get_video_segments(video_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, video_id, start, end, text
            FROM segments
            WHERE video_id = ?
            ORDER BY start ASC, id ASC
            """,
            (video_id,),
        ).fetchall()
        return simplify_segments([dict(row) for row in rows])


def get_video_summary(video_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM video_summaries WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        return row_to_dict(row)


def save_video_summary(video_id: int, summary: str, model: str) -> None:
    summary = to_simplified(summary)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO video_summaries (video_id, summary, model, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(video_id) DO UPDATE SET
                summary = excluded.summary,
                model = excluded.model,
                updated_at = CURRENT_TIMESTAMP
            """,
            (video_id, summary, model),
        )


def get_videos_by_ids(video_ids: list[int]) -> list[dict[str, Any]]:
    if not video_ids:
        return []
    placeholders = ",".join("?" for _ in video_ids)
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM videos WHERE id IN ({placeholders})", video_ids).fetchall()
    by_id = {int(row["id"]): dict(row) for row in rows}
    return [by_id[video_id] for video_id in video_ids if video_id in by_id]


def list_missing_transcript_videos() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM videos
            WHERE status = 'no_transcript'
            ORDER BY updated_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def set_video_asr_status(video_id: int, asr_status: str, error: str = "") -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE videos
            SET asr_status = ?, error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (asr_status, error, video_id),
        )


def save_transcript(video_id: int, source: str, segments: list[dict[str, Any]]) -> None:
    segments = simplify_segments(segments)
    with connect() as conn:
        conn.execute("DELETE FROM segments WHERE video_id = ?", (video_id,))
        conn.executemany(
            "INSERT INTO segments (video_id, start, end, text) VALUES (?, ?, ?, ?)",
            [(video_id, item["start"], item["end"], item["text"]) for item in segments],
        )
        conn.execute(
            """
            UPDATE videos
            SET status = 'ready',
                error = '',
                transcript_source = ?,
                asr_status = 'ready',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (source, video_id),
        )


def list_videos(limit: int = 100, offset: int = 0, content_state: str = "all") -> list[dict[str, Any]]:
    having = video_content_having_clause(content_state)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT v.*, COUNT(s.id) AS segment_count
            FROM videos v
            LEFT JOIN segments s ON s.video_id = v.id
            GROUP BY v.id
            {having}
            ORDER BY v.updated_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]


def count_videos(content_state: str = "all") -> int:
    having = video_content_having_clause(content_state)
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM (
                SELECT v.id
                FROM videos v
                LEFT JOIN segments s ON s.video_id = v.id
                GROUP BY v.id
                {having}
            )
            """
        ).fetchone()
        return int(row["total"] if row else 0)


def video_content_having_clause(content_state: str) -> str:
    if content_state == "available":
        return "HAVING COUNT(s.id) > 0"
    if content_state == "unavailable":
        return "HAVING COUNT(s.id) = 0"
    if content_state == "all":
        return ""
    raise ValueError("content_state must be available, unavailable, or all")


def search_segments(query: str, limit: int = 80) -> list[dict[str, Any]]:
    pattern = f"%{query}%"
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                s.id AS segment_id,
                s.start,
                s.end,
                s.text,
                v.id AS video_id,
                v.bvid,
                v.cid,
                v.page,
                v.title,
                v.part_title,
                v.owner,
                v.url
            FROM segments s
            JOIN videos v ON v.id = s.video_id
            WHERE s.text LIKE ?
            ORDER BY v.title COLLATE NOCASE, s.start
            LIMIT ?
            """,
            (pattern, limit),
        ).fetchall()
        return [simplify_search_row(dict(row)) for row in rows]


def simplify_search_row(row: dict[str, Any]) -> dict[str, Any]:
    row["text"] = to_simplified(str(row.get("text") or ""))
    return row


def get_setting(key: str) -> str:
    try:
        with connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return ""
    return str(row["value"]) if row else ""


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        if value:
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )
        else:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def get_openai_api_key() -> str:
    return get_setting("openai_api_key").strip()


def set_openai_api_key(value: str) -> None:
    set_setting("openai_api_key", value.strip())


def get_openai_config(kind: str) -> dict[str, str]:
    if kind not in {"summary", "transcribe"}:
        raise ValueError("kind must be summary or transcribe")
    default_model = SUMMARY_DEFAULT_MODEL if kind == "summary" else TRANSCRIBE_DEFAULT_MODEL
    prefix = f"{kind}_openai"
    legacy_key = get_openai_api_key()
    return {
        "api_key": get_setting(f"{prefix}_api_key").strip() or legacy_key,
        "base_url": get_setting(f"{prefix}_base_url").strip(),
        "model": get_setting(f"{prefix}_model").strip() or default_model,
        "source": "settings" if get_setting(f"{prefix}_api_key").strip() else ("legacy" if legacy_key else ""),
    }


def set_openai_config(kind: str, *, api_key: str, base_url: str, model: str) -> None:
    if kind not in {"summary", "transcribe"}:
        raise ValueError("kind must be summary or transcribe")
    prefix = f"{kind}_openai"
    set_setting(f"{prefix}_api_key", api_key.strip())
    set_setting(f"{prefix}_base_url", base_url.strip())
    set_setting(f"{prefix}_model", model.strip())


def mask_secret(value: str) -> str:
    secret = value.strip()
    if not secret:
        return ""
    if len(secret) <= 8:
        return "***"
    return f"{secret[:3]}...{secret[-4:]}"
