"use strict";
// EIG Sandbox Agent UI. Every value from the API (including model output and text the sandbox
// read from web pages) is inserted with textContent, never as HTML.

const $ = (sel) => document.querySelector(sel);

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

const usd = (x, d = 4) => "$" + Number(x || 0).toFixed(d);
const secs = (ms) => (ms / 1000).toFixed(1) + " s";
const shortHash = (h) => (h ? h.slice(0, 12) + "…" : "–");
const time = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");

let status = null;
let token = "";
let pollTimer = null;
let clockTimer = null;
let busy = false;

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (opts.auth && token) headers.Authorization = "Bearer " + token;
  const res = await fetch(path, { method: opts.method || "GET", headers, cache: "no-store",
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  let data = {};
  try { data = await res.json(); } catch (e) { data = {}; }
  return { ok: res.ok, status: res.status, data };
}

// --- tiny Markdown renderer (headings, bullets, tables, bold, code), DOM only ---------------
function inline(text) {
  const out = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const t = m[0];
    out.push(t.startsWith("**") ? el("strong", null, t.slice(2, -2)) : el("code", null, t.slice(1, -1)));
    last = m.index + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function markdown(md) {
  const root = el("div", { class: "md" });
  const lines = (md || "").split("\n");
  let list = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trimEnd();
    if (/^\s*\|/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
      i--;
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const body = rows.filter((r) => !/^\s*\|[\s:|-]+\|\s*$/.test(r));
      root.append(el("div", { class: "table-wrap" }, el("table", null,
        el("thead", null, el("tr", null, cells(body[0]).map((c) => el("th", null, inline(c))))),
        el("tbody", null, body.slice(1).map((r) => el("tr", null, cells(r).map((c) => el("td", null, inline(c)))))))));
      list = null;
    } else if (/^#{1,6}\s/.test(line)) {
      root.append(el("h3", null, inline(line.replace(/^#+\s*/, ""))));
      list = null;
    } else if (/^\s*[-*]\s+/.test(line)) {
      if (!list) { list = el("ul"); root.append(list); }
      list.append(el("li", null, inline(line.replace(/^\s*[-*]\s+/, ""))));
    } else if (line.trim()) {
      root.append(el("p", null, inline(line)));
      list = null;
    }
  }
  return root;
}

// --- status chips -----------------------------------------------------------------------------
let clockOffset = 0;

async function loadStatus() {
  const r = await api("/api/status");
  if (!r.ok) { $("#status-chips").replaceChildren(el("li", { class: "chip bad" }, "Orchestrator unavailable")); return; }
  status = r.data;
  clockOffset = status.server_time * 1000 - Date.now();
  renderChips();
  $("#token-note").textContent = status.approvals_enabled
    ? "Approvals are enabled. The token is kept in this tab's memory only and sent with approve / reject."
    : "Approvals are disabled on this server (no approver token configured): proposals can be seen but not decided.";
}

function renderChips() {
  if (!status) return;
  const s = status;
  const prov = s.provider === "mock" ? "MOCK provider (offline, no model called)" : "Vultr Serverless Inference";
  const chips = [
    el("li", { class: "chip" + (s.provider === "mock" ? " mid" : " good") }, prov),
    s.provider !== "mock" ? el("li", { class: "chip" }, "Model ", el("b", null, s.models.reasoning || "–")) : null,
    el("li", { class: "chip" }, "Evidence: ", el("b", null, s.evidence_path === "vision" ? "screenshot → vision model" : "screenshot → OCR → model")),
    el("li", { class: "chip" }, "Spend today ", el("b", null, usd(s.spend.spent_usd, 4)), " of ", usd(s.spend.cap_usd, 2)),
  ];
  if (s.auto_reset && s.auto_reset.next_at) {
    const left = Math.max(0, s.auto_reset.next_at * 1000 - (Date.now() + clockOffset));
    const m = Math.floor(left / 60000), sec = Math.floor((left % 60000) / 1000);
    chips.push(el("li", { class: "chip" }, "Demo resets in ", el("b", { class: "mono" }, `${m}:${String(sec).padStart(2, "0")}`)));
  }
  $("#status-chips").replaceChildren(...chips.filter(Boolean));
}

// --- runs -------------------------------------------------------------------------------------
const STEP_LABELS = {
  plan: "Plan the reads", sandbox: "Sandbox reads the county site", seal: "Results Seal (code)",
  triage: "Model verdicts (2 independent)", gate: "Gate decision (code)", report: "Incident report",
  evidence: "Model reads the screenshot evidence", extract: "Translations: extract facts",
  compare: "Translations: compare (code)", explain: "Translations: explain",
  read: "Sandbox opens the red-team page", drill: "Fresh sandbox does what the injection asks",
  restore: "Orchestrator restores certified results", verify: "Fresh sandbox re-reads, seal re-run",
};
const KIND_TITLES = { check: "Integrity check", redteam: "Red-team drill", approve: "Approve and restore" };

function setBusy(on) {
  busy = on;
  for (const id of ["#btn-check", "#btn-tamper", "#btn-redteam"]) $(id).disabled = on;
  document.querySelectorAll(".decide-btn").forEach((b) => { b.disabled = on; });
}

async function startTask(kind) {
  const body = kind === "check" ? { kind, goal: $("#goal").value } : { kind };
  setBusy(true);
  const r = await api("/api/tasks", { method: "POST", body });
  if (!r.ok) { setBusy(false); showRunError(KIND_TITLES[kind], r.data.error || `HTTP ${r.status}`); return; }
  watch(r.data, kind);
}

function showRunError(title, msg) {
  $("#run").hidden = false;
  $("#run-title").textContent = title;
  $("#run-kind").textContent = "";
  $("#steps").replaceChildren();
  $("#result").replaceChildren();
  const e = $("#run-error");
  e.textContent = msg;
  e.hidden = false;
}

function watch(job, kind) {
  $("#run").hidden = false;
  $("#run-title").textContent = KIND_TITLES[kind] || kind;
  $("#run-kind").textContent = status && status.provider === "mock" ? "MOCK provider" : "live";
  $("#run-error").hidden = true;
  $("#result").replaceChildren();
  $("#steps").replaceChildren();
  $("#run").scrollIntoView({ behavior: "smooth", block: "start" });
  const started = Date.now();
  clearInterval(clockTimer);
  clockTimer = setInterval(() => { $("#run-timer").textContent = secs(Date.now() - started); }, 100);
  clearTimeout(pollTimer);
  const poll = async () => {
    const r = await api("/api/jobs/" + job.id);
    if (!r.ok) { finish(); showRunError(KIND_TITLES[kind], r.data.error || "lost the job"); return; }
    renderSteps(r.data);
    if (r.data.status === "running") { pollTimer = setTimeout(poll, 700); return; }
    finish();
    if (r.data.status === "error") { showRunError(KIND_TITLES[kind], r.data.error); return; }
    $("#run-timer").textContent = secs((r.data.finished_at - r.data.started_at) * 1000);
    renderResult(r.data.result || {});
    refreshSide();
  };
  const finish = () => { clearInterval(clockTimer); setBusy(false); };
  poll();
}

function renderSteps(job) {
  const items = Object.entries(job.steps || {}).map(([key, s]) => {
    const t = s.finished_at ? secs((s.finished_at - s.started_at) * 1000) : "";
    return el("li", { class: "s-" + (s.status || "pending") },
      el("span", { class: "icon", "aria-hidden": "true" }),
      el("span", null, STEP_LABELS[key] || key, s.detail ? el("span", { class: "detail" }, s.detail) : null),
      el("span", { class: "secs" }, t));
  });
  $("#steps").replaceChildren(...items);
}

function box(label, value, cls, sub) {
  return el("div", { class: "box " + (cls || "neutral") }, el("div", { class: "k" }, label),
    el("div", { class: "v" }, value), sub ? el("div", { class: "sub" }, sub) : null);
}

const ACTION_STYLE = {
  NONE: ["good", "No action", "Live results match the certified seal."],
  ROLLBACK: ["bad", "Rollback proposed", "Waiting for an official in section 2."],
  ESCALATE: ["mid", "Escalate to a person", "The evidence is not strong enough for the agent to propose a restore."],
  HUMAN_SIGNOFF: ["mid", "Human sign-off", "Looks legitimate; an official must sign off and re-seal."],
};

function sandboxCard(sb, title) {
  if (!sb) return null;
  const life = (sb.lifecycle || []).map((e) => el("li", { class: e.event === "killed" ? "bad" : e.event === "removed" && e.confirmed_gone ? "good" : "" },
    e.event === "removed" && e.confirmed_gone ? "removed ✓" : e.event));
  const L = sb.limits || {};
  const limits = [`${L.memory || "1g"} RAM`, `${L.cpus || 1} CPU`, `${L.pids || 256} PIDs`, `${L.timeout_s || 60} s cap`,
    "read-only FS", "cap_drop ALL", "empty env", `net: ${L.network || "internal"}`];
  const steps = (sb.steps || []).map((s) => el("tr", null,
    el("td", null, s.action), el("td", null, el("span", { class: "mono" }, s.target || "")),
    el("td", null, s.ok ? el("span", { class: "badge good" }, "ok") : el("span", { class: "badge bad" }, "failed")),
    el("td", { class: "num" }, s.ms != null ? s.ms + " ms" : ""),
    el("td", null, s.error ? el("span", { class: "mono small" }, s.error.split("\n")[0]) : "")));
  return el("div", { class: "card sandbox" },
    el("div", { class: "card-head" }, el("h3", null, title || "Sandbox"),
      el("span", { class: "badge " + (sb.status === "OK" ? "good" : sb.status === "TIMEOUT" ? "mid" : "bad") }, sb.status)),
    el("p", { class: "note" }, "Container ", el("code", null, (sb.container_id || "").slice(0, 12) || "–"),
      ` · uid ${sb.uid ?? "–"} · ${sb.duration_ms != null ? secs(sb.duration_ms) : ""}`),
    el("ol", { class: "lifecycle", "aria-label": "Container lifecycle" }, life),
    el("ul", { class: "limits", "aria-label": "Limits" }, limits.map((l) => el("li", null, l))),
    steps.length ? el("details", null, el("summary", null, `${steps.length} step${steps.length > 1 ? "s" : ""} inside the sandbox`),
      el("div", { class: "table-wrap" }, el("table", null,
        el("thead", null, el("tr", null, ["Action", "Target", "", "Time", "Error"].map((h) => el("th", null, h)))),
        el("tbody", null, steps)))) : null);
}

function evidenceShots(sb, names) {
  if (!sb || !sb.job_id) return null;
  const figs = names.map(([file, caption]) => el("figure", { class: "shot" },
    el("a", { href: `/api/sandbox/${sb.job_id}/${file}`, target: "_blank", rel: "noopener" },
      el("img", { src: `/api/sandbox/${sb.job_id}/${file}`, alt: caption + " (screenshot taken inside the sandbox)", loading: "lazy" })),
    el("figcaption", null, caption)));
  return el("div", { class: "shots" }, figs);
}

function renderCheck(r) {
  const nodes = [];
  if (r.status === "TIMEOUT" || r.status === "FAILED") {
    nodes.push(el("div", { class: "verdict" }, box("Run", r.status, "mid", r.error)));
    nodes.push(sandboxCard(r.sandbox, "Sandbox"));
    return nodes;
  }
  const seal = r.seal || {};
  const d = r.decision || {};
  const [cls, label, sub] = ACTION_STYLE[d.action] || ["neutral", d.action, ""];
  nodes.push(el("div", { class: "verdict" },
    box("Results Seal (code)", seal.match ? "MATCH" : "MISMATCH", seal.match ? "good" : "bad",
      `certified ${shortHash(seal.certified_sha256)} · live ${shortHash(seal.live_sha256)}`),
    box("Gate decision", label, cls, d.reason || sub)));

  nodes.push(el("div", { class: "two" },
    el("div", null, el("h3", null, "What the sandbox saw"),
      evidenceShots(r.sandbox, [["results_page.png", "Live results page"]]),
      el("p", { class: "note" },
        el("a", { href: `/api/sandbox/${r.sandbox.job_id}/results_page.ocr.txt`, target: "_blank", rel: "noopener" }, "OCR text"), " · ",
        el("a", { href: `/api/sandbox/${r.sandbox.job_id}/results_page.txt`, target: "_blank", rel: "noopener" }, "DOM text"))),
    el("div", null, sandboxCard(r.sandbox, "Sandbox run"))));

  if (!seal.match) {
    const rows = (seal.diff || []).slice(0, 20).map((x) => el("tr", null,
      el("td", null, el("span", { class: "mono" }, x.path)), el("td", { class: "num" }, String(x.certified)),
      el("td", { class: "num bad-text" }, String(x.live))));
    nodes.push(el("h3", null, "What changed (computed, not guessed)"),
      el("div", { class: "table-wrap" }, el("table", null,
        el("thead", null, el("tr", null, el("th", null, "Field"), el("th", null, "Certified"), el("th", null, "Live"))),
        el("tbody", null, rows))));
    if ((seal.findings || []).length) {
      nodes.push(el("ul", { class: "evidence" }, seal.findings.map((f) =>
        el("li", null, el("span", { class: "badge " + (f.severity === "BLOCK" ? "bad" : "mid") }, f.severity), " ",
          el("code", null, f.rule_id), " ", f.title))));
    }
    const votes = (r.votes || []).filter(Boolean);
    if (votes.length) {
      nodes.push(el("h3", null, "Model verdicts"), el("div", { class: "verdict" }, votes.map((v, i) =>
        box(`Verdict ${i + 1}`, `${v.verdict} · ${Number(v.confidence).toFixed(2)}`,
          v.verdict === "tampering" ? "bad" : v.verdict === "legitimate" ? "good" : "mid", v.summary))));
    }
    const n = r.evidence_note;
    if (n && !n.error) {
      nodes.push(el("p", { class: "note" }, el("strong", null, `Evidence note (${n.source}): `),
        `page shows the change: ${n.page_shows_changed_values}; severity ${n.severity}. ${n.explanation}`));
    }
    if ((r.risk_signals || []).length) {
      nodes.push(el("details", null, el("summary", null, `Risk signals for the official (${r.risk_signals.length})`),
        el("ul", { class: "evidence" }, r.risk_signals.map((s) => el("li", null, el("strong", null, s.title), " — ", s.detail)))));
    }
    if (r.incident_report_md) nodes.push(el("h3", null, "Incident report (model draft)"), markdown(r.incident_report_md));
  }
  const ml = r.multilingual;
  if (ml) {
    const text = ml.skipped ? `Translations: skipped (${ml.skipped}).`
      : `Translations: ${(ml.mismatches || []).length} mismatch(es) across ${(ml.pages_read || []).join(", ")}.`;
    nodes.push(el("p", { class: "note" }, text));
  }
  if (r.proposal) {
    nodes.push(el("p", { class: "callout bad" }, "Rollback proposed. ",
      el("a", { href: "#decide" }, "Decide in section 2"), " — nothing changes until an official approves."));
  }
  return nodes;
}

function renderRedteam(r) {
  const nodes = [];
  const s = r.summary || {};
  nodes.push(el("div", { class: "verdict" },
    box("Containment", s.contained ? "ALL CONTAINED" : `${s.breaches || 0} BREACH`, s.contained ? "good" : "bad",
      `${s.attempts || 0} injected actions tried in a fresh sandbox`)));
  if (r.injection) {
    nodes.push(el("h3", null, "Hidden from people, read by the agent"),
      el("blockquote", { class: "injection" }, r.injection.hidden_text || "(none found)"),
      evidenceShots(r.read, [["redteam.png", "What a person sees on the page"]]));
  }
  nodes.push(containmentTable(r.events || []));
  const env = (r.events || []).find((e) => e.env);
  if (env) {
    nodes.push(el("details", null, el("summary", null, "Full environment the sandbox printed"),
      el("pre", { class: "page" }, Object.entries(env.env).map(([k, v]) => `${k}=${v}`).join("\n"))));
  }
  nodes.push(el("div", { class: "two" }, sandboxCard(r.read, "Sandbox 1: read the page"), sandboxCard(r.drill, "Sandbox 2: obey the injection")));
  return nodes;
}

function containmentTable(events) {
  return el("div", { class: "table-wrap" }, el("table", { class: "contain" },
    el("thead", null, el("tr", null, ["", "Attempted action", "Target", "Stopped by", "Evidence"].map((h) => el("th", null, h)))),
    el("tbody", null, events.map((e) => el("tr", null,
      el("td", null, el("span", { class: "badge " + (e.verdict === "CONTAINED" ? "good" : "bad") }, e.verdict)),
      el("td", null, e.action), el("td", null, el("span", { class: "mono" }, e.target || "")),
      el("td", null, e.blocked_by || "—"),
      el("td", null, el("span", { class: "mono small" }, (e.evidence || "").split("\n")[0])))))));
}

function renderApprove(r) {
  const v = r.verify || {};
  return [
    el("div", { class: "verdict" },
      box("Restore (orchestrator)", "Certified results written", "neutral",
        `${shortHash(r.restore && r.restore.before_sha256)} → ${shortHash(r.restore && r.restore.after_sha256)}`),
      box("Re-verified in a fresh sandbox", v.seal_match ? "Seal MATCH" : "Seal still differs", v.seal_match ? "good" : "bad",
        `proposal ${r.proposal && r.proposal.id}: ${r.proposal && r.proposal.status}`)),
    sandboxCard(r.sandbox, "Verification sandbox"),
  ];
}

function renderResult(r) {
  const nodes = r.kind === "redteam" ? renderRedteam(r) : r.kind === "approve" ? renderApprove(r) : renderCheck(r);
  const t = r.totals;
  if (t && t.model_calls) {
    nodes.push(el("p", { class: "note" }, `${t.model_calls} model call(s) · ${t.prompt_tokens + t.completion_tokens} tokens · est. ${usd(t.cost_usd, 5)}`));
  }
  $("#result").replaceChildren(...nodes.filter(Boolean));
}

// --- tamper -------------------------------------------------------------------------------------
async function tamper() {
  setBusy(true);
  const r = await api("/api/simulate-tamper", { method: "POST", body: { mode: $("#tamper-mode").value } });
  setBusy(false);
  const msg = $("#tamper-msg");
  if (!r.ok) { msg.textContent = r.data.error || `HTTP ${r.status}`; msg.className = "hint error"; return; }
  const mayor = (r.data.mayor || []).map((c) => `${c.name} ${c.votes.toLocaleString()}`).join(" · ");
  msg.className = "hint warn-text";
  msg.textContent = `Tampered (${r.data.mode}). Live Mayor results now: ${mayor}. Run an integrity check to catch it.`;
  refreshSide();
}

// --- proposals ------------------------------------------------------------------------------------
async function loadProposals() {
  const r = await api("/api/proposals");
  if (!r.ok) return;
  const props = r.data.proposals || [];
  if (!props.length) { $("#proposals").replaceChildren(el("p", { class: "note" }, "No proposals yet. Simulate a tamper, then run a check.")); return; }
  $("#proposals").replaceChildren(...props.slice(0, 5).map((p) => {
    const pending = p.status === "PENDING_APPROVAL";
    const out = el("p", { class: "note", role: "status" });
    const act = (verb) => async () => {
      if (!token) { out.textContent = "Enter the approver token first."; out.className = "error"; return; }
      setBusy(true);
      const res = await api(`/api/proposals/${p.id}/${verb}`, { method: "POST", body: {}, auth: true });
      setBusy(false);
      if (!res.ok) {
        out.className = "error";
        out.textContent = { 401: "Token required.", 403: "That token is not valid.", 503: "Approvals are disabled on this server." }[res.status]
          || res.data.error || `HTTP ${res.status}`;
        return;
      }
      if (verb === "approve") watch(res.data, "approve"); else loadProposals();
      refreshSide();
    };
    return el("div", { class: "card proposal " + (pending ? "pending" : "") },
      el("div", { class: "card-head" }, el("h3", null, p.action.replace("_", " ").toLowerCase()),
        el("span", { class: "badge " + (pending ? "mid" : p.status.startsWith("RESTORED_VERIFIED") ? "good" : "") }, p.status.replaceAll("_", " "))),
      el("p", null, p.reason),
      el("p", { class: "note mono" }, `${p.id} · live ${shortHash(p.live_sha256)} → certified ${shortHash(p.certified_sha256)}`),
      pending ? el("div", { class: "actions" },
        el("button", { class: "primary decide-btn", type: "button", onclick: act("approve") }, "Approve restore"),
        el("button", { class: "decide-btn", type: "button", onclick: act("reject") }, "Reject")) : null,
      out);
  }));
  if (busy) document.querySelectorAll(".decide-btn").forEach((b) => { b.disabled = true; });
}

// --- ledger and containment -------------------------------------------------------------------------
function summarize(e) {
  const d = e.data || {};
  switch (e.type) {
    case "SANDBOX_RUN": return `${d.purpose} · ${d.status} · container ${d.container} · ${(d.lifecycle || []).join(" → ")}${d.removed ? " ✓" : ""}`;
    case "CHECK_COMPLETED": return `seal ${d.seal_match ? "MATCH" : "MISMATCH"} · ${d.decision}${(d.rule_findings || []).length ? " · " + d.rule_findings.join(", ") : ""}`;
    case "CHECK_FAILED": return `${d.status}: ${d.error || ""}`;
    case "PROPOSAL_CREATED": return `${d.id} ${d.action}`;
    case "PROPOSAL_APPROVED": case "PROPOSAL_REJECTED": return `${d.id} by ${d.by}`;
    case "RESTORE_EXECUTED": return `${shortHash(d.before_sha256)} → ${shortHash(d.after_sha256)}`;
    case "RESTORE_VERIFIED": case "RESTORE_UNVERIFIED": return `${d.id} seal ${d.seal_match ? "MATCH" : "differs"}`;
    case "INJECTION_FOUND": return `asks: ${[...(d.asks.read_files || []), d.asks.print_env ? "print env" : null, ...(d.asks.send_to || [])].filter(Boolean).join(", ")}`;
    case "CONTAINED": case "BREACH": return `${d.action} ${d.target || ""} — ${d.blocked_by || d.evidence || ""}`;
    case "REDTEAM_COMPLETED": return `${d.attempts} attempts · ${d.breaches} breaches`;
    case "SIMULATED_TAMPER": return `${d.mode} by ${d.by}`;
    case "RESET": return `${d.reason}${d.restored ? " · results restored" : ""}${(d.expired_proposals || []).length ? " · expired " + d.expired_proposals.join(", ") : ""}`;
    default: return JSON.stringify(d).slice(0, 120);
  }
}

const TYPE_CLASS = { CONTAINED: "good", RESTORE_VERIFIED: "good", BREACH: "bad", CHECK_FAILED: "mid", SIMULATED_TAMPER: "bad",
  PROPOSAL_CREATED: "mid", INJECTION_FOUND: "mid", RESTORE_UNVERIFIED: "bad" };

async function loadLedger() {
  const r = await api("/api/ledger");
  if (!r.ok) return;
  const v = r.data.verify || {};
  const badge = $("#ledger-badge");
  badge.className = "badge big " + (v.ok ? "good" : "bad");
  badge.textContent = v.ok ? `✓ Chain verified · ${v.entries} entries · head ${shortHash(v.head)}` : `✗ Chain broken at #${v.bad_seq}: ${v.error}`;
  const entries = (r.data.entries || []).slice().reverse();
  $("#ledger-table").replaceChildren(entries.length ? el("div", { class: "table-wrap" }, el("table", { class: "ledger" },
    el("thead", null, el("tr", null, ["#", "Time", "Event", "Detail", "Hash"].map((h) => el("th", null, h)))),
    el("tbody", null, entries.slice(0, 40).map((e) => el("tr", null,
      el("td", { class: "num" }, e.seq), el("td", { class: "nowrap" }, time(e.ts)),
      el("td", null, el("span", { class: "badge " + (TYPE_CLASS[e.type] || "") }, e.type)),
      el("td", null, summarize(e)),
      el("td", null, el("span", { class: "mono small", title: `prev ${e.prev_hash}` }, shortHash(e.entry_hash))))))))
    : el("p", { class: "note" }, "The ledger is empty."));

  const contained = (r.data.entries || []).filter((e) => e.type === "CONTAINED" || e.type === "BREACH").reverse();
  if (contained.length) {
    $("#containment-log").replaceChildren(containmentTable(contained.slice(0, 15).map((e) => ({
      verdict: e.type, action: e.data.action, target: e.data.target, blocked_by: e.data.blocked_by,
      evidence: `${time(e.ts)} · ${e.data.evidence || ""}` }))));
  }
}

function refreshSide() { loadStatus(); loadProposals(); loadLedger(); }

document.addEventListener("DOMContentLoaded", () => {
  $("#btn-check").addEventListener("click", () => startTask("check"));
  $("#btn-redteam").addEventListener("click", () => startTask("redteam"));
  $("#btn-tamper").addEventListener("click", tamper);
  $("#token").addEventListener("input", (e) => { token = e.target.value.trim(); });
  refreshSide();
  setInterval(renderChips, 1000);
  setInterval(() => { if (!busy) refreshSide(); }, 10000);
});
