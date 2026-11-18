"use strict";
// beta.15 UI acceptance: per-paper diagnosis, token-bound remedies, legacy
// recovery with model choice, and the journal-acronym quick save. Synthetic
// fixtures only; production services are never contacted.
const test = require("node:test");
const assert = require("node:assert/strict");
const { OP, RUN, fixture, ui: baseUI } = require("./helpers.cjs");

const BETA15_CAPS = ["keys-v1", "mineru-v1", "translation-policy-v1", "credential-reveal-v1",
  "model-profiles-v2", "model-test-role-v1", "pause-resume-v1", "pause-recovery-v1",
  "resume-model-choice-v1", "diagnosis-v1", "remedy-v1", "issues-ledger-v1", "acronyms-v1",
  "history-v1", "item-selection-v1"];

function ui(f) { return baseUI(f); }

function diagnosisFixture(view, { active = false, capabilities = BETA15_CAPS } = {}) {
  const f = fixture();
  f.state = { active, text: "synthetic current", view: structuredClone(view) };
  f.diagnosis = {
    run_id: view.id, key: "ABCDEFG2", citekey: "Sample2026", title: "示例",
    accepted: false, exit_code: 0, classification: [
      { category: "translation", label: "译文对照/内容问题",
        evidence: [{ code: "quote_source", message: "英文对照与原文不一致", file: "Sample2026_ZH.md", unit: 2 }] }],
    qa: { ok: false, errors: [{ code: "quote_source", message: "m" }] },
    artifacts: { files: ["EN", "ZH", "Note"], qc_marked: true }, log_tail: "tail",
    latest_attempt: { run_id: view.id, exit_code: 0 },
    paths: { report: "r.md", paper_folder: "papers/Sample2026" },
    ledger: { status: "needs_repair", reasons: ["产物验收未通过"], updated: "t", dispositions: 0 },
    remedies: [
      { id: "repair_zh", label: "定向修复译文", kind: "worker", available: true, reason: "" },
      { id: "retranslate_zh", label: "仅重新翻译中文", kind: "worker", available: true, reason: "" },
      { id: "retry_qc", label: "重跑质检", kind: "worker", available: false, reason: "不需要" },
      { id: "adopt_existing", label: "采纳现有产物", kind: "local", available: false, reason: "产物不齐" },
      { id: "fix_missing_pdf", label: "缺少正本：请到 Zotero 修复后复查", kind: "instruction", available: true },
      { id: "recheck", label: "复查此条目", kind: "ledger", available: true, reason: "" }]
  };
  f.remedyPreview = { preview: true, action: "repair_zh", steps: ["zh"], token: "synthetic-one-time-token",
    will_backup: ["Sample2026_ZH.md"], model_settings: "glm-5.3-flash", user_edit_conflicts: [], notes: [] };
  f.remedyApplied = { ok: true, action: "repair_zh", message: "处置完成", published: ["x"] };
  f.remedyBodies = [];
  f.operationStatus = "completed";
  let opCount = 0;
  Object.defineProperty(f, "operationResult", {
    get() { opCount += 1; return opCount === 1 ? f.remedyPreview : f.remedyApplied; },
    set(value) { opCount = -1; f.remedyApplied = value; f.remedyPreview = value; },
    configurable: true });
  f.override = r => {
    const path = new URL(r.url).pathname;
    if (path === "/v1/status") return { status: 200, data: { capabilities, ...structuredClone(f.state) } };
    if (path === "/v1/history") return { status: 200, data: { runs: [], active: f.state.active, capabilities: [] } };
    if (path === `/v1/diagnosis/${view.id}/ABCDEFG2`) return { status: 200, data: structuredClone(f.diagnosis) };
    if (path === "/v1/remedy") {
      const body = JSON.parse(r.body);
      f.remedyBodies.push(body);
      return { status: 200, data: { operation: OP } };
    }
    return undefined;
  };
  return f;
}

const BLOCKED_VIEW = papers => ({ id: RUN, status: "blocked", terminal: true, monitor_done: true,
  papers: papers ?? [{ key: "ABCDEFG2", citekey: "Sample2026", title: "示例", phase: "attention",
    phase_label: "需要处理", accepted: false, note: "已转入待处理" }] });

async function openDiagnosis(u) {
  await u.el("nav-task").emit("click");
  const row = u.el("status-rows").children[0];
  await row.children[row.children.length - 1].children[0].emit("click");
}

test("per-paper detail button opens a bounded diagnosis with executable actions", async () => {
  const f = diagnosisFixture(BLOCKED_VIEW());
  const u = ui(f); await u.controller.init();
  await openDiagnosis(u);
  assert.equal(u.el("diagnosis-section").hidden, false);
  assert.match(u.el("diagnosis-text").textContent, /译文对照\/内容问题/);
  assert.match(u.el("diagnosis-text").textContent, /quote_source/);
  const buttons = [...u.el("diagnosis-remedies").children];
  assert.ok(buttons.some(b => b.textContent === "定向修复译文" && !b.disabled));
  assert.ok(buttons.some(b => b.textContent === "重跑质检" && b.disabled));
  assert.ok(buttons.some(b => b.textContent === "仅重新翻译中文" && !b.disabled));
  // Instruction remedies render as guidance, never as fake retry buttons.
  assert.ok(!buttons.some(b => b.textContent.includes("缺少正本")));
  assert.match(u.el("diagnosis-result").textContent, /缺少正本/);
  u.controller.close();
});

test("old bridge explains missing diagnosis support instead of failing silently", async () => {
  const f = diagnosisFixture(BLOCKED_VIEW(), { capabilities: BETA15_CAPS.filter(c => c !== "diagnosis-v1") });
  const u = ui(f); await u.controller.init();
  await openDiagnosis(u);
  assert.equal(u.el("diagnosis-section").hidden, false);
  assert.match(u.el('diagnosis-text').textContent, /重启空闲后台/);
  assert.equal(u.el('diagnosis-section').scrollOptions.block, 'start');
  assert.match(u.el("message").textContent, /不支持单篇诊断/);
  assert.equal(f.requests.filter(r => r.url.includes("/v1/diagnosis")).length, 0);
  u.controller.close();
});

test('long task tables scroll a successful diagnosis into view', async () => {
  const f=diagnosisFixture(BLOCKED_VIEW(Array.from({length:30},(_,i)=>({key:'ABCDEFG2',citekey:'Sample'+i,accepted:false}))));
  const u=ui(f);await u.controller.init();await openDiagnosis(u);
  assert.equal(u.el('diagnosis-section').hidden,false);
  assert.equal(u.el('diagnosis-section').scrollOptions.block,'start');
  u.controller.close();
});

test('idle old backend is replaced and active backend cannot be restarted', async () => {
  const f=diagnosisFixture(BLOCKED_VIEW(),{capabilities:['history-v1']});
  const u=ui(f);let restarts=0;
  u.host.restartBackend=async pid=>{assert.equal(pid, f.manifest.pid);restarts++;f.state.backend={pid:9876,version:'0.2.0-beta.16'};};
  await u.controller.init();
  await u.el('restart-backend').emit('click');
  assert.equal(restarts,1);
  assert.match(u.el('backend-status').textContent,/0.2.0-beta.16/);
  f.state.active=true;await u.controller.tick();
  assert.equal(u.el('restart-backend').disabled,true);
  await u.controller.restartBackend();assert.equal(restarts,1);
  u.controller.close();
});

test("status refresh keeps the mounted diagnosis section", async () => {
  const f = diagnosisFixture(BLOCKED_VIEW());
  const u = ui(f); await u.controller.init();
  await openDiagnosis(u);
  await u.controller.tick();
  assert.equal(u.el("diagnosis-section").hidden, false);
  assert.match(u.el("diagnosis-text").textContent, /Sample2026/);
  u.controller.close();
});

test("remedy executes only after a token-bound preview and explicit confirmation", async () => {
  const f = diagnosisFixture(BLOCKED_VIEW());
  const u = ui(f); await u.controller.init();
  await openDiagnosis(u);
  const repair = [...u.el("diagnosis-remedies").children].find(b => b.textContent === "定向修复译文");
  await repair.emit("click");
  assert.equal(u.window.confirms.length, 1);
  assert.equal(f.remedyBodies.length, 2);
  assert.equal(f.remedyBodies[0].preview, true);
  assert.equal(f.remedyBodies[0].token, undefined);
  assert.equal(f.remedyBodies[1].confirm, true);
  assert.equal(f.remedyBodies[1].token, "synthetic-one-time-token");
  assert.match(u.el("diagnosis-result").textContent, /处置完成/);
  // The one-time token never reaches the DOM.
  assert.ok(!u.el("diagnosis-result").textContent.includes("synthetic-one-time-token"));
  u.controller.close();
});

test("declining the remedy confirmation executes nothing", async () => {
  const f = diagnosisFixture(BLOCKED_VIEW());
  const u = ui(f); await u.controller.init();
  u.window.confirmResult = false;
  await openDiagnosis(u);
  const repair = [...u.el("diagnosis-remedies").children].find(b => b.textContent === "定向修复译文");
  await repair.emit("click");
  assert.equal(f.remedyBodies.length, 1);  // preview only
  assert.equal(f.remedyBodies[0].preview, true);
  u.controller.close();
});

test("legacy recovery button appears, previews the model choice, then dispatches", async () => {
  const view = { ...BLOCKED_VIEW(), can_recover_pause: true, pause_requested: true };
  const f = diagnosisFixture(view);
  let resumePreviewBody = null, recoveryBody = null;
  f.override = r => {
    const path = new URL(r.url).pathname;
    if (path === "/v1/status") return { status: 200, data: { capabilities: BETA15_CAPS, ...structuredClone(f.state) } };
    if (path.startsWith("/v1/status/")) return { status: 200, data: { active: f.state.active,
      view: structuredClone(f.state.view ?? view), text: "synthetic detail" } };
    if (path === "/v1/history") return { status: 200, data: { runs: [], active: f.state.active, capabilities: [] } };
    if (path === "/v1/resume/preview") { resumePreviewBody = JSON.parse(r.body); return { status: 200, data: { operation: OP } }; }
    if (path === "/v1/recovery") { recoveryBody = JSON.parse(r.body); return { status: 200, data: { run_id: RUN + "_rec", started: true } }; }
    return undefined;
  };
  f.operationResult = { run_id: RUN, steps: { ABCDEFG2: ["qc", "zh"] }, needs_action: {},
    frozen_model_config: null, saved_model_config: { qc: { provider: "glm", model: "glm-5.3", reasoning_effort: "high" } },
    saved_model_ok: true, recovery: true, dry_run: true };
  const u = ui(f); await u.controller.init();
  await u.el("nav-task").emit("click");
  assert.equal(u.el("recover-pause").hidden, false);
  await u.el("recover-pause").emit("click");
  assert.deepEqual(resumePreviewBody, { run_id: RUN, recovery: true });
  // Both dialogs accepted: the recovery dispatch, then the saved-model choice.
  assert.equal(u.window.confirms.length, 2);
  assert.equal(recoveryBody.run_id, RUN);
  assert.equal(recoveryBody.confirm, true);
  assert.equal(recoveryBody.use_saved_model, true);
  u.controller.close();
});

test("resume with model-choice capability previews frozen vs saved config first", async () => {
  const view = { id: RUN, status: "paused", terminal: true, monitor_done: true, can_resume: true };
  const f = diagnosisFixture(view);
  let previewBody = null, resumeBody = null;
  f.override = r => {
    const path = new URL(r.url).pathname;
    if (path === "/v1/status") return { status: 200, data: { capabilities: BETA15_CAPS, ...structuredClone(f.state) } };
    if (path.startsWith("/v1/status/")) return { status: 200, data: { active: f.state.active,
      view: structuredClone(f.state.view ?? view), text: "synthetic detail" } };
    if (path === "/v1/history") return { status: 200, data: { runs: [], active: f.state.active, capabilities: [] } };
    if (path === "/v1/resume/preview") { previewBody = JSON.parse(r.body); return { status: 200, data: { operation: OP } }; }
    if (path === "/v1/resume") { resumeBody = JSON.parse(r.body); return { status: 200, data: { run_id: RUN + "_resume", started: true, resume_of: RUN } }; }
    return undefined;
  };
  f.operationResult = { run_id: RUN, steps: { ABCDEFG2: ["zh"] }, needs_action: {},
    frozen_model_config: { qc: { provider: "glm", model: "glm-5.3-flash", reasoning_effort: "default" } },
    saved_model_config: { qc: { provider: "glm", model: "glm-5.3", reasoning_effort: "high" } },
    saved_model_ok: true, dry_run: true };
  const u = ui(f); await u.controller.init();
  await u.el("nav-task").emit("click");
  await u.el("resume").emit("click");
  assert.deepEqual(previewBody, { run_id: RUN, recovery: false });
  assert.equal(u.window.confirms.length, 2);
  assert.match(u.window.confirms[0], /原任务冻结模型/);
  assert.match(u.window.confirms[1], /已保存/);
  assert.equal(resumeBody.run_id, RUN);
  assert.equal(resumeBody.use_saved_model, true);
  u.controller.close();
});

test("paused run resume defaults to the frozen config when the choice is declined", async () => {
  const view = { id: RUN, status: "paused", terminal: true, monitor_done: true, can_resume: true };
  const f = diagnosisFixture(view);
  let resumeBody = null;
  f.override = r => {
    const path = new URL(r.url).pathname;
    if (path === "/v1/status") return { status: 200, data: { capabilities: BETA15_CAPS, ...structuredClone(f.state) } };
    if (path.startsWith("/v1/status/")) return { status: 200, data: { active: f.state.active,
      view: structuredClone(f.state.view ?? view), text: "synthetic detail" } };
    if (path === "/v1/history") return { status: 200, data: { runs: [], active: f.state.active, capabilities: [] } };
    if (path === "/v1/resume/preview") return { status: 200, data: { operation: OP } };
    if (path === "/v1/resume") { resumeBody = JSON.parse(r.body); return { status: 200, data: { run_id: RUN + "_resume", started: true, resume_of: RUN } }; }
    return undefined;
  };
  f.operationResult = { run_id: RUN, steps: { ABCDEFG2: ["zh"] }, needs_action: {},
    frozen_model_config: { qc: { provider: "glm", model: "glm-5.3-flash", reasoning_effort: "default" } },
    saved_model_config: { qc: { provider: "glm", model: "glm-5.3", reasoning_effort: "high" } },
    saved_model_ok: true, dry_run: true };
  const u = ui(f); await u.controller.init();
  u.window.confirmResult = true;
  u.window.confirm = value => { u.window.confirms.push(value); return u.window.confirms.length < 2; };
  await u.el("nav-task").emit("click");
  await u.el("resume").emit("click");
  assert.equal(resumeBody.use_saved_model, undefined);  // omitted = frozen config
  u.controller.close();
});

test("unknown journal preview offers a journal-specific acronym save", async () => {
  const f = fixture();
  f.preview = { preview_id: RUN, text: "p", items: [], deferred: [
    { key: "ABCDEFG2", citekey: "New2026JPO-Paper", title: "新文献",
      unmapped_publication_title: "Synthetic Journal, Ocean Research",
      blocked: ["IntakeBlocked: missing/unmapped publicationTitle: 'Synthetic Journal, Ocean Research'"] }] };
  let acronymBody = null;
  f.override = r => {
    const path = new URL(r.url).pathname;
    if (path === "/v1/status") return { status: 200, data: { capabilities: BETA15_CAPS, ...f.state } };
    if (path === "/v1/acronyms") { acronymBody = JSON.parse(r.body); return { status: 200,
      data: { title: "Synthetic Journal, Ocean Research", proposed: "SJOR", changed: true, message: "已保存；重新预演后生效。" } }; }
    return undefined;
  };
  const u = ui(f); await u.controller.init();
  await u.controller.previewScope([]);
  await u.controller.tick();  // the preview operation completes with the payload
  assert.equal(u.el("acronym-section").hidden, false);
  assert.equal(u.el("acronym-title").value, "Synthetic Journal, Ocean Research");
  u.el("acronym-proposed").value = "SJOR";
  await u.el("acronym-save").emit("click");
  assert.deepEqual(acronymBody, { title: "Synthetic Journal, Ocean Research", proposed: "SJOR", confirm: true });
  assert.match(u.el("message").textContent, /重新预演/);
  u.controller.close();
});

test("acronym save is disabled without the acronyms-v1 capability", async () => {
  const f = fixture();
  f.preview = { preview_id: RUN, text: "p", items: [], deferred: [
    { key: "ABCDEFG2", citekey: "New2026JPO-Paper", title: "新文献",
      unmapped_publication_title: "Synthetic Journal",
      blocked: ["IntakeBlocked: missing/unmapped publicationTitle: 'Synthetic Journal'"] }] };
  const caps = BETA15_CAPS.filter(c => c !== "acronyms-v1");
  f.override = r => {
    const path = new URL(r.url).pathname;
    if (path === "/v1/status") return { status: 200, data: { capabilities: caps, ...f.state } };
    return undefined;
  };
  const u = ui(f); await u.controller.init();
  await u.controller.previewScope([]);
  await u.controller.tick();
  assert.equal(u.el("acronym-section").hidden, true);  // not even offered on old bridges
  u.controller.close();
});
