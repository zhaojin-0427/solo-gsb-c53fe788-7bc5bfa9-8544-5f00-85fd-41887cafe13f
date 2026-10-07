"""术语一致性审查的端到端回归测试。

覆盖：
- 词表解析：合法词表（表头列名定位、允许其他列与乱序列、BOM）；空表 /
  空字段 / 折叠后同一变体对应冲突写法 / 非法 CSV（缺列、列数不足、
  非 UTF-8）→ 400；折叠后同一变体同一写法的重复行去重；
- 命中规则：含拉丁字母的变体忽略大小写，且仅在两端不是拉丁字母或数字时
  命中（文本首 / 尾视为合法边界）；其余变体按字面命中（无边界限制）；
- 字素区间：从 0 起的半开字素区间（UAX #29，emoji 组合序列按 1）；
  大小写折叠改变长度（ß→ss）时命中区间仍映射回原文字素，定位不偏移；
- 重叠命中按（字素长度、起点、CSV 行序）择优，不产生重复告警；
- 当前文案优先修订稿：修订后按修订稿扫描，恢复后按导入原文扫描；
- text_hash：文案变化即变、单改时间参数（最小间隔 / 容差 / 采纳 / CPS）
  不变；term-review 响应与项目详情携带同一指纹；
- 只读：审查不改动详情、采纳、留白结论与 JSON / SRT / ZIP 导出；
- 项目不存在 → 404。
"""
import asyncio
import io
import json
import os
import sys
import zipfile

DB = "/tmp/test_terms.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_PATH"] = DB  # 须在导入 app 之前设置

sys.path.insert(0, "/workspace")

from fastapi import HTTPException, UploadFile  # noqa: E402

from app import main as m  # noqa: E402
from app import terms as terms_mod  # noqa: E402

m.db.init_db()

HEADER = "规范写法,禁用变体\n"


def _upload(name: str, content) -> UploadFile:
    data = content.encode("utf-8") if isinstance(content, str) else content
    return UploadFile(io.BytesIO(data), filename=name)


def _create(srt, shots=None, **kw):
    return asyncio.run(m.create_project(
        name="术语场景", cps_limit=kw.get("cps_limit", 21.0),
        min_gap_ms=kw.get("min_gap_ms", 0),
        tolerance_ms=kw.get("tolerance_ms", 200),
        subtitle=_upload("a.srt", srt),
        shots_file=_upload("s.json", shots) if shots else None,
    ))


def _review(pid, content, name="terms.csv"):
    return asyncio.run(m.term_review(pid, _upload(name, content)))


def _expect_400(content, why):
    try:
        terms_mod.parse_terms_csv(
            content.encode("utf-8") if isinstance(content, str) else content)
        raise AssertionError(f"应拒绝（{why}）：{content!r}")
    except terms_mod.TermsError:
        pass


def _srt(*texts):
    blocks = []
    for i, t in enumerate(texts, start=1):
        blocks.append(f"{i}\n00:00:0{i},000 --> 00:00:0{i},800\n{t}")
    return "\n\n".join(blocks) + "\n"


# ---------- 词表解析 ----------

def test_csv_parse_errors():
    # 空表：完全空 / 只有表头 / 只有空行
    for bad in ("", "   \n\n", HEADER, HEADER + "\n\n", " \n \n"):
        _expect_400(bad, "空表")
    # 空字段：规范写法或禁用变体为空（含纯空白）
    for bad in (HEADER + "规范A,\n", HEADER + ",变体B\n",
                HEADER + ",\n", HEADER + "规范A,   \n",
                HEADER + "  ,变体B\n"):
        _expect_400(bad, "空字段")
    # 非法 CSV：缺必需列 / 数据行列数不足 / 非 UTF-8
    _expect_400("写法,变体\na,b\n", "缺必需列")
    _expect_400(HEADER + "只有一列\n", "列数不足")
    _expect_400(b"\xff\xfe\x00\x01", "非 UTF-8")
    # 冲突：折叠后同一变体对应不同规范写法（拉丁忽略大小写 / 非拉丁字面）
    _expect_400(HEADER + "甲,iphone\n乙,IPHONE\n", "折叠后冲突")
    _expect_400(HEADER + "规范一,变体X\n规范二,变体X\n", "字面冲突")
    # 非法词表整体拒绝：接口侧同样 400，且不落任何报告
    proj = _create(_srt("甲"))
    for bad in ("", HEADER, HEADER + "规范A,\n", "写法,变体\na,b\n",
                HEADER + "甲,iphone\n乙,IPHONE\n"):
        try:
            _review(proj["id"], bad)
            raise AssertionError(f"接口应 400：{bad!r}")
        except HTTPException as exc:
            assert exc.status_code == 400, exc.status_code
    print("csv parse errors (empty/blank field/conflict/invalid) ok")


def test_csv_dedupe_and_header_flex():
    # BOM + 重复行去重（折叠后同一变体同一写法只计一次）
    entries = terms_mod.parse_terms_csv(
        ("﻿规范写法,禁用变体\n"
         "iPhone,iphone\n"
         "iPhone,IPHONE\n"
         "iPhone,iPhone\n").encode("utf-8"))
    assert len(entries) == 1
    assert entries[0].canonical == "iPhone" and entries[0].variant == "iphone"
    assert entries[0].row == 1 and entries[0].latin is True
    # 允许其他列、列顺序任意（按表头列名定位）；字段去空白
    entries = terms_mod.parse_terms_csv(
        "备注,禁用变体,规范写法\nx, app , App \n".encode("utf-8"))
    assert len(entries) == 1
    assert entries[0].canonical == "App" and entries[0].variant == "app"
    # 非拉丁变体不去重大小写概念：同一字面重复行去重
    entries = terms_mod.parse_terms_csv(
        (HEADER + "甲方,甲方\n甲方,甲方\n").encode("utf-8"))
    assert len(entries) == 1 and entries[0].latin is False
    print("csv dedupe + header flex ok")


# ---------- 命中规则 ----------

def test_latin_case_insensitive_with_boundaries():
    proj = _create(_srt(
        "app 真好",       # 1: 命中 [0,3)
        "apple 不行",     # 2: 右端是拉丁字母 l → 不命中
        "我的app2",       # 3: 右端是数字 → 不命中
        "2app 好",        # 4: 左端是数字 → 不命中
        "(APP) 好",       # 5: 括号是合法边界，忽略大小写命中 [1,4)
        "好用 App。",     # 6: 命中 [3,6)
    ))
    v = _review(proj["id"], HEADER + "应用,app\n")
    assert v["term_count"] == 1
    got = {(h["ord"], h["start"], h["end"], h["surface"]) for h in v["hits"]}
    assert got == {(1, 0, 3, "app"), (5, 1, 4, "APP"), (6, 3, 6, "App")}, got
    assert v["hit_count"] == 3
    print("latin case-insensitive + boundary ok")


def test_non_latin_literal_no_boundary():
    proj = _create(_srt("甲方和甲苯方", "x甲苯y"))
    # 非拉丁变体按字面命中：夹在拉丁字母中间也命中（无边界限制）
    v = _review(proj["id"], HEADER + "苯甲,甲苯\n")
    got = {(h["ord"], h["start"], h["end"], h["surface"]) for h in v["hits"]}
    assert got == {(1, 3, 5, "甲苯"), (2, 1, 3, "甲苯")}, got
    print("non-latin literal (no boundary) ok")


def test_casefold_length_change_no_offset():
    # ß 折叠为 ss（长度 1→2）：命中区间必须映射回原文字素，后续命中不偏移
    proj = _create(_srt("Straße 街"))
    v = _review(proj["id"], HEADER + "Strasse,strasse\n街道,街\n")
    got = [(h["start"], h["end"], h["surface"], h["variant"])
           for h in v["hits"]]
    # 字素：S0 t1 r2 a3 ß4 e5 空6 街7
    assert got == [(0, 6, "Straße", "strasse"), (7, 8, "街", "街")], got
    print("casefold length change keeps grapheme offsets ok")


def test_grapheme_intervals_emoji():
    # 👨‍👩‍👧‍👦 为 1 个字素簇；区间从 0 起、半开
    proj = _create(_srt("👨‍👩‍👧‍👦看APP吧"))
    v = _review(proj["id"], HEADER + "应用,app\n")
    got = [(h["start"], h["end"], h["surface"]) for h in v["hits"]]
    assert got == [(2, 5, "APP")], got
    print("grapheme intervals (emoji cluster = 1) ok")


def test_overlap_resolution():
    # 非拉丁变体（字面命中，无边界约束）便于纯粹验证重叠择优
    csv = (HEADER + "长词,甲乙丙丁\n短一,甲乙\n短二,乙丙\n双,甲甲\n"
           "拉长短,abc de\n拉长短二,abc\n")
    proj = _create(_srt(
        "甲乙丙丁",       # 1: 长词（4 字素）挤掉 甲乙 / 乙丙
        "甲乙丙",         # 2: 等长重叠，起点靠前的 甲乙 胜出
        "甲乙和乙丙",     # 3: 不重叠，各自报告
        "甲甲甲",         # 4: 同一变体自我重叠，只报一次
        "abc de",         # 5: 拉丁长词（6 字素）挤掉 abc
    ))
    v = _review(proj["id"], csv)
    got = [(h["ord"], h["start"], h["end"], h["variant"]) for h in v["hits"]]
    assert got == [
        (1, 0, 4, "甲乙丙丁"),   # 字素长度降序：长词优先
        (2, 0, 2, "甲乙"),       # 等长：起点升序
        (3, 0, 2, "甲乙"),       # 不重叠：都报告
        (3, 3, 5, "乙丙"),
        (4, 0, 2, "甲甲"),       # 自我重叠去重，不重复告警
        (5, 0, 6, "abc de"),     # 拉丁变体同样按长度择优
    ], got
    print("overlap resolution (length/start/row, no duplicates) ok")


def test_response_shape_and_fields():
    proj = _create(_srt("app 真好", "一切正常"))
    v = _review(proj["id"], HEADER + "应用,app\n")
    assert v["project_id"] == proj["id"]
    assert v["project_name"] == proj["name"]
    assert v["terms_name"] == "terms.csv"
    assert v["text_hash"] == proj["text_hash"]
    assert v["term_count"] == 1 and v["cue_count"] == 2
    assert v["hit_count"] == 1
    h = v["hits"][0]
    assert h["cue_id"] == "1" and h["ord"] == 1
    assert h["variant"] == "app" and h["canonical"] == "应用"
    assert h["row"] == 1 and h["start"] == 0 and h["end"] == 3
    assert h["surface"] == "app" and h["text"] == "app 真好"
    assert isinstance(h["cue_uid"], int)
    print("response shape + fields ok")


# ---------- 当前文案（修订稿优先）与 text_hash 绑定 ----------

def test_current_text_edited_preferred():
    proj = _create(_srt("旧词甲"))
    pid = proj["id"]
    csv = HEADER + "新词表,新词\n旧词表,旧词\n"

    v = _review(pid, csv)
    assert [(h["variant"], h["start"], h["end"]) for h in v["hits"]] == [
        ("旧词", 0, 2)]

    # 修订后按修订稿扫描：新词命中、旧词消失
    c = proj["analysis"]["cues"][0]
    proj = m.edit_cue_text(pid, c["uid"], m.TextEditIn(
        text="改用新词吧", base_version=c["text_version"]))
    v = _review(pid, csv)
    assert [(h["variant"], h["start"], h["end"]) for h in v["hits"]] == [
        ("新词", 2, 4)]
    assert v["text_hash"] == proj["text_hash"]

    # 恢复导入原文后按导入文案扫描
    c = proj["analysis"]["cues"][0]
    proj = m.restore_cue_text(pid, c["uid"], m.TextRestoreIn(
        base_version=c["text_version"]))
    v = _review(pid, csv)
    assert [(h["variant"], h["start"], h["end"]) for h in v["hits"]] == [
        ("旧词", 0, 2)]
    print("current text (edited preferred) ok")


def test_text_hash_binding():
    """text_hash 只随文案变化：单改时间参数（最小间隔 / 容差 / 采纳 / CPS）
    指纹不变（术语报告保留），文案修订 / 恢复才改变（报告过期）。"""
    srt = ("1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
           "2\n00:00:01,000 --> 00:00:02,000\n乙\n")
    proj = _create(srt, min_gap_ms=40)
    pid = proj["id"]
    th0, tl0 = proj["text_hash"], proj["timeline_hash"]
    assert th0 and tl0 and th0 != tl0

    # 最小间隔 40 → 100：生效时间轴变化（timeline_hash 变），文案未动
    # （text_hash 不变）——术语报告按文案判断仍然有效
    proj = m.update_settings(pid, m.SettingsIn(
        cps_limit=21.0, min_gap_ms=100, tolerance_ms=200))
    assert proj["timeline_hash"] != tl0
    assert proj["text_hash"] == th0

    # 仅调 CPS 上限：两者都不变
    proj = m.update_settings(pid, m.SettingsIn(
        cps_limit=10.0, min_gap_ms=100, tolerance_ms=200))
    assert proj["timeline_hash"] != tl0  # min_gap 仍是 100
    assert proj["text_hash"] == th0

    # 采纳提案（采纳值即提案值）：时间轴与文案均未实变
    cues = proj["analysis"]["cues"]
    tl_before = proj["timeline_hash"]
    proj = m.adopt(pid, m.AdoptIn(
        cue_uid=cues[1]["uid"],
        new_start_ms=cues[1]["proposal_start_ms"],
        new_end_ms=cues[1]["proposal_end_ms"]))
    assert proj["timeline_hash"] == tl_before
    assert proj["text_hash"] == th0

    # 文案修订：两者都变 → 旧术语报告过期
    proj = m.edit_cue_text(pid, cues[0]["uid"], m.TextEditIn(
        text="甲（修订）", base_version=cues[0]["text_version"]))
    assert proj["text_hash"] != th0
    assert proj["timeline_hash"] != tl0
    v = _review(pid, HEADER + "修订稿,修订\n")
    assert v["text_hash"] == proj["text_hash"]
    assert v["hit_count"] == 1

    # 恢复导入原文：文案指纹回到初始值（旧报告重新有效）
    c = proj["analysis"]["cues"][0]
    proj = m.restore_cue_text(pid, c["uid"], m.TextRestoreIn(
        base_version=c["text_version"]))
    assert proj["text_hash"] == th0
    print("text_hash binding (text-only validity) ok")


# ---------- 只读与错误 ----------

def test_review_is_read_only():
    shots = json.dumps([{"start_ms": 0, "end_ms": 10000}])
    proj = _create(_srt("app 真好", "一切正常", "第三"), shots=shots)
    pid = proj["id"]
    # 制造采纳与留白结论
    cues = proj["analysis"]["cues"]
    proj = m.adopt(pid, m.AdoptIn(
        cue_uid=cues[0]["uid"],
        new_start_ms=cues[0]["proposal_start_ms"],
        new_end_ms=cues[0]["proposal_end_ms"]))
    gaps = proj["gap_review"]["items"]
    assert gaps
    proj = m.review_gap(pid, m.GapReviewIn(
        shot_ord=gaps[0]["shot_ord"], gap_start_ms=gaps[0]["gap_start_ms"],
        gap_end_ms=gaps[0]["gap_end_ms"], decision="intentional"))

    before = m.get_project(pid)
    doc_before = m.export(pid, fmt="json").body
    srt_before = m.export(pid, fmt="srt").body
    with zipfile.ZipFile(io.BytesIO(m.export(pid, fmt="zip").body)) as zf:
        zip_before = {n: zf.read(n) for n in zf.namelist()}

    # 有命中 / 无命中 / 词表非法，都不落库、不改任何东西
    _review(pid, HEADER + "应用,app\n")
    _review(pid, HEADER + "无关,zzz\n")
    try:
        _review(pid, HEADER + "甲,iphone\n乙,IPHONE\n")
        raise AssertionError("冲突词表应 400")
    except HTTPException as exc:
        assert exc.status_code == 400

    after = m.get_project(pid)
    assert after == before
    assert m.export(pid, fmt="json").body == doc_before
    assert m.export(pid, fmt="srt").body == srt_before
    with zipfile.ZipFile(io.BytesIO(m.export(pid, fmt="zip").body)) as zf:
        assert {n: zf.read(n) for n in zf.namelist()} == zip_before
    print("term review is read-only (detail/adoption/gaps/exports) ok")


def test_project_missing_404():
    try:
        _review(999999, HEADER + "应用,app\n")
        raise AssertionError("不存在的项目应 404")
    except HTTPException as exc:
        assert exc.status_code == 404
    print("missing project 404 ok")


def main():
    test_csv_parse_errors()
    test_csv_dedupe_and_header_flex()
    test_latin_case_insensitive_with_boundaries()
    test_non_latin_literal_no_boundary()
    test_casefold_length_change_no_offset()
    test_grapheme_intervals_emoji()
    test_overlap_resolution()
    test_response_shape_and_fields()
    test_current_text_edited_preferred()
    test_text_hash_binding()
    test_review_is_read_only()
    test_project_missing_404()
    print("terms review regression ok")


if __name__ == "__main__":
    main()
