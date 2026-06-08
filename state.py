"""状态持久化 — 记住已注册的音频目录列表。"""

import json
from pathlib import Path

_STATE_FILE = Path(__file__).resolve().parent / ".gsc_state"


def load_audio_dirs() -> list[Path]:
    """返回所有已注册的音频目录列表。兼容旧版单目录格式。"""
    if not _STATE_FILE.exists():
        return []
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        # 兼容旧格式 {"audio_dir": "/path"}
        if "audio_dirs" in data:
            dirs = [Path(d) for d in data["audio_dirs"]]
        elif "audio_dir" in data:
            dirs = [Path(data["audio_dir"])]
        else:
            return []
        return [d for d in dirs if d.exists()]
    except Exception:
        return []


def save_audio_dir(audio_dir: Path) -> bool:
    """注册一个音频目录（去重）。返回 True 表示新增，False 表示已存在。"""
    resolved = audio_dir.resolve()
    existing = load_audio_dirs()
    existing_strs = [str(d.resolve()) for d in existing]
    if str(resolved) in existing_strs:
        return False
    existing.append(resolved)
    _write(existing)
    return True


def remove_audio_dir(audio_dir: Path) -> bool:
    """取消注册一个音频目录。返回 True 表示已删除。"""
    resolved = audio_dir.resolve()
    existing = load_audio_dirs()
    new_list = [d for d in existing if d.resolve() != resolved]
    if len(new_list) == len(existing):
        return False
    _write(new_list)
    return True


def load_audio_dir():
    """兼容旧接口：返回第一个已注册目录或 None。CLI 模式仍用此接口。"""
    dirs = load_audio_dirs()
    return dirs[0] if dirs else None


def _write(dirs: list[Path]):
    _STATE_FILE.write_text(
        json.dumps({"audio_dirs": [str(d.resolve()) for d in dirs]}, ensure_ascii=False),
        encoding="utf-8",
    )
