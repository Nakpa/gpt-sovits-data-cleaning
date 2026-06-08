"""GPT-SoVITS Data Cleaning — Web UI Server.

FastAPI backend exposing all CLI operations as REST endpoints,
with SSE streaming for real-time progress during ASR processing.

Usage:
    python server.py              # start on http://localhost:8765
    python server.py --port 9000  # custom port
"""

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, Request, Form, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from env_loader import load_dotenv

load_dotenv()

from storage.db import init_db, get_stats, get_all_done, clear_all, reset_errors
from pipeline.scanner import scan_directory
from pipeline.processor import process_files
from pipeline.postprocess import run_postprocess
from storage.formatters import export_all, next_version
from audio.fixer import auto_fix
from audio.utils import is_audio_file, check_sample_rate, detect_clipping, detect_silence
from state import save_audio_dir, load_audio_dir, load_audio_dirs, remove_audio_dir

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="GPT-SoVITS Data Cleaning", version="0.1.0")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

from typing import Optional as _Opt

# ── Progress tracking ──────────────────────────────────────────────
# Maps task_id -> asyncio.Queue. Each task gets its own queue for SSE.
_progress_queues: dict = {}
_active_task: _Opt[str] = None


async def _push_progress(task_id: str, event: str, data: dict):
    """Push a progress event to the task's SSE queue."""
    q = _progress_queues.get(task_id)
    if q:
        await q.put({"event": event, "data": data})


async def _close_progress(task_id: str):
    global _active_task
    q = _progress_queues.get(task_id)
    if q:
        await q.put(None)  # sentinel to close SSE
    _progress_queues.pop(task_id, None)
    if _active_task == task_id:
        _active_task = None


# ── Pages ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    dirs = load_audio_dirs()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "saved_dirs": [str(d) for d in dirs],
        "saved_dir": str(dirs[0]) if dirs else "",
        "has_api_key": bool(os.getenv("DASHSCOPE_API_KEY")),
    })


# ── Dashboard / Stats ──────────────────────────────────────────────

@app.get("/api/stats")
async def api_stats(dir: str = ""):
    conn = init_db()
    stats = get_stats(conn, source_dir=dir)
    total = sum(stats.values()) if stats else 0

    where_clause = ("WHERE source_dir = ?" if dir else "WHERE 1=1")
    params = (dir,) if dir else ()

    q_row = conn.execute(
        f"SELECT COUNT(*) as cnt FROM audio_cache {where_clause} AND quality_issues IS NOT NULL AND quality_issues != '[]' AND quality_issues != ''",
        params,
    ).fetchone()

    emotion_rows = conn.execute(
        f"SELECT emotion, COUNT(*) as cnt FROM audio_cache {where_clause} AND status = 'done' GROUP BY emotion ORDER BY cnt DESC",
        params,
    ).fetchall()

    raw_text_count = conn.execute(
        f"SELECT COUNT(*) as cnt FROM audio_cache {where_clause} AND raw_asr_text IS NOT NULL AND raw_asr_text != ''",
        params,
    ).fetchone()

    conn.close()

    return {
        "total": total,
        "done": stats.get("done", 0),
        "pending": stats.get("pending", 0),
        "error": stats.get("error", 0),
        "filtered": stats.get("filtered", 0),
        "processing": stats.get("processing", 0),
        "quality_issues": q_row["cnt"] if q_row else 0,
        "raw_asr_text_count": raw_text_count["cnt"] if raw_text_count else 0,
        "emotions": {r["emotion"]: r["cnt"] for r in emotion_rows},
        "saved_dir": str(load_audio_dir()) if load_audio_dir() else "",
        "registered_dirs": [str(d) for d in load_audio_dirs()],
    }


# ── Audio Directory ────────────────────────────────────────────────

@app.post("/api/audio-dir")
async def api_set_audio_dir(dir_path: str = Form(...)):
    audio_dir = Path(dir_path)
    if not audio_dir.exists():
        return JSONResponse({"ok": False, "error": f"目录不存在: {dir_path}"}, 400)
    if not audio_dir.is_dir():
        return JSONResponse({"ok": False, "error": f"不是目录: {dir_path}"}, 400)
    is_new = save_audio_dir(audio_dir)
    return {"ok": True, "dir": str(audio_dir.resolve()), "is_new": is_new}


# ── Directory Management ───────────────────────────────────────────

@app.get("/api/dirs")
async def api_list_dirs():
    """返回所有注册目录及各自的统计信息。"""
    dirs = load_audio_dirs()
    result = []
    for d in dirs:
        conn = init_db()
        stats = get_stats(conn, source_dir=str(d.resolve()))
        total = sum(stats.values()) if stats else 0
        conn.close()
        result.append({
            "path": str(d.resolve()),
            "exists": d.exists(),
            "total": total,
            "done": stats.get("done", 0),
            "pending": stats.get("pending", 0),
            "error": stats.get("error", 0),
            "filtered": stats.get("filtered", 0),
        })
    return {"ok": True, "dirs": result}


@app.post("/api/dirs")
async def api_add_dir(dir_path: str = Form(...)):
    audio_dir = Path(dir_path)
    if not audio_dir.exists():
        return JSONResponse({"ok": False, "error": f"目录不存在: {dir_path}"}, 400)
    if not audio_dir.is_dir():
        return JSONResponse({"ok": False, "error": f"不是目录: {dir_path}"}, 400)
    is_new = save_audio_dir(audio_dir)
    return {"ok": True, "dir": str(audio_dir.resolve()), "is_new": is_new}


@app.delete("/api/dirs")
async def api_remove_dir(dir_path: str = Form(...)):
    audio_dir = Path(dir_path)
    removed = remove_audio_dir(audio_dir)
    if not removed:
        return JSONResponse({"ok": False, "error": "目录未在注册列表中"}, 404)
    return {"ok": True, "message": f"已移除: {dir_path}"}


@app.post("/api/dirs/refresh")
async def api_refresh_all_dirs():
    """重新扫描所有注册目录，更新缓存中的 source_dir 和 duration。"""
    from audio.utils import get_audio_duration_ms
    dirs = load_audio_dirs()
    total_dur_updates = 0
    total_source_updates = 0

    for d in dirs:
        conn = init_db()
        source_dir = str(d.resolve())

        # 为该目录下的文件补填 source_dir
        cur = conn.execute(
            "UPDATE audio_cache SET source_dir = ?, updated_at = datetime('now') "
            "WHERE (source_dir IS NULL OR source_dir = '') AND file_path LIKE ?",
            (source_dir, str(d) + "%"),
        )
        total_source_updates += cur.rowcount

        # 补填 duration
        null_rows = conn.execute(
            "SELECT id, file_path FROM audio_cache WHERE source_dir = ? AND duration_ms IS NULL",
            (source_dir,),
        ).fetchall()
        for r in null_rows:
            fp = Path(r["file_path"])
            if fp.exists():
                dur = get_audio_duration_ms(fp)
                if dur is not None:
                    conn.execute(
                        "UPDATE audio_cache SET duration_ms = ?, updated_at = datetime('now') WHERE id = ?",
                        (dur, r["id"]),
                    )
                    total_dur_updates += 1
        conn.commit()
        conn.close()

    return {
        "ok": True,
        "dirs_scanned": len(dirs),
        "source_dir_updates": total_source_updates,
        "duration_updates": total_dur_updates,
    }


# ── Scan ───────────────────────────────────────────────────────────

@app.post("/api/scan")
async def api_scan(dir_path: str = Form("")):
    audio_dir = Path(dir_path) if dir_path else load_audio_dir()
    if not audio_dir or not audio_dir.exists():
        return JSONResponse({"ok": False, "error": "请先选择一个有效的音频目录"}, 400)

    conn = init_db()
    scan_result = scan_directory(audio_dir, conn)

    items = []
    for fp_s, status in sorted(scan_result.get("items", {}).items()):
        fp = Path(fp_s)
        items.append({
            "file_name": fp.name,
            "file_path": fp_s,
            "status": status,
        })

    conn.close()

    # Auto-register directory
    save_audio_dir(audio_dir)

    return {
        "ok": True,
        "dir": str(audio_dir),
        "total": scan_result["total"],
        "cached": scan_result["cached"],
        "new": scan_result["new"],
        "changed": scan_result["changed"],
        "skipped": scan_result.get("skipped", 0),
        "warnings": scan_result.get("warnings", []),
        "items": items,
    }


# ── Files ──────────────────────────────────────────────────────────

@app.get("/api/files")
async def api_files(status: str = "", dir: str = "", limit: int = 500, offset: int = 0):
    conn = init_db()

    conditions = []
    params = []
    if status:
        conditions.append("status = ?")
        params.append(status)
    if dir:
        conditions.append("source_dir = ?")
        params.append(dir)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = conn.execute(
        f"SELECT * FROM audio_cache {where} ORDER BY id LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    count_row = conn.execute(
        f"SELECT COUNT(*) as cnt FROM audio_cache {where}", params
    ).fetchone()

    files = []
    for r in rows:
        d = dict(r)
        d.pop("asr_raw", None)
        files.append(d)

    conn.close()
    return {"files": files, "total": count_row["cnt"] if count_row else 0}


# ── Fix ────────────────────────────────────────────────────────────

@app.post("/api/fix")
async def api_fix(dir_path: str = Form(""), dry_run: bool = Form(False)):
    audio_dir = Path(dir_path) if dir_path else load_audio_dir()
    if not audio_dir or not audio_dir.exists():
        return JSONResponse({"ok": False, "error": "请先选择一个有效的音频目录"}, 400)

    files = sorted([p for p in audio_dir.iterdir() if p.is_file() and is_audio_file(p)])

    results = []
    fixed_count = 0
    for fp in files:
        if dry_run:
            sr = check_sample_rate(fp)
            clip = detect_clipping(fp)
            sil = detect_silence(fp)
            issues = []
            if not sr["ok"]:
                issues.append(sr["message"])
            if not clip["ok"]:
                issues.append(clip["message"])
            if not sil["ok"]:
                issues.append(sil["message"])
            results.append({
                "file_name": fp.name,
                "status": "PASS" if not issues else "FAIL",
                "issues": issues,
            })
        else:
            result = auto_fix(fp)
            if result.get("any_fixed"):
                fixed_count += 1
                parts = []
                if result.get("sample_rate", {}).get("fixed"):
                    parts.append(f"重采样 {result['sample_rate']['method']}")
                if result.get("loudness", {}).get("fixed"):
                    parts.append(f"响度 {result['loudness']['peak_before_db']}→{result['loudness']['peak_after_db']}dB")
                if result.get("clipping", {}).get("fixed"):
                    parts.append(f"去削波 gain={result['clipping']['gain_applied']}")
                results.append({
                    "file_name": fp.name,
                    "fixed": True,
                    "details": ", ".join(parts),
                })
            else:
                results.append({
                    "file_name": fp.name,
                    "fixed": False,
                    "details": "",
                })

    return {
        "ok": True,
        "dry_run": dry_run,
        "total": len(files),
        "fixed_count": fixed_count if not dry_run else sum(1 for r in results if r.get("status") == "FAIL"),
        "results": results,
    }


# ── Process (ASR) ──────────────────────────────────────────────────

@app.post("/api/process")
async def api_process(
    dir_path: str = Form(""),
    speaker: str = Form("heroine"),
    language: str = Form("ja"),
    concurrency: int = Form(3),
    fix_audio: bool = Form(False),
    skip_postprocess: bool = Form(False),
):
    global _active_task

    audio_dir = Path(dir_path) if dir_path else load_audio_dir()
    if not audio_dir or not audio_dir.exists():
        return JSONResponse({"ok": False, "error": "请先选择一个有效的音频目录"}, 400)

    if not os.getenv("DASHSCOPE_API_KEY"):
        return JSONResponse({"ok": False, "error": "未设置 DASHSCOPE_API_KEY，请检查 .env 配置"}, 400)

    if _active_task:
        return JSONResponse({"ok": False, "error": "已有正在运行的处理任务"}, 409)

    conn = init_db()

    # Scan first
    scan_result = scan_directory(audio_dir, conn)
    new_or_changed = scan_result["new"] + scan_result["changed"]

    if new_or_changed == 0:
        conn.close()
        return {"ok": True, "message": "no_work", "total": scan_result["total"], "cached": scan_result["cached"]}

    source_dir = str(audio_dir.resolve())
    rows = conn.execute(
        "SELECT file_path FROM audio_cache WHERE status = 'pending' AND source_dir = ?",
        (source_dir,),
    ).fetchall()
    pending_files = [Path(r[0]) for r in rows]

    if not pending_files:
        conn.close()
        return {"ok": True, "message": "no_work", "total": scan_result["total"], "cached": scan_result["cached"]}

    task_id = str(uuid.uuid4())[:8]
    _active_task = task_id
    _progress_queues[task_id] = asyncio.Queue()

    start_time = time.time()

    def on_progress(done, total, current_file, status, error_msg=""):
        elapsed = time.time() - start_time
        eta = (elapsed / done) * (total - done) if done > 0 else 0
        asyncio.ensure_future(_push_progress(task_id, "progress", {
            "done": done,
            "total": total,
            "current_file": current_file,
            "status": status,
            "elapsed": elapsed,
            "eta": eta,
            "pct": done / total * 100,
            "error": error_msg[:200] if error_msg else "",
        }))

    async def run_processing():
        try:
            await process_files(
                pending_files, conn, language, concurrency,
                fix_audio=fix_audio, on_progress=on_progress,
            )
            elapsed = time.time() - start_time
            final_stats = get_stats(conn)

            # Postprocess
            pp_stats = None
            if not skip_postprocess:
                pp_stats = run_postprocess(conn)

            # Export
            output_root = Path("./output")
            ver = next_version(output_root)
            export_all(conn, audio_dir, output_root, speaker, language)

            await _push_progress(task_id, "complete", {
                "elapsed": elapsed,
                "done_count": final_stats.get("done", 0),
                "error_count": final_stats.get("error", 0),
                "postprocess": pp_stats,
                "version": ver,
            })
        except Exception as e:
            await _push_progress(task_id, "error", {"message": str(e)})
        finally:
            conn.close()
            await _push_progress(task_id, "done", {})
            await asyncio.sleep(0.5)
            await _close_progress(task_id)

    asyncio.ensure_future(run_processing())

    return {
        "ok": True,
        "task_id": task_id,
        "total": len(pending_files),
        "warnings": scan_result.get("warnings", []),
        "scan_summary": {
            "total": scan_result["total"],
            "cached": scan_result["cached"],
            "new": scan_result["new"],
            "changed": scan_result["changed"],
        },
    }


@app.get("/api/stream/{task_id}")
async def api_stream(task_id: str):
    """SSE endpoint for real-time progress."""
    q = _progress_queues.get(task_id)
    if not q:
        return StreamingResponse(
            _empty_sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def event_generator():
        while True:
            msg = await q.get()
            if msg is None:
                break
            event = msg["event"]
            data = json.dumps(msg["data"], ensure_ascii=False)
            yield f"event: {event}\ndata: {data}\n\n"
            if event in ("done", "error"):
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


async def _empty_sse():
    yield "event: error\ndata: {\"message\":\"unknown task\"}\n\n"


@app.get("/api/process-status")
async def api_process_status():
    return {"active": _active_task is not None, "task_id": _active_task}


# ── Postprocess ────────────────────────────────────────────────────

@app.post("/api/postprocess")
async def api_postprocess():
    conn = init_db()
    pp_stats = run_postprocess(conn)

    emotion_rows = conn.execute(
        "SELECT emotion, COUNT(*) as cnt FROM audio_cache WHERE status = 'done' GROUP BY emotion ORDER BY cnt DESC"
    ).fetchall()

    filtered_rows = conn.execute(
        "SELECT file_name, asr_text, status, quality_issues, error_msg "
        "FROM audio_cache WHERE status IN ('filtered', 'error') ORDER BY id LIMIT 50"
    ).fetchall()

    conn.close()

    filtered = []
    for r in filtered_rows:
        d = dict(r)
        d.pop("quality_issues", None)
        d.pop("error_msg", None)
        text = d.get("asr_text") or r.get("error_msg", "") or ""
        filtered.append({
            "file_name": r["file_name"],
            "status": r["status"],
            "text_preview": text[:60],
        })

    return {
        "ok": True,
        "total": pp_stats["total"],
        "normalized": pp_stats["normalized"],
        "filtered": pp_stats["filtered"],
        "unchanged": pp_stats["unchanged"],
        "emotions": {r["emotion"]: r["cnt"] for r in emotion_rows},
        "filtered_items": filtered,
    }


# ── Reports ────────────────────────────────────────────────────────

@app.get("/api/reports/emotion")
async def api_emotion_report():
    conn = init_db()
    rows = conn.execute(
        "SELECT emotion, COUNT(*) as cnt FROM audio_cache WHERE status = 'done' GROUP BY emotion ORDER BY cnt DESC"
    ).fetchall()
    conn.close()

    total = sum(r["cnt"] for r in rows)
    emotions = {}
    for r in rows:
        emotions[r["emotion"]] = {
            "count": r["cnt"],
            "pct": round(r["cnt"] / total * 100, 1) if total else 0
        }

    return {"ok": True, "total": total, "emotions": emotions}


@app.get("/api/reports/filtered")
async def api_filtered_report(limit: int = 50):
    conn = init_db()
    rows = conn.execute(
        "SELECT file_name, asr_text, status, quality_issues, error_msg "
        "FROM audio_cache WHERE status IN ('filtered', 'error') ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()

    total_row = conn.execute(
        "SELECT COUNT(*) as cnt FROM audio_cache WHERE status IN ('filtered', 'error')"
    ).fetchone()
    conn.close()

    items = []
    for r in rows:
        text_preview = (r["asr_text"] or r["error_msg"] or "")[:60]
        items.append({
            "file_name": r["file_name"],
            "status": r["status"],
            "text_preview": text_preview,
        })

    return {"ok": True, "total": total_row["cnt"] if total_row else 0, "items": items}


# ── Export ─────────────────────────────────────────────────────────

@app.post("/api/export")
async def api_export(
    dir_path: str = Form(""),
    output: str = Form("./output"),
    speaker: str = Form("heroine"),
    language: str = Form("ja"),
):
    audio_dir = Path(dir_path) if dir_path else load_audio_dir()
    if not audio_dir or not audio_dir.exists():
        return JSONResponse({"ok": False, "error": "请先选择一个有效的音频目录"}, 400)

    conn = init_db()
    records = get_all_done(conn, source_dir=str(audio_dir.resolve()))

    if not records:
        conn.close()
        return JSONResponse({"ok": False, "error": "该目录下没有已完成的记录可以导出"}, 400)

    output_root = Path(output)
    ver = next_version(output_root)
    export_all(conn, audio_dir, output_root, speaker, language)

    emotion_counts = {}
    for r in records:
        em = r.get("emotion_final") or r.get("emotion") or "neutral"
        emotion_counts[em] = emotion_counts.get(em, 0) + 1

    conn.close()

    return {
        "ok": True,
        "version": ver,
        "output_dir": str(output_root / f"v{ver}"),
        "total": len(records),
        "emotion_counts": emotion_counts,
    }


# ── Clear Cache ────────────────────────────────────────────────────

@app.delete("/api/cache")
async def api_clear_cache(clear_dirs: bool = Query(False)):
    conn = init_db()
    stats = get_stats(conn)
    n = clear_all(conn) if stats else 0
    conn.close()

    dirs_cleared = 0
    if clear_dirs:
        for d in load_audio_dirs():
            remove_audio_dir(d)
            dirs_cleared += 1

    msg = f"已清空 {n} 条缓存记录"
    if dirs_cleared:
        msg += f"，已移除 {dirs_cleared} 个注册目录"

    return {"ok": True, "deleted": n, "dirs_cleared": dirs_cleared, "message": msg}


# ── Refresh Durations ──────────────────────────────────────────────

@app.post("/api/refresh-durations")
async def api_refresh_durations(dir: str = Form("")):
    """Re-scan duration for cached files that have NULL duration_ms, optionally per directory."""
    from audio.utils import get_audio_duration_ms

    conn = init_db()
    if dir:
        rows = conn.execute(
            "SELECT id, file_path, file_name FROM audio_cache WHERE source_dir = ? AND duration_ms IS NULL",
            (dir,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, file_path, file_name FROM audio_cache WHERE duration_ms IS NULL"
        ).fetchall()
    conn.close()

    if not rows:
        return {"ok": True, "updated": 0, "message": "所有文件已有 duration 数据"}

    updated = 0
    failed = 0
    conn = init_db()
    for r in rows:
        fp = Path(r["file_path"])
        if fp.exists():
            dur = get_audio_duration_ms(fp)
            if dur is not None:
                conn.execute(
                    "UPDATE audio_cache SET duration_ms = ?, updated_at = datetime('now') WHERE id = ?",
                    (dur, r["id"]),
                )
                updated += 1
            else:
                failed += 1
        else:
            failed += 1
    conn.commit()
    conn.close()

    return {
        "ok": True,
        "updated": updated,
        "failed": failed,
        "message": f"已更新 {updated} 条 duration{f'，{failed} 条失败' if failed else ''}",
    }


# ── Main ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="GPT-SoVITS 数据预处理 Web UI")
    parser.add_argument("--port", "-p", type=int, default=8765, help="监听端口 (默认: 8765)")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    args = parser.parse_args()

    print(f"""
  ╔══════════════════════════════════════╗
  ║  GPT-SoVITS 数据预处理 Web UI       ║
  ║  http://{args.host}:{args.port}                ║
  ╚══════════════════════════════════════╝
""")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
