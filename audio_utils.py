"""音频文件校验工具."""

import wave
from pathlib import Path
from typing import Optional

AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"}


def is_audio_file(path: Path) -> bool:
    """通过扩展名判断是否为支持的音频格式。"""
    return path.suffix.lower() in AUDIO_EXTENSIONS


def get_wav_duration_ms(path: Path) -> Optional[float]:
    """获取 WAV 文件时长（毫秒）。非 WAV 格式返回 None（需要用 ffprobe 等工具）。"""
    if path.suffix.lower() != ".wav":
        return None
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate == 0:
                return None
            return (frames / rate) * 1000
    except Exception:
        return None


def get_audio_info(path: Path) -> dict:
    """获取音频基本信息。"""
    info = {
        "file_path": str(path),
        "file_name": path.name,
        "size_bytes": path.stat().st_size,
        "duration_ms": None,
        "sample_rate": None,
    }
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                info["sample_rate"] = rate
                if rate > 0:
                    info["duration_ms"] = (frames / rate) * 1000
        except Exception:
            pass
    return info
