"use strict";
// Demo page for the Nemotron Integrity Agent. Every value from the API (including model
// output) is inserted with textContent, never as HTML.

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

const short = (m) => (m || "").split("/").pop();
const usd = (x) => "$" + Number(x || 0).toFixed(5);
const secs = (ms) => (ms / 1000).toFixed(1) + " s";

let scenarios = [];
let pollTimer = null;
let clockTimer = null;

// --- tiny Markdown renderer (headings, bullets, tables, bold, code), DOM only ---------
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
      const table = el("table", null,
        el("thead", null, el("tr", null, cells(body[0]).map((c) => el("th", null, inline(c))))),
        el("tbody", null, body.slice(1).map((r) => el("tr", null, cells(r).map((c) => el("td", null, inline(c)))))));
      root.append(el("div", { class: "table-wrap" }, table));
      list = null;
    } else if (/^#{1,6}\s/.test(line)) {
      root.append(el("h2", null, inline(line.replace(/^#+\s*/, ""))));
      list = null;
    } else if (/^\s*[-*]\s+/.test(line)) {
      if (!list) { list = el("ul", { class: "evidence" }); root.append(list); }
      list.append(el("li", null, inline(line.replace(/^\s*[-*]\s+/, ""))));
    } else if (line.trim()) {
      root.append(el("p", null, inline(line)));
      list = null;
    }
  }
  return root;
}

// --- scenario cards ------------------------------------------------------------------
const FEATURE_LABEL = { seal: "Results Seal + tamper triage", translations: "Multilingual consistency",
                        hunt: "Misinformation hunter (Tavily)" };

async function loadScenarios() {
  const res = await fetch("/api/scenarios");
  const data = await res.json();
  scenarios = data.scenarios;
  const routing = $("#routing");
  routing.replaceChildren(...data.routing.map((r) =>
    el("li", { title: r.why }, el("b", null, r.role), " ", short(r.model))));
  const grid = $("#scenarios");
  grid.replaceChildren(...scenarios.map((s) => el("article", { class: "card" },
    el("span", { class: "tag" }, FEATURE_LABEL[s.feature] || s.feature),
    el("h3", null, s.title),
    el("p", null, s.blurb),
    el("div", { class: "actions" },
      s.recorded ? el("button", { class: "primary", onclick: () => start(s, false) }, "Replay") : null,
      el("button", { onclick: () => start(s, true) }, "Run live")))));
}

function setButtons(disabled) {
  document.querySelectorAll(".card button").forEach((b) => { b.disabled = disabled; });
}

// --- running a scenario --------------------------------------------------------------
async function start(scenario, live) {
  clearInterval(pollTimer);
  clearInterval(clockTimer);
  const run = $("#run");
  run.hidden = false;
  $("#run-title").textContent = scenario.title;
  $("#run-mode").textContent = live ? "Live run" : "Recorded run";
  $("#run-error").hidden = true;
  $("#result").replaceChildren();
  renderSteps(scenario, {});
  run.scrollIntoView({ behavior: "smooth", block: "start" });
  setButtons(true);
  try {
    const res = await fetch("/api/run", { method: "POST", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify({ scenario: scenario.id, live }) });
    const job = await res.json();
    if (!res.ok) throw new Error(job.error || res.statusText);
    if (!live) {
      $("#run-mode").textContent = "Recorded run · " + (job.recorded_at || "").replace("T", " ").replace("+00:00", " UTC");
      return finish(scenario, job);
    }
    const t0 = Date.now();
    clockTimer = setInterval(() => { $("#run-timer").textContent = secs(Date.now() - t0); }, 200);
    pollTimer = setInterval(() => poll(scenario, job.id), 1000);
  } catch (err) {
    showError(err.message);
  }
}

async function poll(scenario, id) {
  try {
    const res = await fetch("/api/jobs/" + id);
    const job = await res.json();
    if (!res.ok) throw new Error(job.error || res.statusText);
    renderSteps(scenario, job.steps);
    if (job.status !== "running") finish(scenario, job);
  } catch (err) {
    showError(err.message);
  }
}

function showError(msg) {
  clearInterval(pollTimer);
  clearInterval(clockTimer);
  const e = $("#run-error");
  e.textContent = msg;
  e.hidden = false;
  setButtons(false);
}

function renderSteps(scenario, steps, done) {
  const now = Date.now() / 1000;
  $("#steps").replaceChildren(...scenario.steps.map((s) => {
    const st = steps[s.key];
    const status = st ? st.status : (done ? "skipped" : "pending");
    const dur = st ? ((st.finished_at || now) - st.started_at) : null;
    return el("li", { class: "s-" + status },
      el("span", { class: "icon", "aria-hidden": "true" }),
      el("span", null, s.label, st && st.detail ? el("span", { class: "detail" }, st.detail) :
                                (status === "skipped" ? el("span", { class: "detail" }, "not needed for this result") : null)),
      el("span", { class: "secs" }, dur !== null ? dur.toFixed(1) + " s" : ""));
  }));
}

function finish(scenario, job) {
  clearInterval(pollTimer);
  clearInterval(clockTimer);
  setButtons(false);
  renderSteps(scenario, job.steps || {}, true);
  $("#run-timer").textContent = job.finished_at ? secs((job.finished_at - job.started_at) * 1000) : "";
  if (job.status === "error") return showError("The run failed: " + job.error);
  const r = job.result;
  const out = $("#result");
  if (scenario.feature === "seal") out.append(...renderSeal(r));
  else if (scenario.feature === "translations") out.append(...renderTranslations(r));
  else out.append(...renderHunt(r));
  out.append(...renderCalls(r));
}

// --- result views --------------------------------------------------------------------
const ACTION = {
  NONE: ["good", "No action", "Live results match the certified seal."],
  HUMAN_SIGNOFF: ["mid", "Official sign-off", "Looks legitimate; an official must approve and re-seal."],
  ESCALATE: ["mid", "Escalate to a person", "Not enough evidence for the system to act."],
  ROLLBACK: ["bad", "Rollback recommended", "Restore the certified copy and notify the duty official, who confirms or reverses it."],
};
const VERDICT_CLASS = { legitimate: "good", tampering: "bad", uncertain: "mid" };
// One colour for the verdict box: the shared verdict's colour, or amber when the votes disagree.
function voteClass(votes) {
  const kinds = new Set(votes.map((v) => (v ? v.verdict : "error")));
  return kinds.size === 1 ? (VERDICT_CLASS[[...kinds][0]] || "neutral") : "mid";
}

function renderSeal(r) {
  const nodes = [];
  const seal = r.seal;
  const [cls, label, note] = ACTION[r.decision.action] || ["neutral", r.decision.action, ""];
  const t = r.triage;
  nodes.push(el("div", { class: "verdict" },
    el("div", { class: "box " + (seal.match ? "good" : "bad") }, el("div", { class: "k" }, "Results Seal"),
      el("div", { class: "v" }, seal.match ? "Match" : "Mismatch"),
      el("div", { class: "note mono" }, "certified " + seal.certified_sha256.slice(0, 12) + "… / live " + seal.live_sha256.slice(0, 12) + "…")),
    t ? el("div", { class: "box " + voteClass(r.votes || [t]) }, el("div", { class: "k" }, (r.votes || []).length > 1 ? "Nemotron verdicts (independent)" : "Nemotron verdict"),
      ...(r.votes || [t]).map((v) => el("div", { class: "v" }, v ? v.verdict + " · " + v.confidence.toFixed(2) : "no valid verdict")),
      el("div", { class: "note" }, "model suggests: " + t.recommended_action.replaceAll("_", " "))) :
      el("div", { class: "box neutral" }, el("div", { class: "k" }, "Nemotron verdict"),
        el("div", { class: "v" }, seal.match ? "not called" : "no valid verdict"),
        el("div", { class: "note" }, r.triage_error || "the seal matched, so no model was needed")),
    el("div", { class: "box " + cls }, el("div", { class: "k" }, "Gate decision (code)"),
      el("div", { class: "v" }, label), el("div", { class: "note" }, note))));
  nodes.push(el("p", { class: "note" }, r.decision.reason));
  if (r.case && r.case.expected) nodes.push(el("p", { class: "note" }, "Fixture " + r.case.id + ", expected verdict: " + r.case.expected + "."));
  if (t) {
    nodes.push(el("h3", null, "Why"), el("p", null, t.summary));
    nodes.push(el("ul", { class: "evidence" }, t.evidence.map((e) => el("li", null,
      el("span", { class: "badge " + (e.points_to === "tampering" ? "bad" : e.points_to === "legitimate" ? "good" : "neutral") }, e.points_to),
      " ", el("code", null, e.change), " — ", e.observation))));
  }
  if (!seal.match) {
    const sig = r.risk_signals || [];
    nodes.push(el("h3", null, "Risk signals for the official"),
      el("p", { class: "note" }, "Deterministic and informational only: the gate does not use them and Nemotron does not see them."),
      sig.length ? el("ul", { class: "evidence" }, sig.map((x) => el("li", null,
        el("span", { class: "badge mid" }, x.id), " ", el("strong", null, x.title), " — ", x.detail)))
        : el("p", null, "None of the five signals fired (service account, off-hours, ticket scope, one-sided removal, registered-voter drop)."));
  }
  if (seal.diff && seal.diff.length) {
    nodes.push(el("h3", null, "What changed (deterministic diff)"), el("div", { class: "table-wrap" }, el("table", null,
      el("thead", null, el("tr", null, el("th", null, "Field"), el("th", null, "Certified"), el("th", null, "Live"))),
      el("tbody", null, seal.diff.map((d) => el("tr", null, el("td", null, el("code", null, d.path)),
        el("td", null, String(d.certified)), el("td", null, String(d.live))))))));
  }
  if (seal.findings && seal.findings.length) {
    nodes.push(el("h3", null, "Checker rules on the live copy"), el("ul", { class: "evidence" },
      seal.findings.map((f) => el("li", null, el("span", { class: "badge " + (f.severity === "BLOCK" ? "bad" : "mid") }, f.severity), " ", f.rule_id + " " + f.title))));
  }
  if (r.incident_report_md) nodes.push(el("h3", null, "Incident report (Nemotron Ultra)"), markdown(r.incident_report_md));
  if (r.case && r.case.metadata) {
    nodes.push(el("details", null, el("summary", null, "Change metadata given to Nemotron"),
      el("pre", { class: "page" }, JSON.stringify(r.case.metadata, null, 2))));
  }
  return nodes;
}

function renderTranslations(r) {
  const nodes = [];
  const notes = Object.fromEntries((r.explanations || []).map((e) => [e.id, e]));
  nodes.push(el("div", { class: "verdict" },
    el("div", { class: "box " + (r.mismatches.length ? "bad" : "good") }, el("div", { class: "k" }, "Deterministic mismatches"),
      el("div", { class: "v" }, String(r.mismatches.length)), el("div", { class: "note" }, "4 languages vs the certified calendar"))));
  if (r.summary) nodes.push(el("p", null, r.summary));
  if (r.mismatches.length) {
    nodes.push(el("div", { class: "table-wrap" }, el("table", null,
      el("thead", null, el("tr", null, ["Page", "Field", "Certified", "Page says", "Severity", "Voter impact", "Draft fix (for a human translator)"].map((h) => el("th", null, h)))),
      el("tbody", null, r.mismatches.map((m) => {
        const e = notes[m.id] || {};
        return el("tr", null, el("td", null, m.lang), el("td", null, el("code", { class: "nowrap" }, m.field)),
          el("td", null, JSON.stringify(m.certified)), el("td", null, el("q", null, m.quote || String(m.page)),
            m.lookalike_of ? el("div", { class: "note" }, "lookalike of " + m.lookalike_of) : null),
          el("td", null, e.severity ? el("span", { class: "badge " + (e.severity === "high" ? "bad" : "mid") }, e.severity) : m.kind),
          el("td", null, e.voter_impact || ""), el("td", null, e.suggested_fix || ""));
      })))));
  }
  if (r.pages) {
    nodes.push(el("details", null, el("summary", null, "Page text the models read"),
      ...Object.entries(r.pages).map(([lang, text]) => el("div", null, el("h3", null, lang), el("pre", { class: "page" }, text)))));
  }
  return nodes;
}

function renderHunt(r) {
  const nodes = [];
  const cats = {};
  r.items.forEach((it) => { cats[it.category] = (cats[it.category] || 0) + 1; });
  nodes.push(el("div", { class: "verdict" },
    el("div", { class: "box " + (r.findings.length ? "bad" : "good") }, el("div", { class: "k" }, "Findings"),
      el("div", { class: "v" }, String(r.findings.length)), el("div", { class: "note" }, "pages with false claims or lookalike domains")),
    el("div", { class: "box neutral" }, el("div", { class: "k" }, "Results reviewed"),
      el("div", { class: "v" }, String(r.items.length)),
      el("div", { class: "note" }, Object.entries(cats).map(([k, v]) => v + " " + k).join(", ")))));
  if (r.report_md) nodes.push(el("h3", null, "Report (Nemotron Super)"), markdown(r.report_md));
  nodes.push(el("h3", null, "Every result"), el("div", { class: "table-wrap" }, el("table", null,
    el("thead", null, el("tr", null, ["Category", "URL", "Source", "Evidence"].map((h) => el("th", null, h)))),
    el("tbody", null, r.items.map((it) => el("tr", null,
      el("td", null, el("span", { class: "badge " + (["misinformation", "impersonation"].includes(it.category) ? "bad" : it.category === "official" ? "good" : "neutral") }, it.category),
        it.demo_decoy ? el("div", { class: "note" }, "demo decoy") : null),
      el("td", null, el("span", { class: "mono" }, it.url)),
      el("td", { class: "note" }, it.source),
      el("td", null, (it.contradictions || []).map((m) => el("div", null, el("code", null, m.field), ": " + m.page + " (certified " + m.certified + ")")),
        (it.lookalikes || []).map((h) => el("div", null, "lookalike domain ", el("code", null, h))),
        !(it.contradictions || []).length && !(it.lookalikes || []).length ? el("span", { class: "note" }, it.reason || "") : null)))))));
  if (r.errors && r.errors.length) nodes.push(el("p", { class: "note" }, "Notes: " + r.errors.join("; ")));
  return nodes;
}

function renderCalls(r) {
  const calls = r.calls || [];
  if (!calls.length) return [el("p", { class: "note" }, "No model or Tavily calls were needed.")];
  const t = r.totals || {};
  return [el("h3", null, "Every call: model, tokens, latency, cost"), el("div", { class: "table-wrap" }, el("table", null,
    el("thead", null, el("tr", null, ["Task", "Model / API", "Tokens in", "Tokens out (reasoning)", "Latency", "Est. cost"].map((h) => el("th", null, h)))),
    el("tbody", null, calls.map((c) => el("tr", null, el("td", null, c.task + (c.ok ? "" : " ✗")),
      el("td", null, el("span", { class: "mono" }, short(c.model))),
      el("td", { class: "num" }, c.role === "tavily" ? "–" : c.prompt_tokens),
      el("td", { class: "num" }, c.role === "tavily" ? c.completion_tokens + " results" : c.completion_tokens + " (" + c.reasoning_tokens + ")"),
      el("td", { class: "num" }, secs(c.latency_ms)),
      el("td", { class: "num" }, c.role === "tavily" ? "credits" : usd(c.cost_usd))))),
    el("tfoot", null, el("tr", null, el("th", null, "Total"), el("th", null, t.model_calls + " model calls, " + t.tavily_calls + " Tavily calls"),
      el("td", { class: "num" }, t.prompt_tokens), el("td", { class: "num" }, t.completion_tokens), el("td"), el("td", { class: "num" }, usd(t.cost_usd))))))];
}

loadScenarios().catch((err) => {
  $("#scenarios").replaceChildren(el("p", { class: "error" }, "Could not load scenarios: " + err.message));
});
