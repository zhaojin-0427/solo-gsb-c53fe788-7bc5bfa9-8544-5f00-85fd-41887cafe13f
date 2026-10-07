"""术语一致性审查：读取 UTF-8 CSV 词表（两列：规范写法 / 禁用变体），
在每条字幕的**当前可见文案**（修订稿优先，否则导入可见文本）中查找命中的
禁用变体，给出推荐写法与**从 0 起、按 UAX #29 扩展字素簇计**的原文半开
字素区间 [start, end)。审查只读，不自动修改文案。

匹配规则
========

- 变体中**含拉丁字母**（``\\p{Latin}``）时忽略大小写：原文按逐字素簇
  ``str.casefold`` 折叠后与折叠变体比较；且只有命中区间**两端相邻字素的
  首字符都不是拉丁字母或数字**时才算命中（避免 ``abc`` 命中 ``xabc`` /
  ``abc1``）。
- 其余变体（纯中文、纯数字、emoji 等）按**字面**命中，不做大小写折叠，
  也不加词边界限制（``电脑`` 命中 ``1电脑2``）。
- ``casefold`` 可能改变字符数（如 ``İ→i+◌̇``、``ﬁ→fi``、``ß→ss``）：
  折叠按**字素簇**逐簇进行并建立「折叠字符偏移 → 原文字素簇下标」映射，
  命中区间始终映射回原文字素簇，不因折叠长度变化产生定位偏移。

重叠命中的择优（同一 cue 内）
=============================

收集全部候选命中后按 **(字素长度降序, 起点升序, CSV 数据行序升序)** 排序，
贪心选取互不重叠（半开区间端点相贴不算重叠）的命中——同一区间只会告警
一次，不会重复。

词表校验
========

``parse_glossary`` 对下列情况抛 :class:`GlossaryError`（路由层转 400）：

- 非 UTF-8 编码、CSV 结构非法、首行不是 ``规范写法,禁用变体`` 两列表头、
  数据行列数不为 2；
- 空表（表头后没有任何词条）；
- 空字段（规范写法或禁用变体去首尾空白后为空；整行全空视为空行跳过）；
- 折叠后同一禁用变体对应了不同的规范写法（冲突）。折叠键相同且规范写法
  也相同的重复行以先出现的一行为准（不报错、不重复告警）。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

import regex

# UAX #29 扩展字素簇（与 subtitles.count_graphemes 同一套切分）
_GRAPHEME_RE = regex.compile(r"\X")
_LATIN_RE = regex.compile(r"\p{Latin}")
_DIGIT_RE = regex.compile(r"\d")

HEADER_CANONICAL = "规范写法"
HEADER_VARIANT = "禁用变体"

# 词表规模保护：真实术语表（数百至数千行）秒级完成，超限提示拆分。
MAX_ENTRIES = 10000


class GlossaryError(ValueError):
    """词表无法用于审查（编码 / 结构 / 表头 / 空表 / 空字段 / 冲突）。"""


@dataclass(frozen=True)
class Entry:
    canonical: str    # 规范写法（CSV 原值，去首尾空白）
    variant: str      # 禁用变体（CSV 原值，去首尾空白）
    row: int          # CSV 数据行序（1 起，不含表头），重叠并列时择优
    latin: bool       # 变体是否含拉丁字母（决定折叠匹配 / 字面匹配）
    key: str          # 匹配键：拉丁项为 casefold，其余为变体字面


@dataclass(frozen=True)
class Hit:
    cue_ord: int
    cue_id: str
    start: int                 # 起始字素簇下标（从 0 起，含）
    end: int                   # 结束字素簇下标（不含）
    matched: str               # 原文中实际命中的字素片段
    canonical: str             # 推荐写法
    csv_row: int


def _is_blank_row(fields: list[str]) -> bool:
    return all(f.strip() == "" for f in fields)


def parse_glossary(content: bytes) -> list[Entry]:
    """解析并校验 UTF-8 CSV 词表，返回按数据行序排列的词条列表。"""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GlossaryError(f"词表必须是 UTF-8 编码的 CSV：{exc}") from None
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise GlossaryError(f"CSV 解析失败：{exc}") from None

    # 找表头（跳过前面的整空行）
    idx = 0
    while idx < len(rows) and _is_blank_row(rows[idx]):
        idx += 1
    if idx >= len(rows):
        raise GlossaryError("空表：CSV 中没有任何词条，至少需要表头与一行词条")
    header = [f.strip() for f in rows[idx]]
    if header != [HEADER_CANONICAL, HEADER_VARIANT]:
        raise GlossaryError(
            "词表表头必须恰为两列：规范写法,禁用变体（按此顺序，UTF-8 CSV）")
    idx += 1

    entries: list[Entry] = []
    seen: dict[tuple[bool, str], Entry] = {}
    data_row = 0
    for raw in rows[idx:]:
        if not raw or _is_blank_row(raw):
            continue  # 整空行（含尾随空行）跳过
        data_row += 1
        if len(raw) != 2:
            raise GlossaryError(
                f"第 {data_row} 行：每行必须恰为两列（规范写法 / 禁用变体），"
                f"实际为 {len(raw)} 列")
        canonical, variant = raw[0].strip(), raw[1].strip()
        if canonical == "" or variant == "":
            raise GlossaryError(
                f"第 {data_row} 行：规范写法与禁用变体均不能为空")
        latin = _LATIN_RE.search(variant) is not None
        key = variant.casefold() if latin else variant
        dup = seen.get((latin, key))
        if dup is not None and dup.canonical != canonical:
            raise GlossaryError(
                f"第 {data_row} 行：禁用变体「{variant}」折叠后与第 {dup.row} 行"
                f"的变体相同，却对应冲突的规范写法"
                f"「{dup.canonical}」/「{canonical}」")
        if dup is None:
            entry = Entry(canonical=canonical, variant=variant, row=data_row,
                          latin=latin, key=key)
            seen[(latin, key)] = entry
            entries.append(entry)
        if len(entries) > MAX_ENTRIES:
            raise GlossaryError(f"词表词条过多（>{MAX_ENTRIES}），请拆分后重试")
    if not entries:
        raise GlossaryError("空表：表头之后没有任何有效词条")
    return entries


def _fold_clusters(clusters: list[str]) -> tuple[str, list[int]]:
    """逐字素簇折叠，返回 (折叠后字符串, 折叠字符偏移 → 字素簇下标)。

    每个折叠字符位置都映射到它所属的原字素簇——casefold 使某些字素
    （İ/ﬁ/ß…）折叠为多个字符时，这些字符位置全部指向同一原字素簇，
    从而把折叠串上的命中精确还原为原文字素区间，不产生偏移。
    """
    parts: list[str] = []
    pos2cluster: list[int] = []
    for i, g in enumerate(clusters):
        folded = g.casefold()
        # casefold 只会 1→n 映射；防御空串（理论上不发生），空则跳过该簇
        # 在折叠串中的位置（不改变其他簇的归属）。
        parts.append(folded)
        pos2cluster.extend([i] * len(folded))
    return "".join(parts), pos2cluster


def _latin_boundary_ok(clusters: list[str], i0: int, i1: int) -> bool:
    """拉丁变体的词边界：命中区间两端相邻字素的首字符都不是拉丁字母/数字。

    判定基于**原文相邻字素簇**而非折叠串字符：折叠不改变相邻关系，按
    原文字素判定与「两端不是拉丁字母或数字」的字素级语义一致。
    """
    if i0 > 0:
        ch = clusters[i0 - 1][0]
        if _LATIN_RE.match(ch) or _DIGIT_RE.match(ch):
            return False
    if i1 < len(clusters):
        ch = clusters[i1][0]
        if _LATIN_RE.match(ch) or _DIGIT_RE.match(ch):
            return False
    return True


def _candidates_for_cue(
    clusters: list[str],
    folded: str,
    pos2cluster: list[int],
    char2cluster: list[int],
    entry: Entry,
) -> list[tuple[int, int]]:
    """一条词条在一条 cue 内的全部候选命中（可能彼此重叠，交由统一择优）。"""
    ranges: list[tuple[int, int]] = []
    n = len(clusters)
    if entry.latin:
        needle = entry.key
        if not needle:
            return ranges
        start = 0
        while True:
            p = folded.find(needle, start)
            if p < 0:
                break
            q = p + len(needle) - 1
            i0 = pos2cluster[p]
            i1 = pos2cluster[q] + 1
            if _latin_boundary_ok(clusters, i0, i1):
                ranges.append((i0, i1))
            start = p + 1
    else:
        original = "".join(clusters)
        needle = entry.key
        start = 0
        while True:
            p = original.find(needle, start)
            if p < 0:
                break
            i0 = char2cluster[p]
            # 末字符所属字素簇 +1 = 半开字素区间的右端
            i1 = char2cluster[p + len(needle) - 1] + 1
            if i0 < n and i1 <= n:
                ranges.append((i0, i1))
            start = p + 1
    return ranges


def review_cue(ord_: int, cue_id: str, text: str,
               entries: list[Entry]) -> list[Hit]:
    """对一条当前文案做术语审查，返回择优去重后的命中（按起点排序）。"""
    clusters = _GRAPHEME_RE.findall(text)
    if not clusters or not entries:
        return []
    folded, pos2cluster = _fold_clusters(clusters)
    original = "".join(clusters)
    # 原文字符偏移 → 字素簇下标（字面命中用）
    char2cluster: list[int] = []
    for i, g in enumerate(clusters):
        char2cluster.extend([i] * len(g))

    candidates: list[tuple[int, int, int, Entry]] = []  # (i0, i1, 长度反序用, entry)
    for entry in entries:
        for i0, i1 in _candidates_for_cue(
                clusters, folded, pos2cluster, char2cluster, entry):
            candidates.append((i0, i1, i1 - i0, entry))

    # 择优：字素长度降序 → 起点升序 → CSV 行序升序；贪心保留互不重叠者。
    candidates.sort(key=lambda c: (-c[2], c[0], c[3].row))
    chosen: list[tuple[int, int, Entry]] = []
    for i0, i1, _length, entry in candidates:
        if any(i0 < j1 and j0 < i1 for j0, j1, _ in chosen):
            continue  # 与已择优的命中重叠（含同区间的重复告警）→ 丢弃
        chosen.append((i0, i1, entry))

    hits = []
    for i0, i1, entry in sorted(chosen, key=lambda h: (h[0], h[1])):
        hits.append(Hit(
            cue_ord=ord_, cue_id=cue_id, start=i0, end=i1,
            matched="".join(clusters[i0:i1]),
            canonical=entry.canonical, csv_row=entry.row,
        ))
    return hits


def review_texts(cues: list[tuple[int, str, str]],
                 entries: list[Entry]) -> list[dict]:
    """对多条 cue（``(ord, cue_id, 当前文案)``）执行审查，返回扁平化、
    按 cue 顺序与起点排序的命中字典列表。无命中的 cue 不出现在结果中。"""
    hits: list[Hit] = []
    for ord_, cue_id, text in cues:
        hits.extend(review_cue(ord_, cue_id, text, entries))
    hits.sort(key=lambda h: (h.cue_ord, h.start, h.csv_row))
    return [{
        "cue_ord": h.cue_ord,
        "cue_id": h.cue_id,
        "start_grapheme": h.start,
        "end_grapheme": h.end,
        "matched": h.matched,
        "recommended": h.canonical,
        "csv_row": h.csv_row,
    } for h in hits]
