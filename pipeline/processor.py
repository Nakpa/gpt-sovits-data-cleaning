"""并发处理器 — 每文件单独调用 ASR API，Semaphore 控制并发。

ASR 阶段只做转写 + 情感识别，文本归一化和过滤留给后处理阶段。
"""

import asyncio
import concurrent.futures
import sqlite3
from pathlib import Path
from typing import Optional, Callable

from api.asr import call_asr
from audio.fixer import auto_fix as fix_audio_file
from audio.utils import validate_audio_quality
from storage.db import mark_processing, save_result, save_error


async def process_files(
    file_paths: list[Path],
    conn: sqlite3.Connection,
    language_hint: Optional[str] = None,
    concurrency: int = 3,
    fix_audio: bool = False,
    on_progress: Optional[Callable[[int, int, str, str, str], None]] = None,
):
    """并发 ASR 转写，原始文本直接落库。

    on_progress(done, total, current_file, status, error_msg): 进度回调
    status: ok | error, error_msg: 错误详情（仅 status=error 时有值）
    """
    total = len(file_paths)
    done = 0
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(concurrency)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=concurrency)

    async def process_one(fp: Path):
        nonlocal done

        from pipeline.scanner import compute_md5
        file_name = fp.name
        file_hash = compute_md5(fp)

        async with semaphore:
            mark_processing(conn, file_name, file_hash)
            proc_status = "error"

            try:
                loop = asyncio.get_running_loop()
                error_detail = ""

                # 0. 自动修复 (可选)
                if fix_audio:
                    fix_result = await loop.run_in_executor(executor, fix_audio_file, fp, False)
                    # ffmpeg 转换 .ogg/.mp3 → .wav 后更新 fp 和 DB 记录
                    if isinstance(fix_result, dict):
                        conv = fix_result.get("ffmpeg_convert", {})
                        new_path = conv.get("new_path", "")
                        if new_path:
                            new_fp = Path(new_path)
                            new_name = new_fp.name
                            new_hash = compute_md5(new_fp)
                            conn.execute(
                                "UPDATE audio_cache SET file_path = ?, file_name = ?, file_hash = ? WHERE file_name = ? AND file_hash = ?",
                                (str(new_fp), new_name, new_hash, file_name, file_hash),
                            )
                            conn.commit()
                            fp = new_fp
                            file_name = new_name
                            file_hash = new_hash

                # 1. 音频质量检测
                quality = await loop.run_in_executor(executor, validate_audio_quality, fp)
                quality_issues = quality.get("issues", [])

                # 2. ASR 转写 → 原始文本直接落库
                result = await loop.run_in_executor(executor, call_asr, fp, language_hint)

                raw_text = result.get("text", "")
                if raw_text:
                    save_result(
                        conn, file_name, file_hash,
                        asr_text=raw_text,               # 原始文本
                        emotion=result["emotion"],
                        language=result.get("language", language_hint or ""),
                        asr_raw=result["raw_response"],
                        confidence=result.get("confidence"),
                        status="done",
                        quality_issues=quality_issues,
                        raw_asr_text="",                  # 归一化后再写
                        source_dir=str(fp.parent.resolve()),
                    )
                    proc_status = "ok"
                else:
                    error_detail = str(result.get("raw_response", {}).get("error", "empty transcription"))
                    if quality_issues:
                        error_detail += " | audio: " + "; ".join(quality_issues)
                    save_error(conn, file_name, file_hash, error_detail)

            except Exception as e:
                error_detail = str(e)
                save_error(conn, file_name, file_hash, error_detail)

            async with lock:
                done += 1
                if on_progress:
                    on_progress(done, total, fp.name, proc_status, error_detail or "")

    await asyncio.gather(*[process_one(fp) for fp in file_paths])
    executor.shutdown(wait=False)
