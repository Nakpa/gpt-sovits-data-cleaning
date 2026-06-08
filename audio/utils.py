"""音频文件校验 — 采样率、削波、静音检测."""

import json
import struct
import subprocess
import wave
from pathlib import Path
from typing import Optional

AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma"}

# GPT-SoVITS v2-pro-plus 要求
REQUIRED_SAMPLE_RATE = 32000

# 削波阈值: 样本值达到最大值的 98% 即判定为削波
CLIP_THRESHOLD = 0.98

# 静音阈值: RMS 低于此值判定为静音 (-40dBFS ≈ 1% of max)
SILENCE_RMS_THRESHOLD = 0.01
# 静音帧占比超过此比例则判定为"废片"
SILENCE_RATIO_REJECT = 0.8


def is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS


def get_audio_duration_ms(path: Path) -> Optional[float]:
    """Get duration of an audio file in milliseconds.

    Uses wave module for WAV files, ffprobe as universal fallback.
    """
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                if rate > 0:
                    return (frames / rate) * 1000
        except Exception:
            pass  # fall through to ffprobe

    # ffprobe fallback for all formats
    return _ffprobe_duration_ms(path)


def _ffprobe_duration_ms(path: Path) -> Optional[float]:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        info = json.loads(result.stdout)
        duration_s = float(info.get("format", {}).get("duration", 0))
        return duration_s * 1000 if duration_s > 0 else None
    except Exception:
        return None


def _read_pcm(path: Path) -> tuple[int, list[int]]:
    """读取 16-bit mono WAV，返回 (sample_rate, samples)。"""
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        raw = wf.readframes(n)
        samples = list(struct.unpack(f"<{n}h", raw))
    return sr, samples


def check_sample_rate(path: Path) -> dict:
    """检查采样率是否为 32kHz。返回 {ok, actual, required, message}。"""
    try:
        sr, _ = _read_pcm(path)
        ok = sr == REQUIRED_SAMPLE_RATE
        return {
            "check": "sample_rate",
            "ok": ok,
            "actual": sr,
            "required": REQUIRED_SAMPLE_RATE,
            "message": "" if ok else f"采样率 {sr}Hz (需要 {REQUIRED_SAMPLE_RATE}Hz)",
        }
    except Exception as e:
        return {"check": "sample_rate", "ok": False, "actual": None, "required": REQUIRED_SAMPLE_RATE, "message": str(e)}


def detect_clipping(path: Path) -> dict:
    """检测削波。返回 {ok, clip_ratio, clip_samples, message}。"""
    try:
        _, samples = _read_pcm(path)
        max_val = 32767
        threshold = int(max_val * CLIP_THRESHOLD)
        clip_count = sum(1 for s in samples if abs(s) >= threshold)
        ratio = clip_count / len(samples) if samples else 0
        ok = ratio < 0.01  # 少于 1% 样本削波即通过
        return {
            "check": "clipping",
            "ok": ok,
            "clip_ratio": round(ratio, 4),
            "clip_samples": clip_count,
            "message": "" if ok else f"削波率 {ratio:.1%} ({clip_count}/{len(samples)} 样本)",
        }
    except Exception as e:
        return {"check": "clipping", "ok": True, "clip_ratio": 0, "clip_samples": 0, "message": str(e)}


def detect_silence(path: Path) -> dict:
    """检测静音/低能量。返回 {ok, rms, rms_db, silent_ratio, message}。"""
    try:
        _, samples = _read_pcm(path)
        max_val = 32767.0
        n = len(samples)

        # RMS
        rms = (sum(s**2 for s in samples) / n) ** 0.5 if n else 0
        rms_normalized = rms / max_val
        rms_db = 20 * (rms_normalized ** 0) if rms_normalized else -96
        # Correct dB calculation
        import math
        rms_db = 20 * math.log10(max(rms_normalized, 1e-10))

        # 静音帧占比
        silent = sum(1 for s in samples if abs(s) < max_val * 0.02)
        silent_ratio = silent / n if n else 1.0

        ok = rms_normalized >= SILENCE_RMS_THRESHOLD and silent_ratio < SILENCE_RATIO_REJECT
        msg_parts = []
        if rms_normalized < SILENCE_RMS_THRESHOLD:
            msg_parts.append(f"RMS 过低 ({rms_db:.0f} dB)")
        if silent_ratio >= SILENCE_RATIO_REJECT:
            msg_parts.append(f"静音占比过高 ({silent_ratio:.0%})")

        return {
            "check": "silence",
            "ok": ok,
            "rms": round(rms, 1),
            "rms_db": round(rms_db, 1),
            "silent_ratio": round(silent_ratio, 4),
            "message": "; ".join(msg_parts),
        }
    except Exception as e:
        return {"check": "silence", "ok": True, "rms": 0, "rms_db": 0, "silent_ratio": 0, "message": str(e)}


def validate_audio_quality(path: Path) -> dict:
    """综合音频质量检查。返回 {passed, checks: [...], issues: [...]}。"""
    checks = [
        check_sample_rate(path),
        detect_clipping(path),
        detect_silence(path),
    ]
    issues = [c["message"] for c in checks if not c["ok"] and c["message"]]
    return {
        "passed": all(c["ok"] for c in checks),
        "checks": checks,
        "issues": issues,
    }
