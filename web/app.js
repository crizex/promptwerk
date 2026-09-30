"use strict";
// Promptwerk UI. No framework: state comes from /api/state, a server-sent event says when
// to fetch it again. Everything user-provided is set as textContent, never as HTML.

const $ = (id) => document.getElementById(id);
let S = null;          // last state
let open = null;       // what the detail dialog shows: {kind, name|rid}

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === "class") el.className = v;
    else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) el.append(k instanceof Node ? k : String(k));
  return el;
}

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", "X-Promptwerk": "1" },
  };
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

async function act(action, body, button) {
  if (button) button.disabled = true;
  try {
    await api("/api/" + action, body);
    await refresh();
    return true;
  } catch (e) {
    alert(e.message);
    return false;
  } finally {
    if (button) button.disabled = false;
  }
}

const usd = (n) => "$" + Number(n || 0).toFixed(2);
const FINISHED = new Set(["done", "incomplete", "discarded"]);
const NEEDS = new Set(["waiting_for_answer", "check_failed", "failed", "budget_exhausted"]);
const RESUMABLE = new Set(["failed", "check_failed", "ratelimit", "budget_exhausted", "incomplete"]);
const LIVE = new Set(["running", "checking", "waiting_for_answer"]);

function badge(status) {
  return h("span", { class: "badge s-" + status }, status.replaceAll("_", " "));
}

// ------------------------------------------------------------------ board

function laneOf(plan) {
  if (plan.closed) return "done";
  if (plan.runs.some((r) => NEEDS.has(r.status))) return "needs";
  if (plan.runs.every((r) => FINISHED.has(r.status))) return "done";
  return "running";
}

function draftCard(d) {
  const blocking = d.clarifications.filter((c) => c.blocking).length;
  return h("button", { class: "card draft", type: "button", on: { click: () => show({ kind: "draft", name: d.name }) } },
    h("span", { class: "tag" }, "Draft, waiting for approval"),
    h("strong", {}, d.topic || d.name),
    h("span", { class: "meta" }, `${d.runs.length} run(s), max ${usd(d.runs.reduce((a, r) => a + Number(r.budget_usd || 0), 0))}`
      + (blocking ? `, ${blocking} blocking question(s)` : "") + (d.findings.length ? `, ${d.findings.length} finding(s)` : "")));
}

function planCard(p) {
  const cost = p.runs.reduce((a, r) => a + Number(r.cost_usd || 0), 0);
  const goal = p.summary && p.summary.goal_met ? p.summary.goal_met.state : null;
  const need = p.closed ? null : p.runs.find((r) => NEEDS.has(r.status));
  const why = need && { waiting_for_answer: "A run has a question", check_failed: "Check is red",
    failed: "A run failed", budget_exhausted: "Budget used up" }[need.status];
  return h("button", { class: "card" + (p.closed ? " closed" : "") + (why ? " draft" : ""), type: "button", on: { click: () => show({ kind: "plan", name: p.name }) } },
    why ? h("span", { class: "tag" }, why) : null,
    h("strong", {}, p.topic || p.name),
    h("span", { class: "runs" }, p.runs.map((r) => h("span", { class: "dot s-" + r.status, title: `${r.id}: ${r.status}` }))),
    h("span", { class: "meta" }, `${p.runs.filter((r) => FINISHED.has(r.status)).length}/${p.runs.length} runs, ${usd(cost)}`
      + (goal ? `, goal met: ${goal}` : "") + (p.closed ? ", closed" : "")));
}

function genCard(g) {
  return h("div", { class: "card gen" + (g.running ? "" : " failed") },
    h("span", { class: "tag" }, g.running ? "Planning" : "Planning failed"),
    h("strong", {}, g.topic),
    h("span", { class: "meta" }, g.running ? (g.stage || "starting") : (g.log.trim().split("\n").slice(-2).join(" ") || "no output")),
    g.running ? null : h("div", { class: "row" },
      h("button", { class: "button", type: "button", on: { click: (e) => act("generation-retry", { id: g.id }, e.target) } }, "Retry"),
      h("button", { class: "button ghost", type: "button", on: { click: (e) => act("generation-dismiss", { id: g.id }, e.target) } }, "Dismiss")));
}

function render() {
  const s = S;
  const w = s.worker || {};
  const age = w.time ? Math.round(Date.now() / 1000 - w.time) : null;
  $("worker").textContent = w.text ? `Worker: ${w.text}` + (age > 120 ? ` (${Math.round(age / 60)} min ago)` : "") : "Worker: no signal yet. Is promptwerk-worker running?";
  $("counters").replaceChildren(
    h("span", {}, h("b", {}, usd(s.today_usd)), ` of ${usd(s.daily_cap_usd)} today`));

  const sel = $("project");
  if (sel.dataset.filled !== "1") {
    sel.replaceChildren(...(s.projects.length ? s.projects.map((p) => h("option", { value: p, title: p }, p.split("/").filter(Boolean).pop()))
      : [h("option", { value: "" }, "No projects configured: add them to config.toml")]));
    sel.dataset.filled = "1";
    try {  // last choice survives a reload; storage may be blocked, then the first project wins
      const last = localStorage.getItem("promptwerk.project");
      if (s.projects.includes(last)) sel.value = last;
    } catch {}
    sel.addEventListener("change", () => { try { localStorage.setItem("promptwerk.project", sel.value); } catch {} });
  }

  $("generations").replaceChildren(...s.generations.map(genCard));
  const lanes = { needs: s.drafts.map(draftCard), running: [], done: [] };
  const plans = [...s.plans].reverse();
  for (const p of plans) lanes[laneOf(p)].push(planCard(p));
  for (const [k, list] of Object.entries(lanes)) {
    $(k).replaceChildren(...(list.length ? list : [h("p", { class: "empty" }, k === "needs" ? "Nothing waits for you." : k === "running" ? "Nothing is running." : "No finished plans yet.")]));
  }
  $("nNeeds").textContent = lanes.needs.length;
  $("nRunning").textContent = lanes.running.length;
  $("nDone").textContent = lanes.done.length;
  if (open && $("detail").open && open.kind !== "run") renderDetail();
}

async function refresh() {
  try {
    S = await api("/api/state");
    render();
  } catch (e) {
    $("worker").textContent = "Connection lost: " + e.message;
  }
}

// ------------------------------------------------------------------ detail

function show(what) {
  open = what;
  renderDetail();
  if (!$("detail").open) $("detail").showModal();
}

function list(items, fn) {
  return items && items.length ? h("ul", {}, items.map((x) => h("li", {}, fn ? fn(x) : x))) : null;
}

function runBlock(r) {
  return h("article", { class: "run" },
    h("header", {}, h("strong", {}, r.title), " ", h("code", {}, r.id),
      h("span", { class: "meta" }, ` ${r.mode}, ${r.effort}, max ${usd(r.budget_usd)}`
        + (r.needs && r.needs.length ? `, after ${r.needs.join(", ")}` : ""))),
    r.personas && r.personas.length ? h("p", { class: "meta" }, "Personas: " + r.personas.map((p) => p.role).join(", ")) : null,
    list(r.acceptance),
    r.check ? h("p", { class: "meta" }, "Check: ", h("code", {}, r.check)) : null);
}

function renderDraft(d) {
  const answers = {};
  const cons = h("textarea", { rows: 3, placeholder: "One per line, e.g. Do not touch the database schema" });
  const fatal = d.findings.filter((f) => ["chain", "structure", "path"].includes(f.kind));
  const approve = h("button", { class: "button primary", type: "button", disabled: fatal.length > 0, on: { click: async (e) => {
    const body = { name: d.name, constraints: cons.value, answers: Object.fromEntries(Object.entries(answers).map(([k, el]) => [k, el.value])) };
    if (await act("approve", body, e.target)) $("detail").close();
  } } }, "Approve and queue");
  return [
    h("p", { class: "lead" }, d.rationale || ""),
    d.clarifications.length ? h("section", {}, h("h3", {}, "Questions"), d.clarifications.map((c, i) => h("label", { class: "field" },
      h("span", {}, (c.blocking ? "Blocking: " : "") + c.question),
      h("small", { class: "meta" }, c.why || ""),
      answers[i] = h("input", { type: "text", placeholder: c.blocking ? "Answer required" : "Default: " + (c.assumption || ""), list: "opt" + i }),
      h("datalist", { id: "opt" + i }, (c.options || []).map((o) => h("option", { value: o })))))) : null,
    d.findings.length ? h("section", {}, h("h3", {}, "Open findings"), list(d.findings, (f) => `[${f.kind}] ${f.run}: ${f.problem}`)) : null,
    h("section", {}, h("h3", {}, "Runs"), d.runs.map(runBlock)),
    h("label", { class: "field" }, h("span", {}, "Extra constraints for every run"), cons),
    h("div", { class: "row" }, approve,
      h("button", { class: "button ghost", type: "button", on: { click: async (e) => {
        if (confirm("Discard this draft?") && await act("discard", { name: d.name }, e.target)) $("detail").close();
      } } }, "Discard")),
  ];
}

function summaryBlock(s) {
  if (!s) return null;
  const g = s.goal_met || {};
  return h("section", { class: "summary" },
    h("h3", {}, "Summary"),
    s.error ? h("p", { class: "error" }, "Narrated part failed: " + s.error) : null,
    g.state ? h("p", {}, badge("goal_" + g.state), " ", g.reason || "") : null,
    s.verdict ? h("p", { class: "lead" }, s.verdict) : null,
    s.touch ? h("p", {}, h("b", {}, "See it: "), s.touch) : null,
    s.next_step ? h("p", {}, h("b", {}, "Next step: "), s.next_step) : null,
    s.acceptance && s.acceptance.length ? list(s.acceptance, (a) => [badge(a.met), " ", a.criterion, h("small", { class: "meta" }, " " + a.evidence)]) : null,
    s.missing && s.missing.length ? h("div", {}, h("h3", {}, "Missing"), list(s.missing, (m) => `${m.what} (${m.who.replaceAll("_", " ")})`)) : null,
    s.follow_up ? h("button", { class: "button", type: "button", on: { click: () => {
      $("topic").value = s.follow_up; $("detail").close(); $("topic").focus();
    } } }, "Use the follow-up as next task") : null,
    h("p", { class: "meta" }, `${s.finished}/${s.runs} runs, ${usd(s.cost_usd)} of ${usd(s.budget_usd)}`));
}

function planRun(p, r) {
  const answer = h("textarea", { rows: 2, placeholder: "Your answer" });
  const budget = h("input", { type: "number", min: "0.5", max: "500", step: "0.5", value: r.budget_usd, "aria-label": "Budget in USD" });
  const q = r.question || {};
  return h("article", { class: "run" },
    h("header", {}, badge(r.status), " ", h("strong", {}, r.title), " ", h("code", {}, r.id),
      h("span", { class: "meta" }, ` ${r.mode}, ${usd(r.cost_usd)} of ${usd(r.budget_usd)}`)),
    r.note ? h("p", { class: "meta" }, r.note) : null,
    LIVE.has(r.status) && r.last_tool ? h("p", { class: "meta" }, `Now: ${r.last_tool} ${r.last_target || ""}`) : null,
    r.status === "waiting_for_answer" ? h("div", { class: "question" },
      h("p", {}, q.question || JSON.stringify(q)), list(q.options), answer,
      h("button", { class: "button primary", type: "button", on: { click: (e) => act("answer", { run_id: r.run_id, text: answer.value }, e.target) } }, "Send answer")) : null,
    h("div", { class: "row" },
      r.run_id ? h("button", { class: "button ghost", type: "button", on: { click: () => showRun(r.run_id) } }, "Open run") : null,
      r.run_id && RESUMABLE.has(r.status) ? h("button", { class: "button", type: "button", on: { click: (e) => act("resume", { run_id: r.run_id }, e.target) } }, "Resume") : null,
      r.run_id && LIVE.has(r.status) ? h("button", { class: "button danger", type: "button", on: { click: (e) => confirm("Cancel this run?") && act("cancel", { run_id: r.run_id }, e.target) } }, "Cancel") : null,
      r.run_id && RESUMABLE.has(r.status) ? h("span", { class: "inline" }, budget,
        h("button", { class: "button ghost", type: "button", on: { click: (e) => act("budget", { run_id: r.run_id, usd: budget.value }, e.target) } }, "Set budget")) : null));
}

function renderPlan(p) {
  const done = p.runs.every((r) => FINISHED.has(r.status));
  return [
    p.goal ? h("blockquote", {}, p.goal) : null,
    summaryBlock(p.summary),
    h("section", {}, h("h3", {}, "Runs"), p.runs.map((r) => planRun(p, r))),
    p.finish && p.finish.length ? h("section", {}, h("h3", {}, "Finish line"), list(p.finish, (s) => `${s.ok ? "ok" : "failed"}: ${s.command} ${s.output ? "(" + s.output.slice(0, 160) + ")" : ""}`)) : null,
    h("div", { class: "row" },
      done ? h("button", { class: "button", type: "button", disabled: p.summarizing, on: { click: (e) => act("summary", { plan: p.name }, e.target) } }, p.summarizing ? "Writing summary" : p.summary ? "Rewrite summary" : "Write summary") : null,
      h("button", { class: "button ghost", type: "button", on: { click: (e) => act(p.closed ? "reopen" : "close", { plan: p.name }, e.target) } }, p.closed ? "Reopen plan" : "Close plan")),
  ];
}

function renderDetail() {
  if (!S || !open) return;
  const item = open.kind === "draft" ? S.drafts.find((d) => d.name === open.name) : S.plans.find((p) => p.name === open.name);
  if (!item) { $("detail").close(); return; }
  // keep typed input: only re-render plans, whose inputs are rarely mid-edit
  if (open.kind === "draft" && $("detailBody").dataset.for === open.name) return;
  $("detailTitle").textContent = item.topic || item.name;
  $("detailBody").dataset.for = open.name;
  $("detailBody").replaceChildren(...(open.kind === "draft" ? renderDraft(item) : renderPlan(item)).filter(Boolean));
}

async function showRun(rid) {
  const d = await api("/api/run/" + encodeURIComponent(rid));
  const m = d.meta;
  open = { kind: "run", rid };
  $("detailTitle").textContent = m.title;
  $("detailBody").dataset.for = "";
  $("detailBody").replaceChildren(
    h("p", {}, badge(m.status), ` ${m.mode}, ${usd(m.cost_usd)}, ${m.turns || 0} turns`),
    m.note ? h("p", { class: "meta" }, m.note) : null,
    m.check ? h("pre", {}, `$ ${m.check.command}\nexit ${m.check.code}\n${m.check.output || ""}`) : null,
    d.artifacts.length ? h("section", {}, h("h3", {}, "Artifacts"), list(d.artifacts, (a) => {
      const href = `/api/artifact/${encodeURIComponent(rid)}/${encodeURIComponent(a)}`;
      const link = h("a", { href, target: "_blank", rel: "noopener" }, a);
      return /\.(png|jpe?g|gif|webp)$/i.test(a) ? [link, h("img", { class: "shot", src: href, alt: a, loading: "lazy" })] : link;
    })) : null,
    h("section", {}, h("h3", {}, "Log"), h("pre", { class: "log" }, d.log.join("\n") || "No output yet.")),
    h("details", {}, h("summary", {}, "Prompt"), h("pre", {}, d.prompt)),
    h("button", { class: "button ghost", type: "button", on: { click: () => show({ kind: "plan", name: m.plan }) } }, "Back to plan"));
  if (!$("detail").open) $("detail").showModal();
}

// ------------------------------------------------------------------ compose

function readFile(f) {
  return new Promise((ok, fail) => {
    const r = new FileReader();
    r.onload = () => ok({ name: f.name, data: String(r.result).split(",", 2)[1] || "" });
    r.onerror = fail;
    r.readAsDataURL(f);
  });
}

async function generate(e) {
  e.preventDefault();
  $("composeError").textContent = "";
  const btn = $("generate");
  btn.disabled = true;
  try {
    const attachments = await Promise.all([...$("files").files].map(readFile));
    await api("/api/generate", { topic: $("topic").value, project: $("project").value, attachments });
    $("topic").value = "";
    $("files").value = "";
    $("filelist").textContent = "";
    await refresh();
  } catch (err) {
    $("composeError").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("compose").addEventListener("submit", generate);
  $("topic").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("compose").requestSubmit();
  });
  $("files").addEventListener("change", () => {
    $("filelist").textContent = [...$("files").files].map((f) => f.name).join(", ");
  });
  $("closeDetail").addEventListener("click", () => $("detail").close());
  $("detail").addEventListener("close", () => { open = null; $("detailBody").dataset.for = ""; });
  refresh();
  const ev = new EventSource("/api/events");
  ev.onmessage = () => refresh();
});
