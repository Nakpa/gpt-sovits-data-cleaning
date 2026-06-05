"""状态持久化 — 记住上次使用的音频目录，避免每次都要输路径。"""

import json
from pathlib import Path
from typing import Optional

_STATE_FILE = Path(__file__).resolve().parent / ".gsc_state"


def save_audio_dir(audio_dir: Path):
    _STATE_FILE.write_text(json.dumps({"audio_dir": str(audio_dir.resolve())}), encoding="utf-8")


def load_audio_dir() -> Optional[Path]:
    if not _STATE_FILE.exists():
        return None
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        path = Path(data.get("audio_dir", ""))
        return path if path.exists() else None
    except Exception:
        return None
