"""输出格式化 — GPT-SoVITS list.txt + JSON annotations.json."""

import json
import shutil
from pathlib import Path

from db import get_all_done


def export_all(conn, audio_dir: Path, output_dir: Path, speaker: str, language: str):
    """从数据库读取所有已完成记录，生成三种输出。

    1. 按情感分文件夹拷贝音频
    2. list.txt (GPT-SoVITS 训练格式)
    3. annotations.json (完整结构化标注)
    """
    records = get_all_done(conn)
    if not records:
        print("  没有已完成的记录可以导出。")
        return

    output_dir.mkdir(parents=True, exist_ok=True)

    # 构建输出
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

        rel_path = f"{emotion}/{file_name}"
        text = (rec.get("asr_text") or "").replace("|", " ")

        list_lines.append(f"{rel_path}|{speaker}|{language}|{emotion}|{text}")

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

    print(f"  已生成:")
    print(f"    → {list_path}  ({len(list_lines)} 条)")
    print(f"    → {json_path}  ({len(json_data)} 条)")
    print(f"  按情感分文件夹:")
    for em in sorted(set(r.get("emotion_final") or r.get("emotion") or "neutral" for r in records)):
        count = sum(1 for r in records if (r.get("emotion_final") or r.get("emotion") or "neutral") == em)
        print(f"    {output_dir / em}/  ({count} 个文件)")
