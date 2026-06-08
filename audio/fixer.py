"""音频自动修复 — 重采样、响度归一化、去削波。

WAV 文件用内置 wave 处理，OGG/MP3/FLAC 等用 ffmpeg 转码。
"""

import math
import os
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

REQUIRED_SR = 32000
PEAK_TARGET = 0.707  # -3dBFS


def fix_sample_rate(path: Path, dry_run: bool = False) -> dict:
    """重采样到 32kHz，返回 {fixed, orig_sr, method}。"""
    samples, orig_sr, _ = _read_wav(path)

    if orig_sr == REQUIRED_SR:
        return {"fixed": False, "orig_sr": orig_sr, "method": "already_32k"}

    fixed = _resample(samples, orig_sr, REQUIRED_SR)

    if not dry_run:
        _write_wav(path, fixed, REQUIRED_SR)

    method = f"{orig_sr}→{REQUIRED_SR}"
    return {"fixed": True, "orig_sr": orig_sr, "method": method}


def fix_loudness(path: Path, dry_run: bool = False) -> dict:
    """峰值归一化到 -3dBFS，返回 {fixed, peak_before_db, peak_after_db}。"""
    samples, sr, _ = _read_wav(path)

    max_val = max(abs(s) for s in samples) if samples else 1
    if max_val == 0:
        return {"fixed": False, "peak_before_db": -96, "reason": "silent"}

    peak_before_db = 20 * math.log10(max_val / 32767)
    gain = (PEAK_TARGET * 32767) / max_val

    if 0.95 <= gain <= 1.05:
        return {"fixed": False, "peak_before_db": round(peak_before_db, 1), "reason": "already_ok"}

    fixed = [int(max(-32768, min(32767, s * gain))) for s in samples]

    if not dry_run:
        _write_wav(path, fixed, sr)

    new_max = max(abs(s) for s in fixed)
    peak_after_db = 20 * math.log10(new_max / 32767)
    return {"fixed": True, "peak_before_db": round(peak_before_db, 1), "peak_after_db": round(peak_after_db, 1)}


def fix_clipping(path: Path, dry_run: bool = False) -> dict:
    """削减波: 将整体 gain 降到 90%，让原来削的部分回缩。"""
    samples, sr, _ = _read_wav(path)

    max_val = max(abs(s) for s in samples)
    if max_val < 32000:
        return {"fixed": False, "reason": "no_clipping_detected"}

    gain = (0.9 * 32767) / max_val
    fixed = [int(s * gain) for s in samples]

    if not dry_run:
        _write_wav(path, fixed, sr)

    return {"fixed": True, "gain_applied": round(gain, 3)}


def auto_fix(path: Path, dry_run: bool = False) -> dict:
    """综合修复。

    WAV 文件: 内置 wave 处理 → 重采样 + 归一化 + 去削波。
    非 WAV 文件 (ogg/mp3/flac): 用 ffmpeg 转 32kHz/16bit/mono WAV，
    替换原文件（原文件备份为 .bak）。
    """
    suffix = path.suffix.lower()
    if suffix == ".wav":
        return _auto_fix_wav(path, dry_run)
    else:
        return _auto_fix_ffmpeg(path, dry_run)


# ── WAV path ──────────────────────────────────────────────────

def _auto_fix_wav(path: Path, dry_run: bool = False) -> dict:
    results = {}
    try:
        with wave.open(str(path), "rb") as wf:
            sr = wf.getframerate()
    except Exception:
        return {"error": "cannot read wav"}

    if sr != REQUIRED_SR:
        results["sample_rate"] = fix_sample_rate(path, dry_run=dry_run)

    results["loudness"] = fix_loudness(path, dry_run=dry_run)
    results["clipping"] = fix_clipping(path, dry_run=dry_run)
    results["any_fixed"] = any(r.get("fixed") for r in results.values() if isinstance(r, dict))
    return results


# ── FFmpeg path (non-WAV) ─────────────────────────────────────

def _auto_fix_ffmpeg(path: Path, dry_run: bool = False) -> dict:
    """用 ffmpeg 将非 WAV 音频转为 32kHz 16-bit mono WAV。

    替换原文件: .ogg → .wav，原文件备份为 .bak。
    """
    if dry_run:
        # Check what ffmpeg would do
        info = _ffprobe_info(path)
        orig_sr = info.get("sample_rate", 0)
        orig_fmt = info.get("format", path.suffix)
        needs_fix = (orig_sr != REQUIRED_SR) or (info.get("channels", 1) > 1)
        return {
            "ffmpeg_convert": {
                "fixed": needs_fix,
                "orig_sr": orig_sr,
                "orig_format": orig_fmt,
                "target_sr": REQUIRED_SR,
                "target_format": "wav",
            },
            "any_fixed": needs_fix,
        }

    # Build output path: replace extension with .wav
    out_path = path.with_suffix(".wav")
    if out_path.exists() and out_path.samefile(path) if False else False:
        # Shouldn't happen since suffixes differ
        pass

    bak_path = path.with_suffix(path.suffix + ".bak")

    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", str(path),
        "-ar", str(REQUIRED_SR),
        "-ac", "1",
        "-sample_fmt", "s16",
        str(out_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return {"error": f"ffmpeg failed: {result.stderr.strip()[:200]}"}

        # Rename original to .bak
        if bak_path.exists():
            bak_path.unlink()
        path.rename(bak_path)

        # Move .wav to original location had it been .wav
        # Actually out_path is already at the right location with .wav extension
        # The file_path in DB still points to .ogg though — caller must update

        # Get info
        info = _ffprobe_info(out_path)
        return {
            "ffmpeg_convert": {
                "fixed": True,
                "orig_format": path.suffix,
                "orig_path": str(path),
                "new_path": str(out_path),
                "backup_path": str(bak_path),
                "target_sr": REQUIRED_SR,
                "target_format": "wav",
            },
            "any_fixed": True,
        }
    except FileNotFoundError:
        return {"error": "ffmpeg 未安装。请安装 ffmpeg 并确保在 PATH 中。"}
    except Exception as e:
        return {"error": f"ffmpeg error: {str(e)}"}


def _ffprobe_info(path: Path) -> dict:
    """用 ffprobe 获取音频流信息。"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "a:0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return {}
        import json
        info = json.loads(result.stdout)
        streams = info.get("streams", [])
        if not streams:
            return {}
        s = streams[0]
        return {
            "sample_rate": int(s.get("sample_rate", 0)),
            "channels": int(s.get("channels", 1)),
            "format": str(s.get("codec_name", "")),
        }
    except Exception:
        return {}


# ── internals ──────────────────────────────────────────────────

def _read_wav(path: Path) -> tuple[list[int], int, int]:
    """读取 16-bit mono WAV → (samples, sample_rate, n_channels)."""
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        n = wf.getnframes()
        raw = wf.readframes(n)
        fmt = f"<{n * ch}h"
        data = list(struct.unpack(fmt, raw))
        if ch > 1:
            samples = [sum(data[i : i + ch]) // ch for i in range(0, len(data), ch)]
        else:
            samples = data
    return samples, sr, ch


def _write_wav(path: Path, samples: list[int], sr: int):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        raw = struct.pack(f"<{len(samples)}h", *samples)
        wf.writeframes(raw)


def _resample(samples: list[int], orig_sr: int, target_sr: int) -> list[int]:
    if orig_sr == target_sr:
        return samples[:]

    if target_sr % orig_sr == 0:
        return _upsample_hold(samples, target_sr // orig_sr)

    if orig_sr % target_sr == 0:
        return _downsample_aa(samples, orig_sr // target_sr)

    return _resample_linear(samples, orig_sr, target_sr)


def _upsample_hold(samples: list[int], ratio: int) -> list[int]:
    result = []
    for s in samples:
        result.extend([s] * ratio)
    return result


def _downsample_aa(samples: list[int], ratio: int) -> list[int]:
    result = []
    buf = []
    for s in samples:
        buf.append(s)
        if len(buf) >= ratio:
            result.append(sum(buf) // ratio)
            buf = []
    return result


def _resample_linear(samples: list[int], orig_sr: int, target_sr: int) -> list[int]:
    n = len(samples)
    if n < 2:
        return samples[:]
    out_len = int(n * target_sr / orig_sr)
    result = []
    for i in range(out_len):
        pos = i * (n - 1) / (out_len - 1) if out_len > 1 else 0
        idx = int(pos)
        frac = pos - idx
        if idx >= n - 1:
            result.append(samples[-1])
        else:
            result.append(int(samples[idx] * (1 - frac) + samples[idx + 1] * frac))
    return result
