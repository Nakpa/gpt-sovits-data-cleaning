"""qwen3-asr-flash API 封装.

DashScope MultiModalConversation，通过 base64 编码传入本地音频。
"""

import base64
import os
import sys
from pathlib import Path
from typing import Optional

EMOTION_LABELS = {
    "surprised", "neutral", "happy", "sad",
    "disgusted", "angry", "fearful",
}

_MIME_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
}


def _encode_audio(file_path: Path) -> str:
    """将音频文件编码为 base64 data URI。"""
    mime = _MIME_TYPES.get(file_path.suffix.lower(), "audio/wav")
    data = file_path.read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def call_asr(file_path: Path, language_hint: Optional[str] = None) -> dict:
    """调用 qwen3-asr-flash，返回 {text, emotion, language, confidence, raw_response}。

    通过 base64 data URI 传入音频，避免 file:// 协议在 Windows 上的路径兼容问题。
    """
    try:
        import dashscope
    except ImportError:
        sys.exit(
            "dashscope 未安装。请运行: pip install dashscope\n"
            "并设置环境变量: DASHSCOPE_API_KEY=sk-xxx"
        )

    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        sys.exit("请设置环境变量 DASHSCOPE_API_KEY，从 https://bailian.console.aliyun.com 获取。")

    asr_options = {"enable_itn": False}
    if language_hint:
        asr_options["language"] = language_hint

    data_uri = _encode_audio(file_path)

    response = dashscope.MultiModalConversation.call(
        api_key=api_key,
        model="qwen3-asr-flash",
        messages=[{
            "role": "user",
            "content": [{"audio": data_uri}]
        }],
        result_format="message",
        asr_options=asr_options,
    )

    if response.status_code != 200:
        return {
            "text": "",
            "emotion": "neutral",
            "language": "",
            "confidence": 0.0,
            "raw_response": {"error": response.message, "code": response.status_code},
        }

    output = response.output
    if not output or not output.get("choices"):
        return {
            "text": "",
            "emotion": "neutral",
            "language": "",
            "confidence": 0.0,
            "raw_response": {"error": "no choices in response", "output": output},
        }

    choice = output["choices"][0]
    message = choice.get("message", {})

    # 提取文本
    content = message.get("content", [])
    text = ""
    if isinstance(content, list):
        text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    elif isinstance(content, str):
        text = content

    # 提取情感
    annotations = message.get("annotations", [])
    emotion = "neutral"
    language = ""
    if annotations and isinstance(annotations, list):
        ann = annotations[0]
        emotion = ann.get("emotion", "neutral") or "neutral"
        if emotion not in EMOTION_LABELS:
            emotion = "neutral"
        language = ann.get("language", "") or ""

    return {
        "text": text.strip(),
        "emotion": emotion,
        "language": language,
        "confidence": 0.9,
        "raw_response": response.output,
    }
