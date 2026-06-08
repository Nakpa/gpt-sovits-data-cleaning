"""输出格式化 — GPT-SoVITS list.txt + JSON annotations.json.

每次导出自动创建版本化目录: output/v1/, output/v2/, ...
"""

import json
import re
import shutil
from pathlib import Path

from storage.db import get_all_done


def next_version(output_root: Path) -> int:
    """扫描已有版本目录，返回下一个可用版本号。"""
    if not output_root.exists():
        return 1
    existing = []
    for d in output_root.iterdir():
        if d.is_dir():
            m = re.match(r"^v(\d+)$", d.name)
            if m:
                existing.append(int(m.group(1)))
    return max(existing) + 1 if existing else 1


def export_all(conn, audio_dir: Path, output_root: Path, speaker: str, language: str,
               version: int = None):
    """从数据库读取所有已完成记录，生成版本化输出。

    输出结构:
        output_root/v{N}/
        ├── neutral/
        ├── happy/
        ├── ...
        ├── list.txt
        └── annotations.json
    """
    records = get_all_done(conn)
    if not records:
        print("  没有已完成的记录可以导出。")
        return

    if version is None:
        version = next_version(output_root)

    output_dir = output_root / f"v{version}"
    output_dir.mkdir(parents=True, exist_ok=True)

    list_lines = []
    json_data = []

    for rec in records:
        emotion = rec.get("emotion_final") or rec.get("emotion") or "neutral"
        file_name = rec["file_name"]
        src_path = Path(rec["file_path"])

        # 按情感分文件夹
        emotion_dir = output_dir / emotion
        emotion_dir.mkdir(parents=True, exist_ok=True)
        dst_path = emotion_dir / file_name
        if src_path.exists() and not dst_path.exists():
            shutil.copy2(src_path, dst_path)

        text = (rec.get("asr_text") or "").replace("|", " ")
        vocal_path = str(src_path.resolve())

        # GPT-SoVITS 训练格式: vocal_path|speaker_name|language|text
        list_lines.append(f"{vocal_path}|{speaker}|{language}|{text}")

        json_data.append({
            "file": rel_path,
            "original_path": rec["file_path"],
            "text": text,
            "language": rec.get("language") or language,
            "emotion": {
                "audio": rec.get("emotion"),
                "text_semantic": rec.get("text_emotion"),
                "final": emotion,
            },
            "confidence": rec.get("confidence"),
            "duration_ms": rec.get("duration_ms"),
            "file_hash": rec.get("file_hash"),
            "processed_at": rec.get("updated_at"),
        })

    # 写入 list.txt
    list_path = output_dir / "list.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for line in list_lines:
            f.write(line + "\n")

    # 写入 annotations.json
    json_path = output_dir / "annotations.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)

    print(f"  → {output_dir}/")
    print(f"    list.txt  ({len(list_lines)} 条)")
    print(f"    annotations.json  ({len(json_data)} 条)")
    for em in sorted(set(r.get("emotion_final") or r.get("emotion") or "neutral" for r in records)):
        count = sum(1 for r in records if (r.get("emotion_final") or r.get("emotion") or "neutral") == em)
        print(f"    {em}/  ({count} 个文件)")
