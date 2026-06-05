"""日语文本质量过滤 — 过滤语气词/噪声/无意义短文本."""

import re

# 日语常见语气词/感叹词
JAPANESE_FILLERS = {
    "あっ", "あー", "ああ", "あの", "あのー", "あのう",
    "えっ", "えー", "ええ", "ええと", "えーと",
    "うん", "うーん", "ううん",
    "おっ", "おー", "おお",
    "はっ", "はー", "はあ", "はい",
    "へー", "へえ",
    "ふーん", "ふむ",
    "まあ", "まー",
    "やっ", "やー",
    "わっ", "わー",
    "んー", "んっ",
    "いや", "いやいや",
    "あら", "あれ",
    "さあ", "さー",
    "ねえ", "ねー",
    "ちょっと", "ちょっ",
    "もしもし",
    "もし",
}

# 纯非语言声音（笑/哭/叹息等）
NON_VERBAL_PATTERNS = re.compile(
    r"^[\s\(\)（）\(（\)）\*＊\-…\.。、,，!！?？～~♪♫]+$"
)

# 日语必须包含至少一个假名或汉字
HAS_JAPANESE_CHAR = re.compile(r"[぀-ゟ゠-ヿ一-鿿㐀-䶿]")


def is_filler_only(text: str) -> bool:
    """检查文本是否仅包含语气词。"""
    # Remove punctuation and whitespace
    cleaned = re.sub(r"[\s\-\…\.。、,，!！?？～~♪♫「」『』（）\(\)（）]+", "", text)
    if not cleaned:
        return True
    return cleaned in JAPANESE_FILLERS


def is_non_verbal(text: str) -> bool:
    """检查文本是否全是非语言符号（括号、星号等，通常是标注/注释）。"""
    return bool(NON_VERBAL_PATTERNS.match(text))


def has_meaningful_content(text: str) -> bool:
    """检查文本是否包含日语字符（假名或汉字）。"""
    return bool(HAS_JAPANESE_CHAR.search(text))


def should_filter(text: str, min_chars: int = 2) -> tuple[bool, str]:
    """综合判断：返回 (是否应过滤, 原因)。

    过滤条件：
    1. 空文本或过短
    2. 纯非语言符号
    3. 不包含任何假名/汉字
    4. 仅语气词
    """
    if not text or not text.strip():
        return True, "empty"

    stripped = text.strip()

    if is_non_verbal(stripped):
        return True, "non_verbal"

    if not has_meaningful_content(stripped):
        return True, "no_japanese"

    # Remove punctuation for length check
    cleaned = re.sub(r"[\s\-\…\.。、,，!！?？～~♪♫「」『』（）\(\)（）]+", "", stripped)
    if len(cleaned) < min_chars:
        return True, f"too_short({len(cleaned)} chars)"

    if is_filler_only(stripped):
        return True, "filler_only"

    return False, ""
