"""术语一致性审查：规范写法 / 禁用变体词表解析与字幕文案命中扫描。

词表为 UTF-8 CSV，表头须含「规范写法」「禁用变体」两列（按列名定位，
允许存在其他列）。解析期错误（均拒绝整份词表，不产生任何报告）：

- 空表：没有任何数据行；
- 空字段：任一数据行的规范写法或禁用变体去空白后为空；
- 冲突：大小写折叠后同一变体对应了不同的规范写法（同一变体同一写法的
  重复行去重，只计一次）；
- 非法 CSV：非 UTF-8、缺必需列、数据行列数不足。

命中规则（对每条字幕的**当前可见文案**，修订稿优先）：

- 含拉丁字母（\\p{Latin}）的变体：忽略大小写（Unicode casefold）做子串
  匹配，且命中区间**两端相邻字素不得为拉丁字母或数字**（文本首/尾视为
  合法边界）——避免把 "app" 命中到 "apple"、"app2" 内部；
- 其余变体：按字面做子串匹配，无边界限制；
- 命中区间以 UAX #29 扩展字素簇计，从 0 起的半开区间 [start, end)。
  大小写折叠可能改变长度（如 ß→ss、ﬁ→fi）：逐字素折叠并保留
  「折叠串偏移 → 原文字素下标」映射，命中区间映射回原文，定位不偏移；
- 同一字幕内的重叠命中按（字素长度降序、起点升序、CSV 行序升序）择优，
  被挤掉的命中不再告警，避免重复告警与区间交叠。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

import regex

_GRAPHEME_RE = regex.compile(r"\X")
_LATIN_RE = regex.compile(r"\p{Latin}")
_LATIN_OR_DIGIT_RE = regex.compile(r"[\p{Latin}\p{Nd}]")

HEADER_CANONICAL = "规范写法"
HEADER_VARIANT = "禁用变体"


class TermsError(ValueError):
    """词表解析 / 校验失败（空表、空字段、冲突、非法 CSV）。"""


@dataclass(frozen=True)
class TermEntry:
    """一条去重后的词表项。row 为数据行序（从 1 起，重叠择优与展示用）。"""
    row: int
    canonical: str   # 规范写法（推荐写法）
    variant: str     # 禁用变体（原始写法）
    latin: bool      # 变体是否含拉丁字母
    needle: str      # 匹配用串：含拉丁字母 → casefold；否则原文


@dataclass(frozen=True)
class TermHit:
    """一次命中：原文字素区间 [start, end)（从 0 起，半开）。"""
    start: int
    end: int
    entry: TermEntry
    surface: str     # 命中片段（原文）


def parse_terms_csv(content: bytes) -> list[TermEntry]:
    """解析 UTF-8 词表 CSV，返回去重后的词表项（保持 CSV 行序）。

    任何解析 / 校验失败都抛 TermsError，调用方整体拒绝该份词表。
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TermsError(f"词表必须是 UTF-8 编码的 CSV（解码失败：{exc}）")
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise TermsError(f"词表 CSV 解析失败：{exc}")

    # 跳过全空行后，第一行须为含两个必需列的表头
    idx = 0
    while idx < len(rows) and all(not c.strip() for c in rows[idx]):
        idx += 1
    if idx >= len(rows):
        raise TermsError(
            f"词表为空：未找到「{HEADER_CANONICAL} / {HEADER_VARIANT}」表头")
    header = [c.strip() for c in rows[idx]]
    try:
        ci = header.index(HEADER_CANONICAL)
        vi = header.index(HEADER_VARIANT)
    except ValueError:
        raise TermsError(
            f"词表缺少必需列：表头须包含「{HEADER_CANONICAL}」与"
            f"「{HEADER_VARIANT}」两列")
    need = max(ci, vi) + 1

    entries: list[TermEntry] = []
    seen: dict[str, str] = {}   # 折叠后的变体 → 规范写法（冲突检测 + 去重）
    data_row = 0
    for raw in rows[idx + 1:]:
        if not raw or all(not c.strip() for c in raw):
            continue            # 全空行（如文件末尾空行）跳过
        if len(raw) < need:
            raise TermsError("词表存在列数不足的数据行（非法 CSV）")
        canonical = raw[ci].strip()
        variant = raw[vi].strip()
        if not canonical or not variant:
            raise TermsError(
                f"词表存在空字段：「{HEADER_CANONICAL}」与「{HEADER_VARIANT}」"
                "均不能为空")
        data_row += 1
        latin = bool(_LATIN_RE.search(variant))
        key = variant.casefold() if latin else variant
        prev = seen.get(key)
        if prev is not None:
            if prev != canonical:
                raise TermsError(
                    f"折叠后同一变体「{variant}」对应冲突的规范写法："
                    f"「{prev}」与「{canonical}」")
            continue            # 同一变体同一写法：去重，只计一次
        seen[key] = canonical
        entries.append(TermEntry(row=data_row, canonical=canonical,
                                 variant=variant, latin=latin, needle=key))
    if not entries:
        raise TermsError("词表为空：没有任何有效的数据行")
    return entries


def _boundary_ok(graphemes: list[str], start: int, end: int) -> bool:
    """含拉丁字母变体的边界约束：命中区间两端相邻字素不得为拉丁字母或数字
    （文本首 / 尾没有相邻字素，视为合法边界）。"""
    if start > 0 and _LATIN_OR_DIGIT_RE.search(graphemes[start - 1]):
        return False
    if end < len(graphemes) and _LATIN_OR_DIGIT_RE.search(graphemes[end]):
        return False
    return True


def scan_text(text: str, entries: list[TermEntry]) -> list[TermHit]:
    """在一条字幕的当前可见文案中扫描全部词表项，返回择优后的命中列表。

    区间按 UAX #29 字素簇计（从 0 起，半开）。含拉丁字母的变体在
    「逐字素折叠串」上匹配——折叠可能改变长度（ß→ss），折叠串的每个
    字符偏移都记录其来源字素，命中区间由此映射回原文，定位不偏移。
    重叠命中按（字素长度降序、起点升序、CSV 行序升序）择优。
    """
    graphemes = _GRAPHEME_RE.findall(text)
    # 原文字符偏移 → 字素下标（字面命中用）
    cmap: list[int] = []
    for gi, g in enumerate(graphemes):
        cmap.extend([gi] * len(g))
    # 折叠串字符偏移 → 字素下标（忽略大小写命中用）
    fparts: list[str] = []
    fmap: list[int] = []
    for gi, g in enumerate(graphemes):
        fg = g.casefold()
        fparts.append(fg)
        fmap.extend([gi] * len(fg))
    folded = "".join(fparts)

    candidates: list[TermHit] = []
    for e in entries:
        if not e.needle:
            continue
        hay, hmap = (folded, fmap) if e.latin else (text, cmap)
        pos = 0
        while True:
            i = hay.find(e.needle, pos)
            if i < 0:
                break
            j = i + len(e.needle)
            gs_i, gs_j = hmap[i], hmap[j - 1] + 1
            pos = i + 1
            if e.latin and not _boundary_ok(graphemes, gs_i, gs_j):
                continue
            candidates.append(TermHit(
                start=gs_i, end=gs_j, entry=e,
                surface="".join(graphemes[gs_i:gs_j])))

    # 重叠择优：字素长度降序 → 起点升序 → CSV 行序升序；被挤掉的不再告警
    candidates.sort(key=lambda h: (-(h.end - h.start), h.start, h.entry.row))
    accepted: list[TermHit] = []
    for h in candidates:
        if all(h.end <= a.start or h.start >= a.end for a in accepted):
            accepted.append(h)
    accepted.sort(key=lambda h: (h.start, h.end, h.entry.row))
    return accepted
