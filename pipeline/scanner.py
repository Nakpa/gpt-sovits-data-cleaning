"""目录扫描 + MD5 哈希 + 缓存对比."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from audio.utils import is_audio_file, get_audio_duration_ms, check_sample_rate
from storage.db import lookup_cache, insert_pending, save_result

# 时长过滤阈值 (毫秒)
MIN_DURATION_MS = 500    # 少于 0.5s 过滤
MAX_DURATION_MS = 30000  # 多于 30s 过滤


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

    source_dir = str(audio_dir.resolve())
    result = {"total": len(audio_files), "cached": 0, "new": 0, "changed": 0, "skipped": 0, "items": {}, "warnings": []}

    def _scan_one(fp: Path) -> tuple[str, str, Path, int, Optional[float], dict]:
        h = compute_md5(fp)
        size = fp.stat().st_size
        dur = get_audio_duration_ms(fp)
        sr_check = check_sample_rate(fp) if fp.suffix.lower() == ".wav" else {"ok": True, "actual": None}
        return fp.name, h, fp, size, dur, sr_check

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        file_data = list(pool.map(_scan_one, audio_files))

    for name, file_hash, fp, size, dur, sr_check in file_data:
        # 采样率警告
        if not sr_check.get("ok", True):
            result["warnings"].append(f"[采样率] {name}: {sr_check['message']}")

        # 时长过滤
        if dur is not None:
            if dur < MIN_DURATION_MS:
                result["warnings"].append(f"[太短] {name}: {dur:.0f}ms (最低 {MIN_DURATION_MS}ms)")
                result["skipped"] += 1
                # 写入 filtered 状态，不进入处理队列
                save_result(conn, name, file_hash,
                    asr_text="", emotion="neutral", language="", asr_raw={},
                    status="filtered", source_dir=source_dir,
                    quality_issues=[f"duration too short: {dur:.0f}ms < {MIN_DURATION_MS}ms"])
                result["items"][str(fp)] = "skipped"
                continue
            if dur > MAX_DURATION_MS:
                result["warnings"].append(f"[太长] {name}: {dur/1000:.1f}s (最大 {MAX_DURATION_MS/1000:.0f}s)")
                result["skipped"] += 1
                save_result(conn, name, file_hash,
                    asr_text="", emotion="neutral", language="", asr_raw={},
                    status="filtered", source_dir=source_dir,
                    quality_issues=[f"duration too long: {dur/1000:.1f}s > {MAX_DURATION_MS/1000:.0f}s"])
                result["items"][str(fp)] = "skipped"
                continue

        # backfill duration for cached files that are missing it
        if dur is not None:
            conn.execute(
                "UPDATE audio_cache SET duration_ms = ? WHERE file_name = ? AND file_hash = ? AND duration_ms IS NULL",
                (dur, name, file_hash),
            )

        cached = lookup_cache(conn, name, file_hash)
        if cached:
            result["cached"] += 1
            result["items"][str(fp)] = "cached"
        else:
            cur = conn.execute(
                "SELECT file_hash FROM audio_cache WHERE file_name = ? AND status = 'done'",
                (name,),
            ).fetchone()
            if cur and cur[0] != file_hash:
                result["changed"] += 1
            else:
                result["new"] += 1
            insert_pending(conn, name, str(fp), file_hash, size, dur, source_dir=source_dir)
            result["items"][str(fp)] = "new"

    return result
