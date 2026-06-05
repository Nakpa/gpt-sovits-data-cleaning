"""并发处理器 — 每文件单独调用 ASR API，Semaphore 控制并发。"""

import asyncio
import concurrent.futures
import sqlite3
from pathlib import Path
from typing import Optional, Callable

from asr_qwen import call_asr
from db import mark_processing, save_result, save_error
from filters import should_filter


async def process_files(
    file_paths: list[Path],
    conn: sqlite3.Connection,
    language_hint: Optional[str] = None,
    concurrency: int = 3,
    on_progress: Optional[Callable[[int, int, str, str], None]] = None,
):
    """并发处理音频文件，每条单独调用 qwen3-asr-flash。

    on_progress(done, total, current_file, status): 进度回调
    status: ok | filtered | error
    """
    total = len(file_paths)
    done = 0
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(concurrency)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=concurrency)

    async def process_one(fp: Path):
        nonlocal done

        from scanner import compute_md5
        file_name = fp.name
        file_hash = compute_md5(fp)

        async with semaphore:
            mark_processing(conn, file_name, file_hash)
            proc_status = "error"

            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    executor, call_asr, fp, language_hint
                )

                text = result.get("text", "")
                if text:
                    do_filter, _ = should_filter(text)
                    save_result(
                        conn, file_name, file_hash,
                        asr_text=text,
                        emotion=result["emotion"],
                        language=result.get("language", language_hint or ""),
                        asr_raw=result["raw_response"],
                        confidence=result.get("confidence"),
                        status="filtered" if do_filter else "done",
                    )
                    proc_status = "filtered" if do_filter else "ok"
                else:
                    error_msg = str(result.get("raw_response", {}).get("error", "empty transcription"))
                    save_error(conn, file_name, file_hash, error_msg)

            except Exception as e:
                save_error(conn, file_name, file_hash, str(e))

            async with lock:
                done += 1
                if on_progress:
                    on_progress(done, total, fp.name, proc_status)

    await asyncio.gather(*[process_one(fp) for fp in file_paths])
    executor.shutdown(wait=False)
