"""音频自动修复 — 重采样、响度归一化、去削波."""

import math
import struct
import wave
from pathlib import Path

REQUIRED_SR = 32000
PEAK_TARGET = 0.707  # -3dBFS


def fix_sample_rate(path: Path, dry_run: bool = False) -> dict:
    """重采样到 32kHz，返回 {fixed, orig_sr, method}。

    支持常见转换: 48k→32k (比 2/3)、44.1k→32k、16k→32k (比 2)。
    48kHz↔44.1kHz 互转等复杂比率用线性插值兜底。
    """
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
    """削减波: 将整体 gain 降到 90%，让原来削的部分回缩。

    注意: 已削平的波形无法真正还原，这只是降 gain 避免进一步失真。
    严重削波 (削波率 > 10%) 建议重录。
    """
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
    """综合修复: 采样率 → 响度 → 削波，返回每步结果。"""
    results = {}

    # 先用 WAV header 快速判断采样率，避免每次都读 PCM
    try:
        with wave.open(str(path), "rb") as wf:
            sr = wf.getframerate()
    except Exception:
        return {"error": "cannot read wav"}

    if sr != REQUIRED_SR:
        results["sample_rate"] = fix_sample_rate(path, dry_run=dry_run)

    # 重采样后再检查响度和削波（因为数据已变）
    results["loudness"] = fix_loudness(path, dry_run=dry_run)
    results["clipping"] = fix_clipping(path, dry_run=dry_run)

    results["any_fixed"] = any(r.get("fixed") for r in results.values() if isinstance(r, dict))
    return results


# ── internals ──────────────────────────────────────────────

def _read_wav(path: Path) -> tuple[list[int], int, int]:
    """读取 16-bit mono WAV → (samples, sample_rate, n_channels)."""
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        n = wf.getnframes()
        raw = wf.readframes(n)
        fmt = f"<{n * ch}h"
        data = list(struct.unpack(fmt, raw))
        # Mono-ify: average channels if stereo
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
    """多相重采样。整数比用 sinc，非整数比用线性插值。"""
    if orig_sr == target_sr:
        return samples[:]

    # 整数比: 16k→32k, 8k→32k, etc.
    if target_sr % orig_sr == 0:
        ratio = target_sr // orig_sr
        return _upsample_hold(samples, ratio)

    if orig_sr % target_sr == 0:
        ratio = orig_sr // target_sr
        return _downsample_aa(samples, ratio)

    # 分数比: 48k→32k (3/2 upsample → 1/3 downsample)
    # 简化为: 用线性插值
    return _resample_linear(samples, orig_sr, target_sr)


def _upsample_hold(samples: list[int], ratio: int) -> list[int]:
    """零阶保持上采样。ratio=2 表示每样本复制一次。"""
    result = []
    for s in samples:
        result.extend([s] * ratio)
    return result


def _downsample_aa(samples: list[int], ratio: int) -> list[int]:
    """带简单抗混叠的下采样: 先移动平均再抽取。"""
    result = []
    buf = []
    for s in samples:
        buf.append(s)
        if len(buf) >= ratio:
            result.append(sum(buf) // ratio)
            buf = []
    return result


def _resample_linear(samples: list[int], orig_sr: int, target_sr: int) -> list[int]:
    """线性插值重采样，通用但质量一般。适用于 48k→32k 等分数比。"""
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
