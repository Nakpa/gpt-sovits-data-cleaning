"""目录扫描 + MD5 哈希 + 缓存对比."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from audio_utils import is_audio_file, get_wav_duration_ms
from db import lookup_cache, insert_pending


def compute_md5(file_path: Path) -> str:
    """计算文件的 MD5 哈希。"""
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_directory(audio_dir: Path, conn: sqlite3.Connection,
                   max_workers: int = 8) -> dict:
    """扫描目录，对比缓存，返回 {file_path: status}。

    status 值: 'cached' (跳过) | 'new' (需处理) | 'changed' (文件变了，需重新处理)
    """
    audio_files = sorted(
        [p for p in audio_dir.iterdir() if p.is_file() and is_audio_file(p)]
    )

    result = {"total": len(audio_files), "cached": 0, "new": 0, "changed": 0, "items": {}}

    def _scan_one(fp: Path) -> tuple[str, str, Path, int, Optional[float]]:
        h = compute_md5(fp)
        size = fp.stat().st_size
        dur = get_wav_duration_ms(fp)
        return fp.name, h, fp, size, dur

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        file_data = list(pool.map(_scan_one, audio_files))

    for name, file_hash, fp, size, dur in file_data:
        cached = lookup_cache(conn, name, file_hash)
        if cached:
            result["cached"] += 1
            result["items"][str(fp)] = "cached"
        else:
            # Check if same name but different hash (file changed)
            cur = conn.execute(
                "SELECT file_hash FROM audio_cache WHERE file_name = ? AND status = 'done'",
                (name,),
            ).fetchone()
            if cur and cur[0] != file_hash:
                result["changed"] += 1
            else:
                result["new"] += 1
            insert_pending(conn, name, str(fp), file_hash, size, dur)
            result["items"][str(fp)] = "new"

    return result
