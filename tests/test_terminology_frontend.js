// 术语一致性审查的前端绑定回归（node 直接运行，无框架）：
// 用最小 DOM/fetch 桩在 vm 中加载 app/static/app.js，验证——
// - 结果与「项目 + 文案指纹 text_hash + 提交序号」绑定，与含时间的
//   timeline_hash 无关：仅改时间参数（CPS / 间隔 / 容差 / 采纳）
//   text_hash 不变 → 结果保留、不发请求；当前文案变化（修订/恢复）→
//   旧结果立即作废，用最近一次上传成功的词表快照自动重查；
// - 词表快照只在「上传并审查」成功后记录：非法 CSV（400）只弹错，
//   不替换快照、不清空已展示结果；改选文件框未提交不影响自动重查，
//   清空文件框后快照仍可用；
// - 快照不可读、详情刷新失败或有界重查失败时作废旧结果并提示重新上传；
// - 跨标签页改文案：响应指纹不符时先拉最新详情裁决（一致即接纳，不再
//   重核、不循环请求），仍不符才用快照重查；自动重查有终止条件；
// - 再次提交 / 切换项目交错时迟到响应丢弃，不串项目、不覆盖更新结果；
// - 字素高亮区间按 UAX #29 字素簇定位（含 emoji ZWJ）。
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const APP_JS = path.join(__dirname, "..", "app", "static", "app.js");

// ---------- 最小 DOM 桩 ----------
function makeEl(tag = "div") {
  return {
    tagName: tag,
    innerHTML: "",
    textContent: "",
    value: "",
    href: "",
    files: [],
    children: [],
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    listeners: {},
    addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); },
    appendChild(c) { this.children.push(c); return c; },
    reset() { this.files = []; },
    closest() { return null; },
    querySelector() { return makeEl(); },
    querySelectorAll() { return []; },
  };
}

const bySelector = new Map();
const documentStub = {
  querySelector(sel) {
    if (!bySelector.has(sel)) bySelector.set(sel, makeEl());
    return bySelector.get(sel);
  },
  querySelectorAll() { return []; },
  createElement(tag) { return makeEl(tag); },
};

const pending = [];
const fetchLog = [];
function fetchStub(url, opts = {}) {
  fetchLog.push({ url, method: opts.method || "GET", opts });
  return new Promise((resolve) => pending.push({ url, opts, resolve }));
}
function popPending(substr) {
  for (let i = pending.length - 1; i >= 0; i--) {
    if (pending[i].url.includes(substr)) return pending.splice(i, 1)[0];
  }
  throw new Error(`没有在途请求匹配 ${substr}`);
}
function popPendingExact(url) {
  for (let i = pending.length - 1; i >= 0; i--) {
    if (pending[i].url === url) return pending.splice(i, 1)[0];
  }
  throw new Error(`没有在途请求匹配 ${url}`);
}
function respondJson(req, body, ok = true, status = 200) {
  req.resolve({ ok, status, json: async () => body });
}
const flush = () => new Promise((r) => setImmediate(r));

class FormDataStub {
  constructor() { this.parts = []; }
  append(k, v) { this.parts.push([k, v]); }
}

const src = fs.readFileSync(APP_JS, "utf8") +
  "\n;globalThis.__app = { state, render, renderTermReview," +
  " syncTermReview, resetTermReview, runTermReview," +
  " submittedTermFile, highlightTermText, termClusters };";
const sandbox = {
  document: documentStub,
  fetch: fetchStub,
  FormData: FormDataStub,
  setTimeout: () => 0,
  clearTimeout: () => {},
  console,
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "app.js" });
const app = sandbox.__app;
const { state } = app;

function proj(id, textHash, timelineHash) {
  return {
    id, name: `P${id}`, cps_limit: 21, min_gap_ms: 40, tolerance_ms: 200,
    created_at: "", timeline_hash: timelineHash || `t-${textHash}`,
    text_hash: textHash,
    shots: [], cues: [],
    analysis: {
      cues: [{ uid: 11, ord: 1, cue_id: "1", text: "WiFi 坏了",
               start_ms: 0, end_ms: 1000,
               graphemes: 6, duration_ms: 1000, cps: 6, cps_ok: true,
               raw_hash: "0123456789abcdef0123456789abcdef",
               proposal_start_ms: null, proposal_end_ms: null,
               adopted: false, adopted_start_ms: null, adopted_end_ms: null,
               edited: false, imported_text: "WiFi 坏了", text_version: 0 }],
      chains: [], intrinsic: [],
      stats: { cue_count: 1, shot_count: 0, gap_violations: 0,
               cps_violations: 0, cross_shot_count: 0, proposal_count: 0,
               proposal_moved_count: 0, conflict_cue_count: 0,
               total_shift_ms: 0 },
    },
    gap_review: null,
  };
}
function termResult(pid, hash, hitCount = 1) {
  return {
    project_id: pid, project_name: `P${pid}`, glossary_name: "g.csv",
    text_hash: hash, entry_count: 2, cue_count: 1,
    hit_count: hitCount, cue_hit_count: hitCount ? 1 : 0,
    hits: hitCount ? [{ cue_ord: 1, cue_id: "1", start_grapheme: 0,
                        end_grapheme: 4, matched: "WiFi",
                        recommended: "无线网络", csv_row: 1 }] : [],
  };
}
function termPostCount() {
  return fetchLog.filter((r) => r.url.includes("/terminology")).length;
}
function lastPostedFile() {
  for (let i = fetchLog.length - 1; i >= 0; i--) {
    const r = fetchLog[i];
    if (r.url.includes("/terminology") && r.opts && r.opts.body) {
      const part = r.opts.body.parts.find(([k]) => k === "glossary");
      if (part) return part[1];
    }
  }
  return null;
}

const termForm = documentStub.querySelector("#termForm");
termForm.glossary = makeEl("input");
const settingsForm = documentStub.querySelector("#settingsForm");
settingsForm.cps_limit = makeEl("input");
settingsForm.min_gap_ms = makeEl("input");
settingsForm.tolerance_ms = makeEl("input");

function clearTermState() {
  state.termReport = null;
  state.termNotice = "";
  state.termSnapshot = null;
  state.termAutoInFlight = false;
  state.termSeq++;
}

// 模拟「上传并审查」成功后的提交动作：生产代码里快照由表单 submit 处理器
// 在 runTermReview 成功后写入；测试直接调用 runTermReview 并在成功后
// 落快照（与处理器同构）。
async function submitTerm(file) {
  const ok = await app.runTermReview(file, false);
  if (ok) {
    state.termSnapshot = { file, name: String(file),
                           projectId: state.project.id };
  }
  return ok;
}

// ---------- 场景 ----------

async function test_manual_upload_binds_text_hash() {
  clearTermState();
  state.project = proj(1, "x1");
  const p = submitTerm("glossA");
  assert.strictEqual(termPostCount(), 1);
  respondJson(popPending("terminology"), termResult(1, "x1"));
  const ok = await p; await flush();
  assert.strictEqual(ok, true);
  assert.ok(state.termReport, "结果应展示");
  assert.strictEqual(state.termReport.text_hash, "x1");
  assert.strictEqual(app.submittedTermFile(), "glossA");
  console.log("manual upload binds project+text_hash ok");
}

async function test_time_only_change_keeps_result() {
  // 仅改时间参数（timeline_hash 变、text_hash 不变）：结果保留、不请求
  state.project = proj(1, "x1", "timeline-changed-1");
  const before = termPostCount();
  app.render();
  assert.strictEqual(termPostCount(), before, "时间参数变化不应触发重查");
  assert.ok(state.termReport);
  assert.strictEqual(state.termReport.text_hash, "x1");
  // 再来一次：CPS 变化、时间轴指纹也变，文案指纹仍 x1
  state.project = proj(1, "x1", "timeline-changed-2");
  app.render();
  assert.strictEqual(termPostCount(), before);
  assert.ok(state.termReport);
  console.log("time-only change (text_hash same) keeps result ok");
}

async function test_text_change_auto_rechecks_with_snapshot() {
  // 文案变化（text_hash x1 → x2）：旧结果作废，自动用快照重查
  state.project = proj(1, "x2");
  app.render();
  assert.strictEqual(state.termReport, null, "旧结果必须立即作废");
  assert.ok(state.termNotice.includes("重新审查"));
  assert.strictEqual(lastPostedFile(), "glossA", "自动重查必须用词表快照");
  respondJson(popPending("terminology"), termResult(1, "x2", 0));
  await flush();
  assert.ok(state.termReport);
  assert.strictEqual(state.termReport.hit_count, 0);
  assert.strictEqual(state.termReport.text_hash, "x2");
  console.log("text change -> invalidate + auto re-check ok");
}

async function test_invalid_csv_does_not_replace_snapshot_or_result() {
  // 先有一份有效结果（快照 glossGood）
  clearTermState();
  state.project = proj(2, "y1");
  termForm.glossary.files = ["glossBad"];
  const pBad = submitTerm("glossBad");
  // 非法 CSV 400：不更新快照、无旧结果时也不产生结果
  respondJson(popPending("terminology"),
              { detail: "术语词表无效：空表" }, false, 400);
  const okBad = await pBad; await flush();
  assert.strictEqual(okBad, false);
  assert.strictEqual(state.termSnapshot, null, "失败不得落快照");
  assert.strictEqual(state.termReport, null);

  // 成功上传 glossGood
  const pGood = submitTerm("glossGood");
  respondJson(popPending("terminology"), termResult(2, "y1", 2));
  assert.strictEqual(await pGood, true);
  await flush();
  assert.strictEqual(app.submittedTermFile(), "glossGood");
  assert.strictEqual(state.termReport.hit_count, 2);

  // 再传一份非法 CSV（文件框里的 glossBad2）：只弹错，旧结果与旧快照保留
  termForm.glossary.files = ["glossBad2"];
  const pBad2 = submitTerm("glossBad2");
  respondJson(popPending("terminology"),
              { detail: "术语词表无效：第 1 行：规范写法与禁用变体均不能为空" },
              false, 400);
  assert.strictEqual(await pBad2, false);
  await flush();
  assert.strictEqual(app.submittedTermFile(), "glossGood",
                     "非法 CSV 不得替换有效词表快照");
  assert.ok(state.termReport, "已展示结果不得被非法 CSV 清空");
  assert.strictEqual(state.termReport.hit_count, 2);

  // 文案变化后自动重查仍用 glossGood，而不是文件框里的 glossBad2
  state.project = proj(2, "y2");
  app.render();
  assert.strictEqual(lastPostedFile(), "glossGood");
  respondJson(popPending("terminology"), termResult(2, "y2", 1));
  await flush();
  assert.strictEqual(state.termReport.hit_count, 1);
  console.log("invalid CSV: snapshot/result kept; auto re-check uses good one ok");
}

async function test_reselect_without_submit_ignored() {
  // 文件框改选乙未提交：自动重查仍用已成功上传的甲
  clearTermState();
  state.project = proj(3, "z1");
  const p = submitTerm("gloss-A");
  respondJson(popPending("terminology"), termResult(3, "z1"));
  await p; await flush();
  termForm.glossary.files = ["gloss-B"];   // 只改选，不提交
  state.project = proj(3, "z2");
  app.render();
  assert.strictEqual(state.termReport, null);
  assert.strictEqual(lastPostedFile(), "gloss-A",
                     "改选未提交的乙词表不得参与自动重查");
  respondJson(popPending("terminology"), termResult(3, "z2", 0));
  await flush();
  assert.strictEqual(state.termReport.text_hash, "z2");
  console.log("reselect without submit ignored on re-check ok");
}

async function test_clear_input_keeps_snapshot() {
  clearTermState();
  state.project = proj(4, "q1");
  termForm.glossary.files = ["gloss-C"];
  const p = submitTerm("gloss-C");
  respondJson(popPending("terminology"), termResult(4, "q1"));
  await p; await flush();
  termForm.glossary.files = [];             // 清空文件框
  assert.strictEqual(app.submittedTermFile(), "gloss-C");
  state.project = proj(4, "q2");
  app.render();
  assert.strictEqual(lastPostedFile(), "gloss-C",
                     "清空文件框后仍须用快照自动重查");
  respondJson(popPending("terminology"), termResult(4, "q2"));
  await flush();
  assert.strictEqual(state.termReport.text_hash, "q2");
  console.log("clear input keeps snapshot ok");
}

async function test_unreadable_snapshot_asks_reupload() {
  clearTermState();
  state.project = proj(5, "w1");
  const p = submitTerm("gloss-D");
  respondJson(popPending("terminology"), termResult(5, "w1"));
  await p; await flush();
  state.termSnapshot.file = { unreadable: true };
  state.project = proj(5, "w2");
  app.render();
  respondJson(popPending("terminology"), { detail: "词表无法读取" }, false, 400);
  await flush();
  assert.strictEqual(state.termReport, null, "不可读时不得保留旧结果");
  assert.ok(state.termNotice.includes("重新上传"), "应提示重新上传");
  console.log("unreadable snapshot -> ask re-upload ok");
}

async function test_concurrent_uploads_last_wins() {
  clearTermState();
  state.project = proj(6, "v1");
  const p1 = submitTerm("gloss-E");
  const req1 = popPending("terminology");
  const p2 = submitTerm("gloss-E2");
  const req2 = popPending("terminology");
  respondJson(req2, termResult(6, "v1", 3));    // 第二次先回
  assert.strictEqual(await p2, true);
  await flush();
  assert.strictEqual(state.termReport.hit_count, 3);
  respondJson(req1, termResult(6, "v1", 9));    // 第一次迟到
  assert.strictEqual(await p1, false, "迟到响应必须被丢弃（返回 false）");
  await flush();
  assert.strictEqual(state.termReport.hit_count, 3, "迟到响应不得覆盖");
  console.log("concurrent uploads: stale response discarded ok");
}

async function test_switch_project_discards_inflight() {
  clearTermState();
  state.project = proj(7, "u1");
  const p = submitTerm("gloss-F");
  const req = popPending("terminology");
  app.resetTermReview();
  state.project = proj(8, "s1");
  respondJson(req, termResult(7, "u1", 2));
  await p; await flush();
  assert.strictEqual(state.termReport, null, "切换项目后迟到响应必须丢弃");
  console.log("switch project discards in-flight response ok");
}

async function test_cross_tab_text_change_accepted_after_refresh() {
  // 本页详情过期：响应指纹 x2（另一标签页改了文案），先拉详情，
  // 详情为 x2 → 直接接纳、同步页面，不再发重查请求（无循环）
  clearTermState();
  state.project = proj(9, "x1");
  const p = submitTerm("gloss-G");
  const base = termPostCount();          // 已含本次提交
  respondJson(popPending("terminology"), termResult(9, "x2", 4));
  await flush();
  respondJson(popPendingExact("/api/projects/9"), proj(9, "x2", "t2"));
  const ok = await p; await flush();
  assert.strictEqual(ok, true);
  assert.strictEqual(termPostCount(), base, "可接纳时不得循环重查");
  assert.ok(state.termReport);
  assert.strictEqual(state.termReport.hit_count, 4);
  assert.strictEqual(state.project.text_hash, "x2", "详情应同步到最新");
  console.log("cross-tab text change accepted after detail refresh ok");
}

async function test_stale_on_arrival_rechecked() {
  // 响应 x2 到达时本地已 x3；详情刷新仍 x3 → 用快照按 x3 重查
  clearTermState();
  state.project = proj(9, "x3");
  const p = submitTerm("gloss-H");
  const base = termPostCount();          // 已含本次提交的 1 次
  respondJson(popPending("terminology"), termResult(9, "x2"));
  await flush();
  respondJson(popPendingExact("/api/projects/9"), proj(9, "x3", "t3"));
  await flush();
  assert.strictEqual(termPostCount(), base + 1, "详情后应自动重查一次");
  assert.strictEqual(lastPostedFile(), "gloss-H");
  respondJson(popPending("terminology"), termResult(9, "x3", 2));
  assert.strictEqual(await p, true);
  await flush();
  assert.strictEqual(state.termReport.text_hash, "x3");
  assert.strictEqual(state.termReport.hit_count, 2);
  console.log("stale-on-arrival -> detail refresh then re-check ok");
}

async function test_repeated_text_changes_stop() {
  // 多次文案变化：重查链达到上限后停止并提示重传，不无界请求
  clearTermState();
  state.project = proj(10, "r1");
  const p = submitTerm("gloss-R");          // 第 1 次
  const base = termPostCount();             // 已含第 1 次
  respondJson(popPending("terminology"), termResult(10, "r2"));
  await flush();
  respondJson(popPendingExact("/api/projects/10"), proj(10, "r3"));
  await flush();                            // 第 2 次
  respondJson(popPending("terminology"), termResult(10, "r4"));
  await flush();
  respondJson(popPendingExact("/api/projects/10"), proj(10, "r5"));
  await flush();                            // 第 3 次
  respondJson(popPending("terminology"), termResult(10, "r6"));
  await flush();
  respondJson(popPendingExact("/api/projects/10"), proj(10, "r7"));
  await p; await flush();                   // 达到上限
  assert.strictEqual(termPostCount(), base + 2,
                     "重查链：首次提交 + 至多 2 次重查，共 3 次");
  assert.strictEqual(state.termReport, null);
  assert.ok(state.termNotice.includes("重新上传"));
  console.log("repeated text changes -> bounded stop ok");
}

async function test_detail_refresh_failure_stops() {
  clearTermState();
  state.project = proj(11, "f1");
  const p = submitTerm("gloss-I");
  const base = termPostCount();          // 已含本次提交
  respondJson(popPending("terminology"), termResult(11, "f2"));
  await flush();
  respondJson(popPendingExact("/api/projects/11"),
              { detail: "项目不存在" }, false, 404);
  await p; await flush();
  assert.strictEqual(termPostCount(), base, "详情失败不得继续重查");
  assert.strictEqual(state.termReport, null);
  assert.ok(state.termNotice.includes("重新上传"));
  console.log("detail refresh failure -> stop + ask re-upload ok");
}

async function test_highlight_grapheme_ranges() {
  // 高亮按字素簇（emoji ZWJ 为 1 字素）定位，不发生偏移
  const text = "👨‍👩‍👧 WiFi 和 WiFi";
  const hits = [
    { start_grapheme: 1, end_grapheme: 5 },
    { start_grapheme: 8, end_grapheme: 12 },
  ];
  const html = app.highlightTermText(text, hits);
  assert.ok(html.includes("<mark"), "应产出高亮");
  // 去掉标签后与原文一致（不丢字、不偏位）
  const plain = html.replace(/<[^>]+>/g, "");
  assert.strictEqual(plain, text);
  // emoji ZWJ 序列完整落在第一个 mark 之外（开头 1 个字素）
  assert.ok(html.startsWith("👨‍👩‍👧<mark"));
  // 区间非法 / 越界时安全跳过
  const weird = app.highlightTermText("abc", [
    { start_grapheme: 0, end_grapheme: 2 },
    { start_grapheme: 5, end_grapheme: 9 },
  ]);
  assert.strictEqual(weird.replace(/<[^>]+>/g, ""), "abc");
  console.log("highlight uses grapheme ranges (emoji-safe) ok");
}

async function main() {
  await test_manual_upload_binds_text_hash();
  await test_time_only_change_keeps_result();
  await test_text_change_auto_rechecks_with_snapshot();
  await test_invalid_csv_does_not_replace_snapshot_or_result();
  await test_reselect_without_submit_ignored();
  await test_clear_input_keeps_snapshot();
  await test_unreadable_snapshot_asks_reupload();
  await test_concurrent_uploads_last_wins();
  await test_switch_project_discards_inflight();
  await test_cross_tab_text_change_accepted_after_refresh();
  await test_stale_on_arrival_rechecked();
  await test_repeated_text_changes_stop();
  await test_detail_refresh_failure_stops();
  await test_highlight_grapheme_ranges();
  console.log("frontend terminology review binding regression ok");
}

main().catch((e) => { console.error(e); process.exit(1); });
