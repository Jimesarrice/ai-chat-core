"""朗读前清洗：去掉 markdown/颜文字/emoji，只留能念出来的字。

规则移植自 dsh-tts-bridge 的「去颜文字」（四条正则），另加 markdown 清理。
⚠ 移植教训：清洗函数一定要有单测盯住——原文作者曾因丢了 `return s`
导致「一条都排不进队列」。
"""
from __future__ import annotations

import re

# 1) 括号内没有正常字（汉字/字母/数字）的整块 → 去掉
_BRACKET_JUNK = re.compile(r"[（(][^（()）]{0,30}[)）]")
# 2) 白名单外全清：汉字、CJK 标点（3000-303F）、全角（FF00-FFEF）、字母数字、常用西文标点
_NON_SPEAKABLE = re.compile(
    r"[^\u4e00-\u9fa5\u3000-\u303F\uFF00-\uFFEFA-Za-z0-9\s.,!?;:'\"()\[\]…—－-]+"
)
# 3) ASCII 颜文字
_KAOMOJI = [
    re.compile(r"[（(]?\s*[>＞<＜^vV][\-_^.,;:·]{1,3}[>＜<^vV]\s*[)）]?"),
    re.compile(r"[（(]?\s*[TToO0xX][\-_^.,;:·]{1,3}[TToO0xX]\s*[)）]?"),
    re.compile(r"\borz\b", re.I),
    re.compile(r"[:;=8][-o*']?[)(\[\]dDpP/\\|{}]"),
    re.compile(r"[（(]\s*[)）]"),
]
# 4) 收尾空格式
_SPACE_BEFORE_PUNCT = re.compile(r" ([，。！？、；：）】》])")
_SPACE_AFTER_OPEN = re.compile(r"([（【《]) ")
# 5) markdown 清理
_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_EMPHASIS = re.compile(r"(\*\*|__|\*|~~)")
_MD_HEADING = re.compile(r"^#{1,6}\s*", re.M)
_MD_QUOTE = re.compile(r"^\s*>\s?", re.M)
_MD_LIST = re.compile(r"^\s*[-*+]\s+", re.M)
_URL = re.compile(r"https?://\S+")


def clean_for_tts(text: str) -> str:
    s = str(text or "")
    # markdown 结构
    s = _CODE_BLOCK.sub(" ", s)
    s = _INLINE_CODE.sub(r"\1", s)
    s = _MD_IMAGE.sub(" ", s)
    s = _MD_LINK.sub(r"\1", s)
    s = _URL.sub(" ", s)
    s = _MD_HEADING.sub("", s)
    s = _MD_QUOTE.sub("", s)
    s = _MD_LIST.sub("", s)
    s = _MD_EMPHASIS.sub("", s)
    # 颜文字（先整块括号，后字符级）
    s = _BRACKET_JUNK.sub(" ", s)
    s = _NON_SPEAKABLE.sub(" ", s)
    for rx in _KAOMOJI:
        s = rx.sub(" ", s)
    # 收尾空格
    s = _SPACE_BEFORE_PUNCT.sub(r"\1", s)
    s = _SPACE_AFTER_OPEN.sub(r"\1", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()