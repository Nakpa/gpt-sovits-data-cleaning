"""后处理报告 — 情感分布、过滤审查."""

import json
import sqlite3
from collections import Counter

EMOTION_NAMES = {
    "neutral": "平静", "happy": "开心", "sad": "悲伤",
    "angry": "愤怒", "surprised": "惊讶", "fearful": "恐惧",
    "disgusted": "厌恶",
}


def show_emotion_distribution(conn: sqlite3.Connection) -> dict:
    """显示情感分布直方图，返回 {emotion: count}。"""
    rows = conn.execute(
        "SELECT emotion, COUNT(*) as cnt FROM audio_cache WHERE status = 'done' GROUP BY emotion ORDER BY cnt DESC"
    ).fetchall()

    if not rows:
        print("  无数据。")
        return {}

    dist = {r["emotion"]: r["cnt"] for r in rows}
    total = sum(dist.values())
    max_len = max(len(EMOTION_NAMES.get(e, e)) for e in dist)

    print(f"\n  情感分布 (共 {total} 条):")
    print(f"  {'─' * 40}")

    bar_max = 20
    max_count = max(dist.values())

    for emotion, count in sorted(dist.items(), key=lambda x: -x[1]):
        name = EMOTION_NAMES.get(emotion, emotion)
        bar_len = int(count / max_count * bar_max) if max_count else 0
        bar = "█" * bar_len + "░" * (bar_max - bar_len)
        pct = count / total * 100
        print(f"  {name:<{max_len + 2}} {bar} {pct:5.1f}% ({count})")

    print(f"  {'─' * 40}")

    # 中性占比 > 70% 警告
    neutral_pct = dist.get("neutral", 0) / total * 100
    if neutral_pct > 70:
        print(f"  [!] 中性占比 {neutral_pct:.0f}%，情感倾向过于集中。")
        print(f"      建议补充其他情感的录音，否则模型难以学到情感变化。")

    return dist


def show_filtered_review(conn: sqlite3.Connection, limit: int = 15):
    """显示被过滤的音频摘要。"""
    rows = conn.execute(
        "SELECT file_name, asr_text, status, quality_issues, error_msg "
        "FROM audio_cache WHERE status IN ('filtered', 'error') ORDER BY id"
    ).fetchall()

    if not rows:
        print("  无被过滤或失败的记录。")
        return

    print(f"\n  被过滤/失败的记录 (共 {len(rows)} 条):")
    print(f"  {'─' * 50}")

    # 按原因分组
    reason_groups = Counter()
    for r in rows:
        if r["status"] == "error":
            reason_groups["API 错误"] += 1
        else:
            # Parse quality_issues
            qs = r["quality_issues"] or ""
            if qs and qs != "[]":
                try:
                    issues = json.loads(qs)
                except Exception:
                    issues = [str(qs)]
                for issue in issues:
                    # Extract short reason
                    short = issue.split(":")[0].split(" ")[0] if issue else "?"
                    reason_groups[short] += 1
            else:
                reason_groups["语气词/噪声过滤"] += 1

    print("  按原因统计:")
    for reason, count in reason_groups.most_common():
        print(f"    {reason}: {count} 条")

    print(f"\n  明细 (前 {min(limit, len(rows))} 条):")
    for r in rows[:limit]:
        text_preview = (r["asr_text"] or r["error_msg"] or "(空)")[:40]
        print(f"    [{r['status']}] {r['file_name']}: {text_preview}")

    if len(rows) > limit:
        print(f"    ... 还有 {len(rows) - limit} 条")
