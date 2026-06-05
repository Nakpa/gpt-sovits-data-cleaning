"""日语文本归一化 — 统一字符宽度、清理特殊符号."""

import re
import unicodedata


def normalize_japanese(text: str) -> str:
    """日语文本归一化，返回 (normalized_text, changes)。

    规则:
    1. 半角カナ → 全角カナ (ｱｲｳ → アイウ)
    2. 全角数字 → 半角数字 (１２３ → 123)
    3. 全角英字 → 半角英字 (ＡＢＣ → ABC)
    4. 多个空白字符 → 单个空格
    5. 删除控制字符和零宽字符
    6. 删除首尾空白
    """
    original = text
    changes = []

    # 1. NFKC 归一化 (处理大部分全角/半角转换)
    # 但 NFKC 会把全角カナ转成半角，日语场景需要反着来
    # 所以先做自定义转换
    text = _normalize_kana(text, changes)
    text = _normalize_digits_ascii(text, changes)
    text = _normalize_whitespace(text, changes)
    text = _strip_control_chars(text, changes)
    text = text.strip()

    if text != original:
        changes.append("chars_normalized")

    return text


_HALF_TO_FULL_KATA = str.maketrans(
    "ｦｧｨｩｪｫｬｭｮｯｰｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝﾞﾟ",
    "ヲァィゥェォャュョッーアイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワン゙゚",
)


def _normalize_kana(text: str, changes: list[str]) -> str:
    """半角カナ → 全角カナ。"""
    result = text.translate(_HALF_TO_FULL_KATA)
    if result != text:
        changes.append("kana_half_to_full")
    return result


_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_FULLWIDTH_ALPHA_UPPER = str.maketrans(
    "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
)
_FULLWIDTH_ALPHA_LOWER = str.maketrans(
    "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
    "abcdefghijklmnopqrstuvwxyz",
)


def _normalize_digits_ascii(text: str, changes: list[str]) -> str:
    """全角数字/英字 → 半角。"""
    result = text.translate(_FULLWIDTH_DIGITS)
    result = result.translate(_FULLWIDTH_ALPHA_UPPER)
    result = result.translate(_FULLWIDTH_ALPHA_LOWER)
    if result != text:
        changes.append("fw_digit_alpha_to_hw")
    return result


def _normalize_whitespace(text: str, changes: list[str]) -> str:
    """多个空白字符 → 单个空格，制表符/换行 → 空格。"""
    result = re.sub(r"[\t\r\n]+", " ", text)
    result = re.sub(r" {2,}", " ", result)
    if result != text:
        changes.append("whitespace")
    return result


def _strip_control_chars(text: str, changes: list[str]) -> str:
    """删除 Unicode 控制字符和零宽字符。"""
    cleaned = []
    for ch in text:
        cat = unicodedata.category(ch)
        # Cc: control, Cf: format (zero-width, BOM, etc.), Co: private use
        # Keep: Cc中的 \n \t 已经在前面处理了
        if cat == "Cc":
            continue  # 控制字符直接删
        if cat == "Cf":
            continue  # 零宽空格、BOM 等
        if 0xFE00 <= ord(ch) <= 0xFE0F:
            continue  # 变体选择器 (emoji modifiers)
        cleaned.append(ch)
    result = "".join(cleaned)
    if result != text:
        changes.append("control_chars_removed")
    return result
