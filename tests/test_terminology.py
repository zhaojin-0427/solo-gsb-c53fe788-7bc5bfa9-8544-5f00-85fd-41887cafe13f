"""术语一致性审查的端到端回归测试。

覆盖：
- UTF-8 CSV 解析：表头必须恰为「规范写法,禁用变体」两列；空表 / 空字段 /
  折叠后同一变体对应冲突写法 / 列数不对 / 非 UTF-8 / 结构错误 → 400；
- 服务端读取每条字幕的**当前可见文案**（修订稿优先，否则导入可见文本），
  给出命中变体、推荐写法与从 0 起、按 UAX #29 字素簇计的半开区间，
  不自动改文案；
- 含拉丁字母的变体忽略大小写，且仅在两端相邻字素不是拉丁字母或数字时
  命中（xwifi / WIFI1 / wifi5 不命中；CJK 与空格两侧命中）；其余变体
  按字面命中（中文嵌在数字中也命中、大小写不折叠）；
- casefold 改变长度（İ→i+◌̇、ﬁ→fi）时区间仍映射回原文字素，无定位偏移；
  emoji ZWJ 等多码点字素按 1 字素计；
- 重叠命中按（字素长度降序、起点升序、CSV 行序升序）贪心择优去重，
  同区间不重复告警，端点相贴不算重叠；
- text_hash 只按文案：文案修订 / 恢复使其变化，仅改时间参数（CPS / 最小
  间隔 / 容差 / 采纳 / 撤销）保持不变；审查不落库，CPS / 采纳 / 留白与
  JSON / SRT / ZIP 导出字节级不变；项目不存在 → 404。
"""
import asyncio
import io
import os
import sys
import zipfile

DB = "/tmp/test_terminology.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_PATH"] = DB  # 须在导入 app 之前设置

sys.path.insert(0, "/workspace")

from fastapi import HTTPException, UploadFile  # noqa: E402

from app import main as m  # noqa: E402
from app import terminology as term  # noqa: E402

m.db.init_db()


def _upload(name: str, content, binary=False) -> UploadFile:
    data = content if binary else content.encode("utf-8")
    return UploadFile(io.BytesIO(data), filename=name)


def _create(srt, **kw):
    return asyncio.run(m.create_project(
        name="术语场景", cps_limit=kw.get("cps_limit", 21.0),
        min_gap_ms=kw.get("min_gap_ms", 0),
        tolerance_ms=kw.get("tolerance_ms", 200),
        subtitle=_upload("a.srt", srt),
        shots_file=_upload("s.json", kw["shots"]) if "shots" in kw else None,
    ))


def _review(pid, csv_text, name="glossary.csv", raw=None):
    return asyncio.run(m.review_terminology(
        pid, _upload(name, raw if raw is not None else csv_text,
                     binary=raw is not None)))


def _hits(v):
    return [(h["cue_ord"], h["start_grapheme"], h["end_grapheme"],
             h["matched"], h["recommended"], h["csv_row"]) for h in v["hits"]]


# ---------- 单元：CSV 解析与校验 ----------

def test_parse_errors():
    def must_fail(raw_bytes, label):
        try:
            term.parse_glossary(raw_bytes)
            raise AssertionError(f"{label} 应报错")
        except term.GlossaryError:
            pass

    must_fail("".encode("utf-8"), "空文件")
    must_fail("\n\n".encode("utf-8"), "仅空行")
    must_fail("foo,bar\n推荐,x\n".encode("utf-8"), "表头错误")
    must_fail("规范写法,禁用变体\n".encode("utf-8"), "只有表头")
    must_fail("规范写法,禁用变体\n\n\n".encode("utf-8"), "表头后只空行")
    must_fail("规范写法,禁用变体\n推荐,\n".encode("utf-8"), "空禁用变体")
    must_fail("规范写法,禁用变体\n ,x\n".encode("utf-8"), "空规范写法")
    must_fail("规范写法,禁用变体\n推荐,x,多余\n".encode("utf-8"), "三列")
    must_fail("规范写法,禁用变体\n推荐,x\n推荐2\n".encode("utf-8"), "单列数据行")
    must_fail("规范写法,禁用变体\n甲,wifi\n乙,WiFi\n".encode("utf-8"),
              "折叠后冲突写法")
    must_fail("规范写法,禁用变体\n".encode("gbk"), "非 UTF-8")

    # BOM + 尾随空行 + 空白填充：合法
    entries = term.parse_glossary(
        "﻿规范写法,禁用变体\n 推荐 , wifi \n\n".encode("utf-8"))
    assert len(entries) == 1
    assert entries[0].canonical == "推荐" and entries[0].variant == "wifi"
    assert entries[0].latin and entries[0].key == "wifi"

    # 折叠键相同且规范写法相同：去重、保留先出现行的行号
    entries = term.parse_glossary(
        "规范写法,禁用变体\nWLAN,WiFi\nWLAN,wifi\n".encode("utf-8"))
    assert len(entries) == 1 and entries[0].row == 1
    print("unit glossary parse / validation ok")


# ---------- 单元：匹配规则 ----------

def test_latin_casefold_and_boundaries():
    es = term.parse_glossary(
        "规范写法,禁用变体\n无线网络,WiFi\n".encode("utf-8"))
    # 命中：大写、小写、两侧为 CJK / 空格 / 串首 / 串尾
    hits = term.review_cue(1, "1", "WiFi 和 wifi 与WIFI，wIfI", es)
    assert [(h.start, h.end, h.matched) for h in hits] == [
        (0, 4, "WiFi"), (7, 11, "wifi"), (13, 17, "WIFI"), (18, 22, "wIfI")]
    # 不命中：左邻拉丁、左邻数字、右邻数字
    assert term.review_cue(1, "1", "xwifi WIFI1 wifi5", es) == []
    # 下划线/标点不属于拉丁字母或数字 → 两端允许命中
    hits = term.review_cue(1, "1", "_wifi-", es)
    assert [(h.start, h.end) for h in hits] == [(1, 5)]
    print("unit latin casefold + boundaries ok")


def test_non_latin_literal_match():
    es = term.parse_glossary(
        "规范写法,禁用变体\n计算机,电脑\n无线网络,ＷＩＦＩ\n".encode("utf-8"))
    # 中文按字面命中：嵌在数字中间也命中，无词边界限制
    hits = term.review_cue(1, "1", "1电脑2", es)
    assert [(h.start, h.end, h.matched) for h in hits] == [(1, 3, "电脑")]
    # 全角拉丁变体不含拉丁字母（\p{Latin} 不命中全角形式）→ 按字面命中，
    # 半角 wifi 不折叠匹配，全角形式按字面出现即命中
    assert term.review_cue(1, "1", "wifi WIFI", es) == []
    hits = term.review_cue(1, "1", "wifi 与 ＷＩＦＩ", es)
    assert len(hits) == 1 and hits[0].matched == "ＷＩＦＩ"
    print("unit non-latin literal match ok")


def test_casefold_length_change_maps_to_original_graphemes():
    # İ (U+0130).casefold() == "i̇"（长度 +1）；ﬁ.casefold() == "fi"
    es = term.parse_glossary(
        "规范写法,禁用变体\n伊兹密尔,İzmir\n文件,ﬁle\n".encode("utf-8"))
    text = "İzmir 不错 ﬁle"
    hits = term.review_cue(7, "7", text, es)
    # 与直接按原文字素簇切分的结果逐一核对，杜绝折叠长度造成的偏移
    clusters = term._GRAPHEME_RE.findall(text)
    for h in hits:
        assert "".join(clusters[h.start:h.end]) == h.matched
    assert [(h.start, h.end, h.matched) for h in hits] == [
        (0, 5, "İzmir"), (9, 12, "ﬁle")]
    print("unit casefold length change -> original grapheme ranges ok")


def test_emoji_graphemes():
    es = term.parse_glossary(
        "规范写法,禁用变体\n无线网络,WiFi\n".encode("utf-8"))
    # 👨‍👩‍👧 为多码点 ZWJ 序列，按 1 个扩展字素簇计
    text = "👨‍👩‍👧WiFi"
    hits = term.review_cue(1, "1", text, es)
    assert [(h.start, h.end) for h in hits] == [(1, 5)]
    clusters = term._GRAPHEME_RE.findall(text)
    assert len(clusters) == 5 and "".join(clusters[1:5]) == "WiFi"
    print("unit emoji / ZWJ grapheme ranges ok")


def test_overlap_prefers_length_start_roworder():
    # 长度优先：「WiFi 直连」长于「WiFi」，重叠区间只告警长的
    es = term.parse_glossary((
        "规范写法,禁用变体\n"
        "无线网络直连,WiFi 直连\n"
        "无线网络,WiFi\n").encode("utf-8"))
    hits = term.review_cue(1, "1", "WiFi 直连", es)
    assert [(h.start, h.end, h.canonical, h.csv_row) for h in hits] == [
        (0, 7, "无线网络直连", 1)]

    # 同长度同起点 → CSV 行序优先；同区间不重复告警（用两个不同的字面
    # 变体——折叠等价的变体若推荐写法不同会在词表校验阶段直接报错）
    es2 = term.parse_glossary((
        "规范写法,禁用变体\n"
        "推荐甲,电脑\n"
        "推荐乙,电脳\n").encode("utf-8"))
    hits = term.review_cue(1, "1", "电脑电脳", es2)
    # 两个变体长度相同但出现在不同位置；构造同点竞争：同文案含两种写法时
    # 各自命中（端点相贴）。同点竞争用同一文案不可能（字面不同），故这里
    # 只验证两点都保留且各自归属正确行。
    assert [(h.start, h.end, h.canonical, h.csv_row) for h in hits] == [
        (0, 2, "推荐甲", 1), (2, 4, "推荐乙", 2)]

    # 起点优先 + 端点相贴不算重叠：[0,2) 与 [2,4) 各自保留
    es3 = term.parse_glossary((
        "规范写法,禁用变体\n"
        "甲,电脑\n"
        "乙,电视\n").encode("utf-8"))
    hits = term.review_cue(1, "1", "电脑电视", es3)
    assert [(h.start, h.end, h.csv_row) for h in hits] == [
        (0, 2, 1), (2, 4, 2)]

    # 重叠（共享一个字素）：同长度时起点小者优先，[1,3) 被丢弃
    es4 = term.parse_glossary((
        "规范写法,禁用变体\n"
        "甲,电脑\n"
        "乙,脑脑\n").encode("utf-8"))
    hits = term.review_cue(1, "1", "电脑脑", es4)
    assert [(h.start, h.end) for h in hits] == [(0, 2)]

    # 折叠等价且推荐写法相同的重复行：去重后同区间只告警一次
    es5 = term.parse_glossary((
        "规范写法,禁用变体\n"
        "无线网络,WiFi\n"
        "无线网络,wifi\n").encode("utf-8"))
    hits = term.review_cue(1, "1", "WiFi", es5)
    assert len(hits) == 1 and hits[0].csv_row == 1
    print("unit overlap tie-break (length/start/row) ok")


# ---------- 端到端 ----------

SRT = ("1\n00:00:00,000 --> 00:00:02,000\nWiFi 连不上，xwifi 正常，WIFI1 不管\n\n"
       "2\n00:00:02,000 --> 00:00:03,000\n电脑很慢\n\n"
       "3\n00:00:03,000 --> 00:00:04,000\n一切正常\n")


def test_e2e_hits_on_current_text_and_ranges():
    proj = _create(SRT)
    pid = proj["id"]
    v = _review(pid, "规范写法,禁用变体\n无线网络,WiFi\n计算机,电脑\n")
    assert v["project_id"] == pid and v["entry_count"] == 2
    assert v["cue_count"] == 3 and v["cue_hit_count"] == 2
    assert _hits(v) == [
        (1, 0, 4, "WiFi", "无线网络", 1),
        (2, 0, 2, "电脑", "计算机", 2),
    ]
    # 命中区间按原文字素簇可还原
    c1 = next(c for c in m.get_project(pid)["analysis"]["cues"] if c["ord"] == 1)
    cl = term._GRAPHEME_RE.findall(c1["text"])
    assert "".join(cl[0:4]) == "WiFi"
    print("e2e hits on current text with grapheme ranges ok")
    return pid


def test_e2e_revision_takes_priority_without_auto_edit():
    proj = _create(SRT)
    pid = proj["id"]
    cues = proj["analysis"]["cues"]
    # 修订第 1、2 条：第 1 条消除 WiFi（新增「电脑」），第 2 条消除「电脑」
    proj = m.edit_cue_text(pid, cues[0]["uid"],
                           m.TextEditIn(text="网络正常，电脑坏了", base_version=0))
    proj = m.edit_cue_text(pid, cues[1]["uid"],
                           m.TextEditIn(text="速度很快", base_version=proj["cues"][1]["text_version"]))
    v = _review(pid, "规范写法,禁用变体\n无线网络,WiFi\n计算机,电脑\n")
    # 第 1 条修订稿里的「电脑」命中；第 2 条修订稿无命中（导入原文「电脑」
    # 不再参与审查）；导入原文未被改动
    assert _hits(v) == [(1, 5, 7, "电脑", "计算机", 2)]
    db_cues = m.get_project(pid)["cues"]
    assert db_cues[1]["imported_text"] == "电脑很慢"
    assert db_cues[0]["imported_text"].startswith("WiFi 连不上")
    # 恢复导入原文后命中回到原状
    m.restore_cue_text(pid, cues[0]["uid"],
                       m.TextRestoreIn(base_version=db_cues[0]["text_version"]))
    m.restore_cue_text(pid, cues[1]["uid"],
                       m.TextRestoreIn(base_version=db_cues[1]["text_version"]))
    v = _review(pid, "规范写法,禁用变体\n无线网络,WiFi\n计算机,电脑\n")
    assert v["cue_hit_count"] == 2
    print("e2e revision priority / no auto-edit / restore ok")


def test_e2e_text_hash_text_vs_time():
    # 相接两条，min_gap=40 会产生提案（涉及时间）；术语有效性只按文案
    srt = ("1\n00:00:00,000 --> 00:00:01,000\nWiFi 甲\n\n"
           "2\n00:00:01,000 --> 00:00:02,000\n电脑 乙\n")
    shots = '[{"start_ms": 0, "end_ms": 10000}]'
    proj = _create(srt, shots=shots)
    pid = proj["id"]
    h_text = proj["text_hash"]
    h_time = proj["timeline_hash"]
    v = _review(pid, "规范写法,禁用变体\n无线网络,WiFi\n计算机,电脑\n")
    assert v["text_hash"] == h_text and v["hit_count"] == 2

    # 仅改 CPS 上限：text_hash 不变
    p2 = m.update_settings(pid, m.SettingsIn(cps_limit=9.0, min_gap_ms=0,
                                             tolerance_ms=200))
    assert p2["text_hash"] == h_text
    # 最小间隔 / 容差 / 采纳 / 撤销改变时间轴：text_hash 不变，timeline 变
    p3 = m.update_settings(pid, m.SettingsIn(cps_limit=9.0, min_gap_ms=100,
                                             tolerance_ms=200))
    assert p3["text_hash"] == h_text and p3["timeline_hash"] != h_time
    cue2 = p3["analysis"]["cues"][1]
    if cue2["proposal_start_ms"] is not None:
        p4 = m.adopt(pid, m.AdoptIn(
            cue_uid=cue2["uid"], new_start_ms=cue2["proposal_start_ms"],
            new_end_ms=cue2["proposal_end_ms"]))
        assert p4["text_hash"] == h_text
        p5 = m.revert_adoption(pid, cue2["uid"])
        assert p5["text_hash"] == h_text

    # 文案修订：text_hash 必变；恢复后回到原值
    m.edit_cue_text(pid, p3["cues"][0]["uid"],
                    m.TextEditIn(text="WLAN 甲", base_version=0))
    assert m.get_project(pid)["text_hash"] != h_text
    c = m.get_project(pid)["cues"][0]
    m.restore_cue_text(pid, c["uid"], m.TextRestoreIn(base_version=c["text_version"]))
    assert m.get_project(pid)["text_hash"] == h_text
    print("e2e text_hash binds text not time ok")


def test_e2e_invalid_csv_and_404():
    proj = _create(SRT)
    pid = proj["id"]
    for bad in ["", "规范写法,禁用变体\n", "规范写法,禁用变体\nx,\n",
                "a,b\n无线网络,WiFi\n",
                "规范写法,禁用变体\nA,wifi\nB,WiFi\n"]:
        try:
            _review(pid, bad)
            raise AssertionError("非法词表应 400")
        except HTTPException as exc:
            assert exc.status_code == 400
    try:
        _review(pid, "", raw="规范写法,禁用变体\n无线网络,WiFi\n".encode("gbk"))
        raise AssertionError("GBK 词表应 400")
    except HTTPException as exc:
        assert exc.status_code == 400
    try:
        _review(999999, "规范写法,禁用变体\n无线网络,WiFi\n")
        raise AssertionError("项目不存在应 404")
    except HTTPException as exc:
        assert exc.status_code == 404

    # 无命中的合法词表：200、空 hits，不报错（空表才报错）
    v = _review(pid, "规范写法,禁用变体\n无此变体,不存在的词XYZ\n")
    assert v["hits"] == [] and v["hit_count"] == 0 and v["cue_hit_count"] == 0
    print("e2e invalid CSV -> 400 / 404 / empty result ok")


def test_e2e_review_read_only_exports_unchanged():
    shots = '[{"start_ms": 0, "end_ms": 10000}]'
    proj = _create(SRT, shots=shots)
    pid = proj["id"]
    cues = proj["analysis"]["cues"]
    # 制造采纳与留白结论
    target = next((c for c in cues if c["proposal_start_ms"] is not None), None)
    if target is not None:
        m.adopt(pid, m.AdoptIn(cue_uid=target["uid"],
                               new_start_ms=target["proposal_start_ms"],
                               new_end_ms=target["proposal_end_ms"]))
    gaps = m.get_project(pid)["gap_review"]["items"]
    if gaps:
        m.review_gap(pid, m.GapReviewIn(
            shot_ord=gaps[0]["shot_ord"], gap_start_ms=gaps[0]["gap_start_ms"],
            gap_end_ms=gaps[0]["gap_end_ms"], decision="intentional"))

    before = m.get_project(pid)
    doc = m.export(pid, fmt="json").body
    srt = m.export(pid, fmt="srt").body
    with zipfile.ZipFile(io.BytesIO(m.export(pid, fmt="zip").body)) as zf:
        zips = {n: zf.read(n) for n in zf.namelist()}

    _review(pid, "规范写法,禁用变体\n无线网络,WiFi\n计算机,电脑\n")
    _review(pid, "规范写法,禁用变体\n无线网络,WiFi\n")
    try:
        _review(pid, "规范写法,禁用变体\n无线网络,\n")
    except HTTPException:
        pass

    after = m.get_project(pid)
    assert after == before
    assert m.export(pid, fmt="json").body == doc
    assert m.export(pid, fmt="srt").body == srt
    with zipfile.ZipFile(io.BytesIO(m.export(pid, fmt="zip").body)) as zf:
        assert {n: zf.read(n) for n in zf.namelist()} == zips
    print("e2e review read-only: CPS/adopt/gaps/exports unchanged ok")


def test_e2e_response_shape():
    proj = _create(SRT)
    v = _review(proj["id"], "规范写法,禁用变体\n无线网络,WiFi\n")
    h = v["hits"][0]
    assert set(h) == {"cue_ord", "cue_id", "start_grapheme", "end_grapheme",
                      "matched", "recommended", "csv_row"}
    assert set(v) >= {"project_id", "project_name", "glossary_name",
                      "text_hash", "entry_count", "cue_count",
                      "hit_count", "cue_hit_count", "hits"}
    print("e2e response shape ok")


def main():
    test_parse_errors()
    test_latin_casefold_and_boundaries()
    test_non_latin_literal_match()
    test_casefold_length_change_maps_to_original_graphemes()
    test_emoji_graphemes()
    test_overlap_prefers_length_start_roworder()
    test_e2e_hits_on_current_text_and_ranges()
    test_e2e_revision_takes_priority_without_auto_edit()
    test_e2e_text_hash_text_vs_time()
    test_e2e_invalid_csv_and_404()
    test_e2e_review_read_only_exports_unchanged()
    test_e2e_response_shape()
    print("terminology review regression ok")


if __name__ == "__main__":
    main()
