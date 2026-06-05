"""DeepSeek 文本情感分析 (可选模块).

对 ASR 转写的纯文本做语义层面的情感二次判断。
"""

import json
import os
from typing import Optional

_EMOTION_PROMPT = """\
あなたは日本語テキストの感情分析の専門家です。
以下の日本語テキストの感情を判定し、以下の7つの感情のいずれか1つを選んでください:
- neutral（普通）
- happy（嬉しい）
- sad（悲しい）
- angry（怒り）
- surprised（驚き）
- fearful（恐怖）
- disgusted（嫌悪）

JSON形式で返答してください。他の説明は一切不要です。
{"emotion": "選んだ感情", "confidence": 0.0~1.0}
判定するテキスト: {text}"""


def analyze_text_emotion(text: str,
                         api_key: Optional[str] = None,
                         base_url: str = "https://api.deepseek.com") -> dict:
    """DeepSeek 对文本做情感分析。

    Returns: {"emotion": str, "confidence": float}
    """
    from openai import OpenAI

    key = api_key or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        return {"emotion": "", "confidence": 0.0, "error": "DEEPSEEK_API_KEY not set"}

    client = OpenAI(api_key=key, base_url=base_url)

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{
            "role": "user",
            "content": _EMOTION_PROMPT.format(text=text),
        }],
        response_format={"type": "json_object"},
        temperature=0.1,
    )

    content = response.choices[0].message.content
    try:
        result = json.loads(content)
        return {
            "emotion": result.get("emotion", ""),
            "confidence": result.get("confidence", 0.0),
        }
    except (json.JSONDecodeError, KeyError):
        return {"emotion": "", "confidence": 0.0, "error": "parse failed"}


_VALID_EMOTIONS = {"neutral", "happy", "sad", "angry", "surprised", "fearful", "disgusted"}


def merge_emotions(audio_emotion: str, text_emotion: dict) -> dict:
    """融合 qwen 音频情感和 DeepSeek 文本情感。

    - 一致时直接采纳
    - 不一致时标注为需要人工审核
    """
    te = text_emotion.get("emotion", "")
    tc = text_emotion.get("confidence", 0.0)

    if not te:
        return {
            "source": "audio_only",
            "final": audio_emotion,
            "audio_emotion": audio_emotion,
            "text_emotion": None,
            "needs_review": False,
        }

    if te.lower() == audio_emotion.lower():
        return {
            "source": "both_agree",
            "final": audio_emotion,
            "audio_emotion": audio_emotion,
            "text_emotion": te,
            "text_confidence": tc,
            "needs_review": tc < 0.6,
        }

    # 不一致 → 标记为需要复查
    return {
        "source": "conflict",
        "final": audio_emotion,
        "audio_emotion": audio_emotion,
        "text_emotion": te,
        "text_confidence": tc,
        "needs_review": True,
    }
