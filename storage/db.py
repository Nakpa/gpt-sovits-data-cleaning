"""SQLite 缓存数据库操作."""

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

DB_FILENAME = "preprocess_cache.db"


def get_db_path() -> Path:
    """数据库放在项目 .cache 目录下。"""
    cache_dir = _PROJECT_ROOT / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / DB_FILENAME


def init_db() -> sqlite3.Connection:
    """初始化数据库，建表建索引，返回连接。"""
    db_path = get_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audio_cache (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name   TEXT NOT NULL,
            file_hash   TEXT NOT NULL,
            file_path   TEXT NOT NULL,
            file_size   INTEGER,
            duration_ms REAL,
            status      TEXT DEFAULT 'pending',
            asr_text    TEXT,
            emotion     TEXT,
            language    TEXT,
            asr_raw     TEXT,
            text_emotion TEXT,
            emotion_final TEXT,
            confidence  REAL,
            error_msg   TEXT,
            quality_issues TEXT,          -- JSON array: ["削波率 12%", ...]
            raw_asr_text TEXT,            -- ASR 原始文本 (归一化前)
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_hash ON audio_cache(file_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON audio_cache(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emotion ON audio_cache(emotion_final)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_name_hash ON audio_cache(file_name, file_hash)")
    # 唯一约束保证 upsert 能正确工作
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_name_hash_unique ON audio_cache(file_name, file_hash)")

    # 兼容旧数据库：尝试添加新字段
    for col, col_def in [
        ("quality_issues", "TEXT"),
        ("raw_asr_text", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE audio_cache ADD COLUMN {col} {col_def}")
        except Exception:
            pass  # 字段已存在

    conn.commit()
    return conn


def lookup_cache(conn: sqlite3.Connection, file_name: str, file_hash: str) -> Optional[dict]:
    """查找缓存记录，命中则返回 dict，否则返回 None。"""
    row = conn.execute(
        "SELECT * FROM audio_cache WHERE file_name = ? AND file_hash = ? AND status = 'done'",
        (file_name, file_hash),
    ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def insert_pending(conn: sqlite3.Connection, file_name: str, file_path: str,
                   file_hash: str, file_size: int, duration_ms: Optional[float] = None):
    """插入或重置一条待处理记录。已存在的记录（包括 error）会被重置为 pending。"""
    conn.execute(
        """INSERT INTO audio_cache (file_name, file_hash, file_path, file_size, duration_ms, status)
           VALUES (?, ?, ?, ?, ?, 'pending')
           ON CONFLICT(file_name, file_hash) DO UPDATE SET
             status = 'pending',
             file_path = excluded.file_path,
             file_size = excluded.file_size,
             duration_ms = excluded.duration_ms,
             error_msg = NULL,
             updated_at = datetime('now')""",
        (file_name, file_hash, file_path, file_size, duration_ms),
    )
    conn.commit()


def mark_processing(conn: sqlite3.Connection, file_name: str, file_hash: str):
    conn.execute(
        "UPDATE audio_cache SET status = 'processing', updated_at = datetime('now') WHERE file_name = ? AND file_hash = ?",
        (file_name, file_hash),
    )
    conn.commit()


def save_result(conn: sqlite3.Connection, file_name: str, file_hash: str,
                asr_text: str, emotion: str, language: str, asr_raw: dict,
                confidence: Optional[float] = None, status: str = "done",
                quality_issues: Optional[list[str]] = None, raw_asr_text: str = ""):
    """写入 ASR 结果。"""
    issues_json = json.dumps(quality_issues or [], ensure_ascii=False)
    conn.execute(
        """UPDATE audio_cache SET
             status = ?,
             asr_text = ?,
             emotion = ?,
             language = ?,
             asr_raw = ?,
             confidence = ?,
             quality_issues = ?,
             raw_asr_text = ?,
             updated_at = datetime('now')
           WHERE file_name = ? AND file_hash = ?""",
        (status, asr_text, emotion, language, json.dumps(asr_raw, ensure_ascii=False),
         confidence, issues_json, raw_asr_text or "", file_name, file_hash),
    )
    conn.commit()


def save_error(conn, file_name, file_hash, error_msg):
    conn.execute(
        "UPDATE audio_cache SET status='error', error_msg=?, updated_at=datetime('now') WHERE file_name=? AND file_hash=?",
        (error_msg, file_name, file_hash),
    )
    conn.commit()


def update_emotion_final(conn: sqlite3.Connection, record_id: int, emotion_final: str):
    conn.execute(
        "UPDATE audio_cache SET emotion_final = ?, updated_at = datetime('now') WHERE id = ?",
        (emotion_final, record_id),
    )
    conn.commit()


def get_all_done(conn: sqlite3.Connection) -> list[dict]:
    """返回所有已完成的记录。"""
    rows = conn.execute(
        "SELECT * FROM audio_cache WHERE status = 'done' ORDER BY id"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_stats(conn: sqlite3.Connection) -> dict:
    """统计各状态的记录数。"""
    rows = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM audio_cache GROUP BY status"
    ).fetchall()
    return {r["status"]: r["cnt"] for r in rows}


def clear_all(conn: sqlite3.Connection) -> int:
    """清空所有缓存记录，返回删除条数。"""
    cur = conn.execute("DELETE FROM audio_cache")
    conn.commit()
    return cur.rowcount


def drop_db():
    """完全删除缓存数据库文件。"""
    db_path = get_db_path()
    if db_path.exists():
        db_path.unlink()


def reset_errors(conn: sqlite3.Connection) -> int:
    """将 error 状态的记录重置为 pending，返回重置数。"""
    cur = conn.execute(
        "UPDATE audio_cache SET status = 'pending', error_msg = NULL, updated_at = datetime('now') WHERE status = 'error'"
    )
    conn.commit()
    return cur.rowcount


def _row_to_dict(row) -> dict:
    d = dict(row)
    if d.get("asr_raw") and isinstance(d["asr_raw"], str):
        try:
            d["asr_raw"] = json.loads(d["asr_raw"])
        except json.JSONDecodeError:
            pass
    return d
