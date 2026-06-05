"""后处理 — 文本归一化 + 质量过滤，纯本地操作。

ASR 阶段已落库的原始文本，在这里批量清洗。
可反复执行，不会重复调 API。
"""

import sqlite3

from text.normalizer import normalize_japanese
from text.filters import should_filter


def run_postprocess(conn: sqlite3.Connection) -> dict:
    """对所有 status='done' 的记录执行文本归一化和过滤。

    - 归一化后将 normalized 写入 asr_text，原始文本写入 raw_asr_text
    - 归一化后仍命中过滤规则的 → status 改为 'filtered'

    返回 {total, normalized, filtered, unchanged}
    """
    rows = conn.execute(
        "SELECT id, file_name, file_hash, asr_text, status FROM audio_cache WHERE status IN ('done', 'filtered')"
    ).fetchall()

    stats = {"total": len(rows), "normalized": 0, "filtered": 0, "unchanged": 0}

    for r in rows:
        raw = r["asr_text"] or ""
        if not raw.strip():
            continue

        normalized = normalize_japanese(raw)
        do_filter, reason = should_filter(normalized)

        new_status = "filtered" if do_filter else "done"
        changed = normalized != raw
        old_status = r["status"]

        if changed or new_status != old_status:
            conn.execute(
                """UPDATE audio_cache SET
                     asr_text = ?,
                     raw_asr_text = ?,
                     status = ?,
                     updated_at = datetime('now')
                   WHERE id = ?""",
                (normalized, raw if changed else "", new_status, r["id"]),
            )
            stats["normalized" if changed else "filtered"] += 1
        else:
            stats["unchanged"] += 1

    conn.commit()
    return stats
