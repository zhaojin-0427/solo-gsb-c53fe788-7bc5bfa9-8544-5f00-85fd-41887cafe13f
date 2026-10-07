// 术语一致性审查的前端绑定回归（node 直接运行，无框架）：
// 用最小 DOM/fetch 桩在 vm 中加载 app/static/app.js，验证——
// - 报告与「项目 + 当前文案 text_hash + 提交序号」绑定：只按文案判断有效性，
//   不看时间（timeline_hash 变化但 text_hash 不变时报告保留、不发请求）；
// - 文案变化（text_hash 变化）后：旧报告立即作废，自动用最近一次已提交的
//   词表快照重新审查；无快照 / 快照不可读时提示重新上传词表；
// - 词表切换以「上传并审查」提交动作为唯一边界：改选未提交不换表、清空
//   文件框不影响快照，自动重查一律读取已提交快照；
// - 迟到响应（已有更新的提交 / 已切换项目）一律丢弃；
// - 响应指纹与本页详情不符（另一标签页改了文案）：先拉取最新详情再裁决——
//   可接纳则直接展示（不循环请求），仍不符才有界重查（最多 3 次），
//   详情刷新失败 / 快照不可用时停止并提示重传；
// - 手动提交非法词表（400）只弹错，已展示的旧报告保留。
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
    style: {},
    classList: { add() {}, remove() {}, toggle() {} },
    listeners: {},
    addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); },
    appendChild(c) { this.children.push(c); return c; },
    reset() { this.files = []; if (this.terms) this.terms.files = []; },
    focus() {},
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

// ---------- fetch 桩：响应时机完全由测试控制 ----------
const pending = [];   // 在途请求 {url, opts, resolve}
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
function popPendingExact(url) {   // 精确匹配（区分详情 GET 与列表/审查请求）
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

// ---------- 在 vm 中加载 app.js，并暴露词法绑定的 state 与函数 ----------
const src = fs.readFileSync(APP_JS, "utf8") +
  "\n;globalThis.__app = { state, render, renderTermReview," +
  " syncTermReview, resetTermReview, runTermReview," +
  " submittedTermsFile, scheduleAutoTermReview };";
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

// ---------- 测试工具 ----------
function proj(id, textHash, timelineHash = "tl-" + textHash) {
  return {
    id, name: `P${id}`, cps_limit: 21, min_gap_ms: 40, tolerance_ms: 200,
    created_at: "", timeline_hash: timelineHash, text_hash: textHash,
    shots: [], cues: [],
    analysis: {
      cues: [], chains: [], intrinsic: [],
      stats: { cue_count: 0, shot_count: 0, gap_violations: 0,
               cps_violations: 0, cross_shot_count: 0, proposal_count: 0,
               proposal_moved_count: 0, conflict_cue_count: 0,
               total_shift_ms: 0 },
    },
    gap_review: null,
  };
}
function termResult(pid, textHash, hitCount = 0) {
  const hits = [];
  for (let i = 0; i < hitCount; i++) {
    hits.push({ cue_uid: i + 1, cue_id: String(i + 1), ord: i + 1,
                variant: "app", canonical: "应用", row: 1,
                start: 0, end: 3, surface: "app", text: "app 真好" });
  }
  return { project_id: pid, project_name: `P${pid}`, terms_name: "terms.csv",
           text_hash: textHash, term_count: 1, cue_count: 2,
           hit_count: hitCount, hits };
}
function termPostCount() {
  return fetchLog.filter((r) => r.url.includes("/term-review")).length;
}

const termForm = documentStub.querySelector("#termReviewForm");
termForm.terms = makeEl("input");
const settingsForm = documentStub.querySelector("#settingsForm");
settingsForm.cps_limit = makeEl("input");
settingsForm.min_gap_ms = makeEl("input");
settingsForm.tolerance_ms = makeEl("input");

function clearTermState() {
  state.termReview = null;
  state.termNotice = "";
  state.termSnapshot = null;
  state.termAutoInFlight = false;
  state.termSeq++;
}

// 模拟提交动作（点击「上传并审查」）：提交是词表切换边界——只有这里会
// 写入已提交词表快照。runTermReview 本身不改快照，与生产代码一致。
function submitTerms(file) {
  state.termSnapshot = { file, name: String(file), projectId: state.project.id };
  state.termAutoInFlight = false;
  state.termNotice = "";
  return app.runTermReview(file, false);
}

// 取最近一次审查请求实际携带的文件（FormData 的 terms 部分），
// 用于断言自动重查用的是哪一份词表。
function lastPostedFile() {
  for (let i = fetchLog.length - 1; i >= 0; i--) {
    const r = fetchLog[i];
    if (r.url.includes("/term-review") && r.opts && r.opts.body) {
      const part = r.opts.body.parts.find(([k]) => k === "terms");
      return part ? part[1] : null;
    }
  }
  return null;
}

// ---------- 场景 ----------

async function test_manual_upload_binds_result() {
  clearTermState();
  state.project = proj(1, "t1");
  termForm.terms.files = ["csvA"];
  const p = submitTerms("csvA");
  assert.strictEqual(termPostCount(), 1, "应发出一次审查请求");
  respondJson(popPending("term-review"), termResult(1, "t1", 2));
  await p; await flush();
  assert.ok(state.termReview, "报告应展示");
  assert.strictEqual(state.termReview.text_hash, "t1");
  assert.strictEqual(state.termReview.project_id, 1);
  assert.strictEqual(state.termReview.hit_count, 2);
  assert.strictEqual(app.submittedTermsFile(), "csvA",
                     "提交后应记住已提交词表快照");
  console.log("frontend manual upload binds project+text_hash ok");
}

async function test_text_change_auto_recheck() {
  // 文案修订改变 text_hash（t1 → t2）：旧报告立即作废，
  // 自动用最近一次已提交的词表按新文案重新审查
  state.project = proj(1, "t2");
  app.render();
  assert.strictEqual(state.termReview, null, "旧报告必须立即作废");
  assert.ok(state.termNotice.includes("重新审查"), "应提示正在重查");
  assert.strictEqual(termPostCount(), 2, "应自动发起重新审查");
  assert.strictEqual(lastPostedFile(), "csvA",
                     "自动重查必须使用已提交词表快照");
  respondJson(popPending("term-review"), termResult(1, "t2", 1));
  await flush();
  assert.ok(state.termReview, "自动重查结果应展示");
  assert.strictEqual(state.termReview.text_hash, "t2");
  assert.strictEqual(state.termReview.hit_count, 1);
  console.log("frontend text change -> invalidate + auto re-check ok");
}

async function test_time_only_change_keeps_result() {
  // 单改时间参数（最小间隔/容差/采纳）：timeline_hash 变、text_hash 不变
  // → 报告保留，不发多余请求
  state.project = proj(1, "t2", "tl-changed");
  const before = termPostCount();
  app.render();
  assert.strictEqual(termPostCount(), before, "不应发起新审查");
  assert.ok(state.termReview, "有效报告应保留");
  assert.strictEqual(state.termReview.text_hash, "t2");
  console.log("frontend time-only change keeps result ok");
}

async function test_concurrent_submits_last_wins() {
  // 连续两次提交：第一次的响应迟到，不得覆盖第二次提交的结果
  clearTermState();
  state.project = proj(1, "t2");
  termForm.terms.files = ["csvB"];
  const p1 = submitTerms("csvB");
  const req1 = popPending("term-review");
  termForm.terms.files = ["csvB2"];
  const p2 = submitTerms("csvB2");
  const req2 = popPending("term-review");
  respondJson(req2, termResult(1, "t2", 5));   // 第二次先返回
  await p2; await flush();
  assert.strictEqual(state.termReview.hit_count, 5);
  assert.strictEqual(app.submittedTermsFile(), "csvB2",
                     "快照应为最近一次提交的词表");
  respondJson(req1, termResult(1, "t2", 99));  // 第一次迟到
  await p1; await flush();
  assert.strictEqual(state.termReview.hit_count, 5,
                     "迟到响应不得覆盖最近一次提交的结果");
  console.log("frontend concurrent submits: last response wins ok");
}

async function test_switch_project_discards_inflight() {
  // 提交未完成时切换项目：迟到响应不得落到新项目
  clearTermState();
  state.project = proj(1, "t2");
  termForm.terms.files = ["csvC"];
  const p = submitTerms("csvC");
  const req = popPending("term-review");
  app.resetTermReview();            // 切换项目的前置清理（在途失效）
  state.project = proj(2, "k1");
  respondJson(req, termResult(1, "t2", 7));
  await p; await flush();
  assert.strictEqual(state.termReview, null,
                     "切换项目后迟到响应必须丢弃");
  console.log("frontend switch project discards in-flight response ok");
}

async function test_clear_input_keeps_submitted_snapshot() {
  // 提交词表甲后**清空文件框**，甲快照仍可读取 → 文案变化后自动重查继续
  // 用甲表，而不是提示重传。
  clearTermState();
  state.project = proj(2, "k1");
  termForm.terms.files = ["csvA-submitted"];
  const p = submitTerms("csvA-submitted");
  respondJson(popPending("term-review"), termResult(2, "k1", 3));
  await p; await flush();
  assert.ok(state.termReview);
  termForm.terms.files = [];             // 用户清空了文件框
  assert.strictEqual(app.submittedTermsFile(), "csvA-submitted",
                     "清空文件框不影响已提交词表快照");
  state.project = proj(2, "k2");
  app.render();
  assert.strictEqual(state.termReview, null, "旧报告必须立即作废");
  assert.strictEqual(lastPostedFile(), "csvA-submitted",
                     "清空文件框后仍须用甲表快照自动重查");
  respondJson(popPending("term-review"), termResult(2, "k2", 2));
  await flush();
  assert.ok(state.termReview, "甲表快照的重查结果应展示");
  assert.strictEqual(state.termReview.text_hash, "k2");
  console.log("frontend clear input keeps submitted snapshot ok");
}

async function test_reselect_without_submit_ignored_on_recheck() {
  // 核心回归（不得把乙表的结论当作甲表的）：提交甲表后只在文件框改选乙表
  // 而**不提交**，随后文案变化——自动重查必须携带甲表，
  // 不得读取文件框当前的乙表。
  clearTermState();
  state.project = proj(3, "m1");
  termForm.terms.files = ["terms-A"];
  const p = submitTerms("terms-A");
  respondJson(popPending("term-review"), termResult(3, "m1", 2));
  await p; await flush();
  assert.strictEqual(app.submittedTermsFile(), "terms-A");
  termForm.terms.files = ["terms-B"];    // 只改选，不提交
  state.project = proj(3, "m2");         // 文案变化
  app.render();
  assert.strictEqual(state.termReview, null, "旧报告必须立即作废");
  assert.strictEqual(lastPostedFile(), "terms-A",
                     "改选未提交的乙表不得参与自动重查，必须仍用甲表");
  assert.ok(state.termNotice.includes("重新审查"));
  respondJson(popPending("term-review"), termResult(3, "m2", 4));
  await flush();
  assert.ok(state.termReview, "应展示甲表（而非乙表）的重查结论");
  assert.strictEqual(state.termReview.hit_count, 4);
  assert.strictEqual(app.submittedTermsFile(), "terms-A",
                     "乙表未提交，不得取代甲表快照");
  console.log("frontend reselect without submit ignored on re-check ok");
}

async function test_submit_b_replaces_a_then_recheck_uses_b() {
  // 接上一情境：乙表只有在**提交之后**才可取代甲表——提交乙表后文案变化，
  // 自动重查必须携带乙表。
  state.project = proj(3, "m3");
  termForm.terms.files = ["terms-B"];    // 此时文件框已是乙表
  const p = submitTerms("terms-B");      // 提交：切换边界
  assert.strictEqual(app.submittedTermsFile(), "terms-B",
                     "提交后乙表取代甲表");
  respondJson(popPending("term-review"), termResult(3, "m3", 2));
  await p; await flush();
  state.project = proj(3, "m4");
  app.render();
  assert.strictEqual(lastPostedFile(), "terms-B",
                     "提交乙表后自动重查应使用乙表");
  respondJson(popPending("term-review"), termResult(3, "m4", 1));
  await flush();
  assert.strictEqual(state.termReview.text_hash, "m4");
  console.log("frontend submit B replaces A; re-check uses B ok");
}

async function test_unreadable_snapshot_asks_reupload() {
  // 已提交词表快照已不可读（如浏览器回收文件句柄）：自动重查请求无法
  // 携带该文件而失败 → 作废旧报告并提示重新上传，不回退文件框改选稿。
  clearTermState();
  state.project = proj(2, "k1");
  termForm.terms.files = ["csvGone"];
  const p = submitTerms("csvGone");
  respondJson(popPending("term-review"), termResult(2, "k1", 2));
  await p; await flush();
  assert.ok(state.termReview);
  termForm.terms.files = ["terms-B"];    // 文件框改选了乙表但未提交
  state.termSnapshot.file = { unreadable: true };  // 甲表快照已不可读
  state.project = proj(2, "k2");
  app.render();
  respondJson(popPending("term-review"),
              { detail: "词表无法读取" }, false, 400);
  await flush();
  assert.strictEqual(state.termReview, null, "不可读时不得保留旧报告");
  assert.ok(state.termNotice.includes("重新上传"), "应提示重新上传");
  console.log("frontend unreadable snapshot -> ask re-upload ok");
}

async function test_no_snapshot_asks_reupload() {
  // 已展示报告后快照不可用（非文件框清空）：文案变化后不得自动重查，
  // 作废旧报告并提示重新上传。
  clearTermState();
  state.project = proj(6, "n1");
  termForm.terms.files = ["csvN"];
  const p = submitTerms("csvN");
  respondJson(popPending("term-review"), termResult(6, "n1", 1));
  await p; await flush();
  assert.ok(state.termReview);
  state.termSnapshot = null;             // 已提交词表不可用
  const before = termPostCount();
  state.project = proj(6, "n2");
  app.render();
  await flush();
  assert.strictEqual(termPostCount(), before, "无快照不得发起重查");
  assert.strictEqual(state.termReview, null, "旧报告必须作废");
  assert.ok(state.termNotice.includes("重新上传"), "应提示重新上传");
  console.log("frontend no snapshot -> invalidate + ask re-upload ok");
}

async function test_stale_on_arrival_rechecked() {
  // 响应等待期间另一标签页改了文案（本地已刷新到 k3）：
  // 按旧文案 k2 算出的报告到达即过期 → 先拉取最新详情（仍为 k3）裁决，
  // 报告不可接纳 → 自动用已提交词表快照按新文案重查，不展示过期报告
  clearTermState();
  state.project = proj(2, "k2");
  termForm.terms.files = ["csvE"];
  const p = submitTerms("csvE");
  const req = popPending("term-review");
  state.project = proj(2, "k3");        // 文案在等待期间变化
  const before = termPostCount();
  respondJson(req, termResult(2, "k2", 4));
  await flush();
  respondJson(popPendingExact("/api/projects/2"), proj(2, "k3"));  // 详情仍 k3
  await flush();
  assert.strictEqual(termPostCount(), before + 1,
                     "到达即过期应自动按新文案重查");
  assert.strictEqual(lastPostedFile(), "csvE",
                     "跨标签页改文案后的重查仍须用已提交词表快照");
  assert.ok(!state.termReview || state.termReview.text_hash === "k3",
            "过期报告不得展示");
  respondJson(popPending("term-review"), termResult(2, "k3", 4));
  await p; await flush();
  assert.strictEqual(state.termReview.text_hash, "k3");
  console.log("frontend stale-on-arrival discarded + re-checked ok");
}

async function test_cross_tab_change_accepted_after_detail_refresh() {
  // 另一标签页改了文案，本页详情仍旧（c1）。审查响应携带新指纹 c2——
  // 先拉取最新详情（c2），报告按新详情可接纳：直接展示、页面详情同步到
  // 最新，且不再发起任何重查请求（无循环请求）。
  clearTermState();
  state.project = proj(7, "c1");               // 本页详情过期
  termForm.terms.files = ["csvX"];
  const base = termPostCount();
  const p = submitTerms("csvX");
  respondJson(popPending("term-review"), termResult(7, "c2", 4));
  await flush();
  respondJson(popPendingExact("/api/projects/7"), proj(7, "c2"));  // 最新详情
  await p; await flush();
  assert.strictEqual(termPostCount(), base + 1,
                     "报告可按新详情接纳时不得重复发起审查（无循环请求）");
  assert.ok(state.termReview, "按新详情有效的报告应展示");
  assert.strictEqual(state.termReview.text_hash, "c2");
  assert.strictEqual(state.termReview.hit_count, 4);
  assert.strictEqual(state.project.text_hash, "c2",
                     "页面详情应同步到最新");
  console.log("frontend cross-tab change accepted after detail refresh ok");
}

async function test_repeated_text_changes_stop_recheck() {
  // 多次文案变化：每次响应到达时文案都已再次变化（详情也一路变化），
  // 自动重查必须有终止条件——达到上限后停止、作废旧报告并提示重传，
  // 不无界发请求。
  clearTermState();
  state.project = proj(4, "r1");
  termForm.terms.files = ["csvR"];
  const base = termPostCount();
  const p = submitTerms("csvR");          // 第 1 次审查
  respondJson(popPending("term-review"), termResult(4, "r2", 1));
  await flush();
  respondJson(popPendingExact("/api/projects/4"), proj(4, "r3"));
  await flush();                                          // 仍不符 → 第 2 次
  respondJson(popPending("term-review"), termResult(4, "r4", 1));
  await flush();
  respondJson(popPendingExact("/api/projects/4"), proj(4, "r5"));
  await flush();                                          // 仍不符 → 第 3 次
  respondJson(popPending("term-review"), termResult(4, "r6", 1));
  await flush();
  respondJson(popPendingExact("/api/projects/4"), proj(4, "r7"));
  await p; await flush();                                 // 达到上限 → 停止
  assert.strictEqual(termPostCount(), base + 3,
                     "自动重查链必须有终止条件（最多 3 次审查请求）");
  assert.strictEqual(state.termReview, null,
                     "达到上限后不得显示旧报告");
  assert.ok(state.termNotice.includes("重新上传"), "应提示重新上传");
  console.log("frontend repeated text changes -> bounded stop ok");
}

async function test_detail_refresh_failure_stops() {
  // 详情刷新失败（如项目已被删除/网络错误）：停止并提示重传，
  // 不显示旧报告，也不再发审查请求
  clearTermState();
  state.project = proj(5, "f1");
  termForm.terms.files = ["csvF"];
  const base = termPostCount();
  const p = submitTerms("csvF");
  respondJson(popPending("term-review"), termResult(5, "f2", 1));
  await flush();
  respondJson(popPendingExact("/api/projects/5"),
              { detail: "项目不存在" }, false, 404);
  await p; await flush();
  assert.strictEqual(termPostCount(), base + 1,
                     "详情刷新失败不得继续重查");
  assert.strictEqual(state.termReview, null, "不得显示旧报告");
  assert.ok(state.termNotice.includes("重新上传"), "应提示重新上传");
  console.log("frontend detail refresh failure -> stop + ask re-upload ok");
}

async function test_manual_csv_error_keeps_old_result() {
  // 手动提交非法词表（400）：只弹错，已展示的旧报告保留、不清空
  clearTermState();
  state.project = proj(8, "u1");
  termForm.terms.files = ["csvGood"];
  const p = submitTerms("csvGood");
  respondJson(popPending("term-review"), termResult(8, "u1", 2));
  await p; await flush();
  assert.ok(state.termReview);
  termForm.terms.files = ["csvBad"];
  const p2 = submitTerms("csvBad");       // 提交即换快照（提交是切换边界）
  respondJson(popPending("term-review"),
              { detail: "词表存在空字段" }, false, 400);
  await p2; await flush();
  assert.ok(state.termReview, "手动提交失败不得清空已展示的旧报告");
  assert.strictEqual(state.termReview.hit_count, 2);
  console.log("frontend manual csv error keeps old result ok");
}

async function main() {
  await test_manual_upload_binds_result();
  await test_text_change_auto_recheck();
  await test_time_only_change_keeps_result();
  await test_concurrent_submits_last_wins();
  await test_switch_project_discards_inflight();
  await test_clear_input_keeps_submitted_snapshot();
  await test_reselect_without_submit_ignored_on_recheck();
  await test_submit_b_replaces_a_then_recheck_uses_b();
  await test_unreadable_snapshot_asks_reupload();
  await test_no_snapshot_asks_reupload();
  await test_stale_on_arrival_rechecked();
  await test_cross_tab_change_accepted_after_detail_refresh();
  await test_repeated_text_changes_stop_recheck();
  await test_detail_refresh_failure_stops();
  await test_manual_csv_error_keeps_old_result();
  console.log("frontend term review binding regression ok");
}

main().catch((e) => { console.error(e); process.exit(1); });
