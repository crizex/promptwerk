"use strict";
// Promptwerk UI. No framework: state comes from /api/state, a server-sent event says when
// to fetch it again. Everything user-provided is set as textContent, never as HTML. The CSP
// forbids style attributes: sizes are set through the CSSOM, drawings are SVG elements.

const $ = (id) => document.getElementById(id);
let S = null;          // last state
let open = null;       // what the detail drawer shows: {kind, name|rid}
let UI = null;         // UI version this tab was loaded with
let picked = [];       // files chosen, pasted or dropped for the next plan
let origin = null;     // plan the next task follows up: {name, topic}
let allDone = false;   // Done lane shows every visible plan instead of the newest few

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

function svg(tag, attrs, ...kids) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
  for (const k of kids.flat()) if (k != null) el.append(k instanceof Node ? k : String(k));
  return el;
}

function toast(text, bad) {
  const t = h("p", { class: "toast" + (bad ? " bad" : "") }, text);
  $("toasts").append(t);
  setTimeout(() => t.remove(), bad ? 8000 : 4000);
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
    const out = await api("/api/" + action, body);
    await refresh();
    return out;
  } catch (e) {
    toast(e.message, true);
    return false;
  } finally {
    if (button) button.disabled = false;
  }
}

const usd = (n) => "$" + Number(n || 0).toFixed(2);
const base = (p) => String(p || "").split("/").filter(Boolean).pop() || "";
const FINISHED = new Set(["done", "incomplete", "discarded"]);
const NEEDS = new Set(["waiting_for_answer", "check_failed", "failed", "budget_exhausted"]);
const RESUMABLE = new Set(["failed", "check_failed", "ratelimit", "budget_exhausted", "incomplete"]);
const LIVE = new Set(["running", "checking", "waiting_for_answer"]);
const STALL_H = 6;
const DONE_SHOWN = 8;

function badge(status) {
  return h("span", { class: "badge s-" + status }, status.replaceAll("_", " "));
}

// Hours a working run has been going, once that is suspiciously long. 0 otherwise.
function stalled(r) {
  if (!["running", "checking"].includes(r.status) || !r.started) return 0;
  const hours = (Date.now() - new Date(r.started).getTime()) / 3.6e6;
  return hours >= STALL_H ? Math.floor(hours) : 0;
}

function planOf(name) {
  return S.plans.find((p) => p.name === name);
}

function follows(name) {
  const p = planOf(name);
  return h("span", { class: "meta follows" }, "↳ follows up ", p ? p.topic || p.name : name);
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
    d.from_plan ? follows(d.from_plan) : null,
    h("span", { class: "meta" }, `${d.runs.length} run(s), max ${usd(d.runs.reduce((a, r) => a + Number(r.budget_usd || 0), 0))}`
      + (blocking ? `, ${blocking} blocking question(s)` : "") + (d.findings.length ? `, ${d.findings.length} finding(s)` : "")));
}

function planCard(p) {
  const cost = p.runs.reduce((a, r) => a + Number(r.cost_usd || 0), 0);
  const need = p.closed ? null : p.runs.find((r) => NEEDS.has(r.status));
  const slow = p.closed ? 0 : Math.max(0, ...p.runs.map(stalled));
  const why = need ? { waiting_for_answer: "A run has a question", check_failed: "Check is red",
    failed: "A run failed", budget_exhausted: "Budget used up" }[need.status]
    : slow ? `No progress for ${slow} h` : null;
  return h("button", { class: "card" + (p.closed ? " closed" : "") + (why ? " draft" : ""), type: "button", on: { click: () => show({ kind: "plan", name: p.name }) } },
    why ? h("span", { class: "tag" }, why) : null,
    h("strong", {}, p.topic || p.name),
    p.from_plan ? follows(p.from_plan) : null,
    h("span", { class: "runs" }, p.runs.map((r) => h("span", { class: "dot s-" + r.status, title: `${r.id}: ${r.status}` }))),
    h("span", { class: "meta" }, `${p.runs.filter((r) => FINISHED.has(r.status)).length}/${p.runs.length} runs, ${usd(cost)}`
      + (p.verdict ? `, ${p.verdict.word.toLowerCase()}` : "") + (p.closed ? ", closed" : "")));
}

// The planner's stages as stations, so a waiting minute shows where it is.
const STATIONS = [["A", "Draft"], ["B", "Check"], ["C", "Critic"], ["D", "Approval (you)"]];

function genCard(g) {
  const prompt = g.mode === "prompt";
  const m = /^Stage ([A-D])/.exec(g.stage || "");
  const at = m ? "ABCD".indexOf(m[1]) : -1;
  const fill = h("span", {});
  fill.style.setProperty("width", (prompt ? 50 : (at + 1) * 25) + "%");
  const label = g.running ? (prompt ? "Writing prompt" : "Planning") : (prompt ? "Prompt failed" : "Planning failed");
  return h("div", { class: "card gen" + (g.running ? "" : " failed"), role: "group", "aria-label": `${label}: ${g.topic}` },
    h("span", { class: "tag" }, label),
    h("strong", {}, g.topic),
    g.from_plan ? follows(g.from_plan) : null,
    prompt ? null : h("ol", { class: "stations" }, STATIONS.map(([k, name], i) =>
      h("li", { class: i < at ? "done" : i === at ? (g.running ? "now" : "stop") : "", "aria-current": i === at && g.running ? "step" : null },
        h("b", {}, k), " ", name))),
    g.running ? h("span", { class: "progress", "aria-hidden": "true" }, fill) : null,
    h("span", { class: "meta" }, g.running ? (g.stage || "starting") : (g.log.trim().split("\n").slice(-2).join(" ") || "no output")),
    g.running ? null : h("div", { class: "row" },
      h("button", { class: "button", type: "button", on: { click: (e) => act("generation-retry", { id: g.id }, e.target) } }, "Try again"),
      h("button", { class: "button ghost", type: "button", on: { click: (e) => act("generation-dismiss", { id: g.id }, e.target) } }, "Remove")));
}

function promptCard(p) {
  const text = h("pre", { class: "prompt-text" }, p.prompt);
  const box = h("details", {}, h("summary", {}, "Show prompt"), text);
  return h("article", { class: "card prompt", id: "prompt-" + p.id },
    h("span", { class: "tag" }, "Prompt"),
    h("strong", {}, p.topic),
    box,
    h("span", { class: "meta" }, `${new Date((p.time || 0) * 1000).toLocaleString()}, ${usd(p.cost_usd)}`),
    h("div", { class: "row" },
      h("button", { class: "button primary small", type: "button", on: { click: () => copy(p.prompt, text) } }, "Copy"),
      h("button", { class: "button ghost small", type: "button", on: { click: (e) => {
        if (confirm("Delete this prompt?")) act("prompt-delete", { id: p.id }, e.target);
      } } }, "Delete")));
}

async function copy(text, pre) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Prompt copied.");
  } catch {  // no clipboard over plain http: select the text so Ctrl+C does it
    if (!pre) return toast("The browser blocked copying.", true);
    pre.closest("details").open = true;
    const range = document.createRange();
    range.selectNodeContents(pre);
    getSelection().removeAllRanges();
    getSelection().addRange(range);
    toast("The browser blocked copying. The text is selected: press Ctrl+C.");
  }
}

function render() {
  const s = S;
  const w = s.worker || {};
  const age = w.time ? Math.round(Date.now() / 1000 - w.time) : null;
  $("worker").textContent = w.text ? `Worker: ${w.text}` + (age > 120 ? ` (${Math.round(age / 60)} min ago)` : "") : "Worker: no signal yet. Is promptwerk-worker running?";
  const wk = s.week || {};
  $("counters").replaceChildren(
    h("span", {}, h("b", {}, usd(s.today_usd)), ` of ${usd(s.daily_cap_usd)} today`),
    h("span", { title: "Plans approved in the last 7 days, goal met of those with a summary, cost of all runs" },
      "7 days: ", h("b", {}, wk.plans || 0), wk.plans === 1 ? " plan, goal met " : " plans, goal met ", h("b", {}, `${wk.goal_met || 0}/${wk.judged || 0}`), ", ", h("b", {}, usd(wk.cost_usd))));
  UI = UI || s.ui;
  $("reload").hidden = !s.ui || s.ui === UI;

  const sel = $("project");
  if (sel.dataset.filled !== "1") {
    sel.replaceChildren(s.projects.length ? h("option", { value: "" }, "Infer from the text")
      : h("option", { value: "" }, "No projects configured: add them to config.toml"),
      ...s.projects.map((p) => h("option", { value: p, title: p }, base(p))));
    sel.dataset.filled = "1";
    try {  // last choice survives a reload; storage may be blocked, then "infer" wins
      const last = localStorage.getItem("promptwerk.project");
      if (last === "" || s.projects.includes(last)) sel.value = last;
    } catch {}
    sel.addEventListener("change", () => { try { localStorage.setItem("promptwerk.project", sel.value); } catch {} });
    sel.addEventListener("change", () => { deployHint(); reading(); });
  }
  deployHint();
  reading();

  $("generations").replaceChildren(...s.generations.map(genCard));
  const lanes = { needs: s.drafts.map(draftCard), running: [], done: [] };
  for (const p of [...s.plans].reverse()) lanes[laneOf(p)].push(planCard(p));
  const doneCount = lanes.done.length;
  if (!allDone && doneCount > DONE_SHOWN) {
    lanes.done = lanes.done.slice(0, DONE_SHOWN);
    lanes.done.push(h("button", { class: "button ghost", type: "button", on: { click: () => { allDone = true; render(); } } }, `Show all ${doneCount}`));
  }
  const older = (s.plans_total || 0) - s.plans.length;
  if (older > 0) lanes.done.push(h("p", { class: "meta" }, `${older} older plan(s) not shown.`));
  for (const [k, list] of Object.entries(lanes)) {
    $(k).replaceChildren(...(list.length ? list : [h("p", { class: "empty" }, k === "needs" ? "Nothing waits for you." : k === "running" ? "Nothing is running." : "No finished plans yet.")]));
  }
  $("nNeeds").textContent = lanes.needs.length;
  $("nRunning").textContent = lanes.running.length;
  $("nDone").textContent = doneCount;
  const prompts = s.prompts || [];
  $("promptsBox").hidden = !prompts.length;
  $("nPrompts").textContent = prompts.length;
  // an open prompt stays open across refreshes
  const shown = new Set([...$("prompts").querySelectorAll("details[open]")].map((d) => d.closest("article").id));
  $("prompts").replaceChildren(...prompts.map((p) => {
    const card = promptCard(p);
    if (shown.has(card.id)) card.querySelector("details").open = true;
    return card;
  }));
  if (open && $("detail").open && open.kind !== "run") renderDetail();
  if ($("palette").open) drawPalette();
}

function deployHint() {
  const miss = S && S.without_deploy.includes($("project").value);
  $("deployHint").textContent = miss ? "No deploy script for this project: runs finish without a deploy." : "";
}

async function refresh() {
  try {
    S = await api("/api/state");
    render();
  } catch (e) {
    $("worker").textContent = "Connection lost: " + e.message;
  }
}

// ------------------------------------------------------------------ how I read this

const READ = /\b(review|audit|analy[sz]e|inspect|investigate|explain|compare|look into|find out)\b/gi;
const BUILD = /\b(add|build|fix|implement|create|change|refactor|remove|rename|update|migrate)\b/gi;
const DELIVER = /\b(reports?|tables?|lists?|summary|pdf|csv|markdown|deploy|commit|push|screenshots?)\b/gi;
const FILE = /[\w./-]*[\w-]{2,}\.(?:md|json|csv|tsv|pdf|txt|html?|css|jsx?|tsx?|py|sh|ya?ml|toml|png|jpe?g|svg|sql)\b/gi;
const esc = (t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function projectPattern() {
  const names = (S ? S.projects : []).map(base).filter((n) => n.length > 2);
  return names.length ? new RegExp(`\\b(${names.map(esc).join("|")})\\b`, "gi") : null;
}

// The sentence with what the planner will likely pick up highlighted, plus a short forecast.
function reading() {
  const text = $("topic").value;
  const prompt = mode() === "prompt";
  $("readingBox").hidden = text.trim().length < 8;
  if ($("readingBox").hidden) return;
  const hits = [];
  const kinds = [["file", FILE], ["read", READ], ["build", BUILD], ["delivery", DELIVER]];
  const proj = projectPattern();
  if (proj && !prompt) kinds.unshift(["project", proj]);
  for (const [kind, re] of kinds) for (const m of text.matchAll(re)) hits.push({ kind, at: m.index, end: m.index + m[0].length });
  hits.sort((a, b) => a.at - b.at || b.end - a.end);
  const nodes = [];
  let pos = 0;
  for (const x of hits) {
    if (x.at < pos) continue;  // overlaps an earlier, longer hit
    nodes.push(text.slice(pos, x.at), h("mark", { class: "m-" + x.kind, title: x.kind }, text.slice(x.at, x.end)));
    pos = x.end;
  }
  nodes.push(text.slice(pos));
  $("reading").replaceChildren(...nodes);
  const has = (k) => hits.some((x) => x.kind === k);
  const words = (k) => [...new Set(hits.filter((x) => x.kind === k).map((x) => text.slice(x.at, x.end).toLowerCase()))];
  const named = words("project")[0];
  const rows = prompt ? [["Result", "one prompt to copy"], ["Project", "none"], ["Runs", "nothing is executed"], ["Takes", "1 to 3 min"]]
    : [["Project", $("project").value ? base($("project").value) : named ? `${named} (from the text)` : "the planner infers it"],
      ["Work", has("read") && has("build") ? "read first, then change code" : has("build") ? "changes code" : has("read") ? "reads only, no code changes" : "the planner decides"],
      ["Delivery", has("delivery") ? words("delivery").join(", ") : "a run report"],
      ["Planning takes", "5 to 12 min"]];
  $("readingSum").replaceChildren(...rows.map(([k, v]) => h("div", {}, h("dt", {}, k), h("dd", {}, v))));
}

// ------------------------------------------------------------------ drawings

// One bar: planned budget per run as segments, spent money on top, a line where the plan ends.
function budgetTrack(runs) {
  const cost = runs.reduce((a, r) => a + Number(r.cost_usd || 0), 0);
  const plan = runs.reduce((a, r) => a + Number(r.budget_usd || 0), 0);
  const scale = Math.max(cost, plan) || 1;
  const over = cost > plan;
  const bar = svg("svg", { class: "track", viewBox: "0 0 100 10", preserveAspectRatio: "none", role: "img",
    "aria-label": `${usd(cost)} spent of ${usd(plan)} planned` + (over ? ", over the plan" : "") });
  let x = 0;
  for (const r of runs) {
    const w = (Number(r.budget_usd || 0) / scale) * 100;
    bar.append(svg("rect", { class: "seg", x, y: 0, width: Math.max(w - 0.5, 0), height: 10 }, svg("title", {}, `${r.id}: ${usd(r.budget_usd)}`)));
    x += w;
  }
  if (cost) bar.append(svg("rect", { class: "spent" + (over ? " over" : ""), x: 0, y: 3, width: (cost / scale) * 100, height: 4 }));
  if (over) bar.append(svg("line", { class: "plan", x1: (plan / scale) * 100, x2: (plan / scale) * 100, y1: 0, y2: 10 }));
  return h("div", { class: "budget" }, bar,
    h("p", { class: "meta" }, cost ? `${usd(cost)} spent of ${usd(plan)} planned` + (over ? ". The line marks the plan." : "")
      : `${usd(plan)} planned across ${runs.length} run(s)`));
}

// Runs as boxes in columns by dependency depth, edges from each need to its run.
function graph(runs) {
  if (runs.length < 2) return null;
  const W = 190, H = 96, GX = 36, GY = 14, PAD = 16;
  const by = Object.fromEntries(runs.map((r) => [r.id, r]));
  const depth = {};
  const deep = (id, seen) => {
    if (id in depth) return depth[id];
    if (seen.has(id)) return 0;  // a cycle; the plan check reports it
    seen.add(id);
    const needs = (by[id].needs || []).filter((n) => by[n]);
    return (depth[id] = needs.length ? 1 + Math.max(...needs.map((n) => deep(n, seen))) : 0);
  };
  const cols = [], pos = {};
  for (const r of runs) {
    const c = deep(r.id, new Set());
    (cols[c] = cols[c] || []).push(r);
    pos[r.id] = { x: PAD + c * (W + GX), y: PAD + (cols[c].length - 1) * (H + GY) };
  }
  const width = PAD * 2 + cols.length * W + (cols.length - 1) * GX;
  const height = PAD * 2 + Math.max(...cols.map((c) => (c || []).length)) * (H + GY) - GY;
  const pic = svg("svg", { class: "graph", width, height, viewBox: `0 0 ${width} ${height}`, role: "img",
    "aria-label": `Order of ${runs.length} runs: ` + runs.map((r) => r.needs && r.needs.length ? `${r.id} after ${r.needs.join(" and ")}` : `${r.id} first`).join(", ") });
  for (const r of runs) for (const n of r.needs || []) {
    if (!pos[n]) continue;
    const a = { x: pos[n].x + W, y: pos[n].y + H / 2 }, b = { x: pos[r.id].x, y: pos[r.id].y + H / 2 };
    const mid = (a.x + b.x) / 2;
    pic.append(svg("path", { class: "edge", d: `M${a.x} ${a.y} C${mid} ${a.y} ${mid} ${b.y} ${b.x} ${b.y}` }));
  }
  const cut = (t, n) => (t.length <= n ? t : t.slice(0, n).replace(/\s+\S*$/, ""));
  for (const r of runs) {
    const { x, y } = pos[r.id];
    const tags = [r.mode, r.effort, (r.personas || []).map((p) => p.role).join(", ")].filter(Boolean).join(" / ");
    pic.append(svg("g", { class: "node s-" + (r.status || "draft") },
      svg("title", {}, `${r.title} (${r.id})`),
      svg("rect", { x, y, width: W, height: H, rx: 8 }),
      svg("text", { x: x + 12, y: y + 24, class: "t" }, cut(r.title || r.id, 24)),
      svg("text", { x: x + 12, y: y + 46, class: "k" }, r.id),
      svg("text", { x: x + 12, y: y + 64, class: "k" }, cut(tags, 28)),
      svg("text", { x: x + 12, y: y + 82, class: "k" }, r.status ? `${r.status.replaceAll("_", " ")}, ${usd(r.cost_usd)} of ${usd(r.budget_usd)}` : `max ${usd(r.budget_usd)}`)));
  }
  return h("div", { class: "graph-box" }, pic);
}

// ------------------------------------------------------------------ detail

function show(what) {
  open = what;
  $("detailBody").dataset.for = "";
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
    r.check ? h("p", { class: "meta" }, "Check: ", h("code", {}, r.check)) : null,
    r.cwd ? h("p", { class: "meta" }, "Works in: ", h("code", {}, r.cwd)) : null,
    r.prompt ? h("details", {}, h("summary", {}, "Full prompt"), h("pre", {}, r.prompt)) : null);
}

const IMG = /\.(png|jpe?g|gif|webp)$/i;

function gallery(paths) {  // "<generation id>/<name>" as stored by the planner
  return h("section", {}, h("h3", {}, "Your attachments"), h("div", { class: "gallery" }, paths.map((p) => {
    const [gid, name] = p.split("/");
    const href = `/api/attachment/${encodeURIComponent(gid)}/${encodeURIComponent(name)}`;
    return h("a", { href, target: "_blank", rel: "noopener", class: "thumb", title: name },
      IMG.test(name) ? h("img", { src: href, alt: name, loading: "lazy" }) : h("span", { class: "file" }, name.split(".").pop()), h("small", {}, name));
  })));
}

function originLink(name) {
  const p = planOf(name);
  return p ? h("button", { class: "link", type: "button", on: { click: () => show({ kind: "plan", name }) } }, "↳ follows up " + (p.topic || name))
    : follows(name);
}

function renderDraft(d) {
  const answers = {};
  const cons = h("textarea", { rows: 3, placeholder: "One per line, e.g. Do not touch the database schema" });
  const fatal = d.findings.filter((f) => ["chain", "structure", "path"].includes(f.kind));
  const approve = h("button", { class: "button primary", type: "button", disabled: fatal.length > 0, on: { click: async (e) => {
    const body = { name: d.name, constraints: cons.value, answers: Object.fromEntries(Object.entries(answers).map(([k, el]) => [k, el.value])) };
    if (await act("approve", body, e.target)) { $("detail").close(); toast("Approved and queued."); }
  } } }, "Approve and queue");
  return [
    d.from_plan ? originLink(d.from_plan) : null,
    h("p", { class: "lead" }, d.rationale || ""),
    d.clarifications.length ? h("section", {}, h("h3", {}, "Questions"), d.clarifications.map((c, i) => h("div", { class: "field" },
      h("label", { for: "ans" + i }, (c.blocking ? "Blocking: " : "") + c.question),
      c.why ? h("small", { class: "meta" }, c.why) : null,
      answers[i] = h("textarea", { id: "ans" + i, rows: 2, placeholder: c.blocking ? "Answer required" : "Default: " + (c.assumption || "") }),
      c.options && c.options.length ? h("div", { class: "row options" }, c.options.map((o) => h("button", { class: "button ghost small", type: "button",
        on: { click: () => { answers[i].value = o; answers[i].focus(); } } }, o))) : null))) : null,
    d.attachments.length ? gallery(d.attachments) : null,
    d.findings.length ? h("section", {}, h("h3", {}, "Open findings"), list(d.findings, (f) => `[${f.kind}] ${f.run}: ${f.problem}`)) : null,
    budgetTrack(d.runs),
    graph(d.runs),
    h("section", {}, h("h3", {}, "Runs"), d.runs.map(runBlock)),
    h("label", { class: "field" }, h("span", {}, "Extra constraints for every run"), cons),
    h("div", { class: "row" }, approve,
      h("button", { class: "button ghost", type: "button", on: { click: async (e) => {
        if (confirm("Discard this draft?") && await act("discard", { name: d.name }, e.target)) $("detail").close();
      } } }, "Discard")),
  ];
}

function verdictBlock(v) {
  if (!v) return null;
  return h("section", { class: "verdict v-" + v.level },
    h("h3", {}, v.word),
    list(v.reasons),
    v.step ? h("p", {}, h("b", {}, "Next step: "), v.step) : null,
    v.touch ? h("p", {}, h("b", {}, "See it: "), v.touch) : null,
    v.your_move && v.your_move.length ? h("div", {}, h("b", {}, "Only you can do this:"), list(v.your_move)) : null);
}

function summaryBlock(p) {
  const s = p.summary;
  if (!s) return null;
  const g = s.goal_met || {};
  return h("section", { class: "summary" },
    h("h3", {}, "Summary"),
    s.error ? h("p", { class: "error" }, "Narrated part failed: " + s.error) : null,
    g.state ? h("p", {}, badge("goal_" + g.state), " ", g.reason || "") : null,
    s.verdict ? h("p", { class: "lead" }, s.verdict) : null,
    !p.verdict && s.touch ? h("p", {}, h("b", {}, "See it: "), s.touch) : null,
    !p.verdict && s.next_step ? h("p", {}, h("b", {}, "Next step: "), s.next_step) : null,
    s.acceptance && s.acceptance.length ? list(s.acceptance, (a) => [badge(a.met), " ", a.criterion, h("small", { class: "meta" }, " " + a.evidence)]) : null,
    s.missing && s.missing.length ? h("div", {}, h("h3", {}, "Missing"), list(s.missing, (m) => `${m.what} (${m.who.replaceAll("_", " ")})`)) : null,
    s.follow_up ? h("button", { class: "button", type: "button", on: { click: () => followUp(p, s.follow_up) } }, "Use the follow-up as next task") : null,
    h("p", { class: "meta" }, `${s.finished}/${s.runs} runs, ${usd(s.cost_usd)} of ${usd(s.budget_usd)}`));
}

async function draftFindings(r, button) {
  const out = await act("findings-draft", { run_id: r.run_id }, button);
  if (out && out.draft) { show({ kind: "draft", name: out.draft }); toast("Implementation draft ready. Review it before approval."); }
}

function planRun(p, r) {
  const answer = h("textarea", { rows: 2, placeholder: "Your answer" });
  const budget = h("input", { type: "number", min: "0.5", max: "500", step: "0.5", value: r.budget_usd, "aria-label": "Budget in USD" });
  const q = r.question || {};
  const slow = stalled(r);
  const up = r.escalated;
  return h("article", { class: "run" },
    h("header", {}, badge(r.status), " ", h("strong", {}, r.title), " ", h("code", {}, r.id),
      h("span", { class: "meta" }, ` ${r.mode}, ${usd(r.cost_usd)} of ${usd(r.budget_usd)}` + (r.model ? `, ${r.model}` : "")
        + (r.check_code != null ? `, check exit ${r.check_code}` : ""))),
    slow ? h("p", { class: "warn" }, `No progress for ${slow} h. The run may hang: open it, then cancel or keep waiting.`) : null,
    up ? h("p", { class: "meta esc", title: up.reason || "" }, `Escalated from ${up.from} to ${up.to}: ${up.reason || "no reason given"}`) : null,
    r.note ? h("p", { class: "meta" }, r.note) : null,
    r.check_reason ? h("p", { class: "meta", title: r.check_reason }, "Check command corrected by the worker: " + r.check_reason) : null,
    r.denials ? h("p", { class: "meta" }, `${r.denials} tool call(s) denied by the rules`) : null,
    LIVE.has(r.status) && r.last_tool ? h("p", { class: "meta" }, `Now: ${r.last_tool} ${r.last_target || ""}`) : null,
    r.status === "waiting_for_answer" ? h("div", { class: "question" },
      h("p", {}, q.question || JSON.stringify(q)), list(q.options), answer,
      h("button", { class: "button primary", type: "button", on: { click: (e) => act("answer", { run_id: r.run_id, text: answer.value }, e.target) } }, "Send answer")) : null,
    h("div", { class: "row" },
      r.run_id ? h("button", { class: "button ghost", type: "button", on: { click: () => showRun(r.run_id) } }, "Open run") : null,
      r.has_findings ? h("button", { class: "button", type: "button", on: { click: (e) => draftFindings(r, e.target) } }, "Draft implementation") : null,
      r.run_id && RESUMABLE.has(r.status) ? h("button", { class: "button", type: "button", on: { click: (e) => act("resume", { run_id: r.run_id }, e.target) } }, "Resume") : null,
      r.run_id && LIVE.has(r.status) ? h("button", { class: "button danger", type: "button", on: { click: (e) => confirm("Cancel this run?") && act("cancel", { run_id: r.run_id }, e.target) } }, "Cancel") : null,
      r.run_id && RESUMABLE.has(r.status) ? h("span", { class: "inline" }, budget,
        h("button", { class: "button ghost", type: "button", on: { click: (e) => act("budget", { run_id: r.run_id, usd: budget.value }, e.target) } }, "Set budget")) : null));
}

const WHERE = { draft: "draft", queued: "queued", plan: "running or done", generating: "being planned" };

function renderPlan(p) {
  const done = p.runs.every((r) => FINISHED.has(r.status));
  const live = p.runs.some((r) => LIVE.has(r.status));
  const untouched = !p.closed && p.runs.every((r) => !r.run_id);
  return [
    p.from_plan ? originLink(p.from_plan) : null,
    p.goal ? h("blockquote", {}, p.goal) : null,
    verdictBlock(p.verdict),
    summaryBlock(p),
    p.successors && p.successors.length ? h("section", {}, h("h3", {}, "Follow-ups"), list(p.successors, (x) =>
      x.where === "generating" ? `${x.topic} (${WHERE[x.where]})`
        : h("button", { class: "link", type: "button", on: { click: () => show({ kind: x.where === "draft" ? "draft" : "plan", name: x.name }) } }, `${x.topic || x.name} (${WHERE[x.where]})`))) : null,
    budgetTrack(p.runs),
    graph(p.runs),
    h("section", {}, h("h3", {}, "Runs"), p.runs.map((r) => planRun(p, r))),
    p.finish && p.finish.length ? h("section", {}, h("h3", {}, "Finish line"), list(p.finish, (s) => `${s.ok ? "ok" : "failed"}: ${s.command} ${s.output ? "(" + s.output.slice(0, 160) + ")" : ""}`)) : null,
    h("div", { class: "row" },
      done ? h("button", { class: "button", type: "button", disabled: p.summarizing, on: { click: (e) => act("summary", { plan: p.name }, e.target) } }, p.summarizing ? "Writing summary" : p.summary ? "Rewrite summary" : "Write summary") : null,
      done || p.closed ? h("button", { class: "button", type: "button", on: { click: () => followUp(p, "") } }, "Follow up") : null,
      untouched ? h("button", { class: "button danger", type: "button", on: { click: async (e) => {
        if (confirm("Take this plan out of the queue? Nothing has run yet.") && await act("withdraw", { plan: p.name }, e.target)) $("detail").close();
      } } }, "Withdraw plan") : null,
      h("button", { class: "button ghost", type: "button", disabled: live && !p.closed, title: live && !p.closed ? "A run is still working. Cancel it first." : null,
        on: { click: (e) => act(p.closed ? "reopen" : "close", { plan: p.name }, e.target) } }, p.closed ? "Reopen plan" : "Close plan")),
  ];
}

function renderDetail() {
  if (!S || !open) return;
  const item = open.kind === "draft" ? S.drafts.find((d) => d.name === open.name) : planOf(open.name);
  if (!item) { $("detail").close(); return; }
  // keep typed input: only re-render plans, whose inputs are rarely mid-edit
  if (open.kind === "draft" && $("detailBody").dataset.for === open.name) return;
  // plans: never rebuild under the user's fingers (an answer half typed on a phone)
  const body = $("detailBody");
  if (body.dataset.for === open.name && body.contains(document.activeElement)
    && /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) return;
  $("detailTitle").textContent = item.topic || item.name;
  $("detailBody").dataset.for = open.name;
  $("detailBody").replaceChildren(...(open.kind === "draft" ? renderDraft(item) : renderPlan(item)).filter(Boolean));
}

async function showRun(rid) {
  let d;
  try { d = await api("/api/run/" + encodeURIComponent(rid)); } catch (e) { return toast(e.message, true); }
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
      if (IMG.test(a)) return [link, h("img", { class: "shot", src: href, alt: a, loading: "lazy" })];
      return TEXT.test(a) ? [link, " ", h("button", { class: "button ghost small", type: "button", on: { click: () => showArtifact(rid, a, m.title) } }, "View")] : link;
    })) : null,
    h("section", {}, h("h3", {}, "Log"), h("pre", { class: "log" }, d.log.join("\n") || "No output yet.")),
    h("details", {}, h("summary", {}, "Prompt"), h("pre", {}, d.prompt)),
    h("button", { class: "button ghost", type: "button", on: { click: () => show({ kind: "plan", name: m.plan }) } }, "Back to plan"));
  if (!$("detail").open) $("detail").showModal();
}

// ------------------------------------------------------------------ artifacts

const TEXT = /\.(md|markdown|txt|log|json|csv|tsv|ya?ml|diff|patch)$/i;
const RANK = { critical: 0, blocker: 0, high: 1, major: 1, medium: 2, moderate: 2, low: 3, minor: 3, info: 4 };

// Markdown to DOM nodes. No innerHTML: artifacts are written by a model and read here.
function inline(t) {
  return t.split(/(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)\s]+\))/).map((p) => {
    let m;
    if ((m = p.match(/^`([^`]+)`$/))) return h("code", {}, m[1]);
    if ((m = p.match(/^\*\*([^*]+)\*\*$/))) return h("strong", {}, m[1]);
    if ((m = p.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/)))
      return /^https?:\/\//i.test(m[2]) ? h("a", { href: m[2], target: "_blank", rel: "noopener noreferrer" }, m[1]) : `${m[1]} (${m[2]})`;
    return p;
  });
}

function markdown(text) {
  const out = [];
  let box = null, fence = null;
  const flush = () => { box = null; };
  for (const line of text.split("\n")) {
    let m;
    if (fence !== null) {
      if (/^```/.test(line)) { out.push(h("pre", {}, fence.join("\n"))); fence = null; } else fence.push(line);
      continue;
    }
    if (/^```/.test(line)) { flush(); fence = []; continue; }
    if (!line.trim()) { flush(); continue; }
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) { flush(); out.push(h("h" + Math.min(m[1].length + 2, 6), {}, inline(m[2]))); continue; }
    if (/^\s*---+\s*$/.test(line)) { flush(); out.push(h("hr", {})); continue; }
    if (/^\s*\|.*\|\s*$/.test(line)) {
      if (/^[\s|:-]+$/.test(line)) continue;
      const cells = line.trim().slice(1, -1).split("|").map((c) => c.trim());
      if (!box || box.tagName !== "TBODY") {
        const body = h("tbody", {});
        out.push(h("div", { class: "table" }, h("table", {}, h("thead", {}, h("tr", {}, cells.map((c) => h("th", {}, inline(c))))), body)));
        box = body;
      } else box.append(h("tr", {}, cells.map((c) => h("td", {}, inline(c)))));
      continue;
    }
    const tag = (m = line.match(/^\s*[-*]\s+(.*)$/)) ? "UL" : (m = line.match(/^\s*\d+\.\s+(.*)$/)) ? "OL" : null;
    if (tag) {
      if (!box || box.tagName !== tag) { box = h(tag.toLowerCase(), {}); out.push(box); }
      box.append(h("li", {}, inline(m[1])));
      continue;
    }
    if ((m = line.match(/^\s*>\s?(.*)$/))) {
      if (!box || box.tagName !== "BLOCKQUOTE") { box = h("blockquote", {}); out.push(box); }
      box.append(h("p", {}, inline(m[1])));
      continue;
    }
    flush();
    out.push(h("p", {}, inline(line)));
  }
  if (fence !== null) out.push(h("pre", {}, fence.join("\n")));
  return h("div", { class: "prose" }, out);
}

// A JSON list of findings becomes a table, most severe first. Anything else stays JSON.
function jsonView(text) {
  let data;
  try { data = JSON.parse(text); } catch { return h("pre", {}, text); }
  const rows = Array.isArray(data) ? data : Array.isArray(data && data.findings) ? data.findings : null;
  const sev = rows && rows.length && rows.every((r) => r && typeof r === "object" && !Array.isArray(r))
    && ["severity", "level", "priority"].find((k) => rows.some((r) => k in r));
  if (!sev) return h("pre", {}, JSON.stringify(data, null, 2));
  const rank = (r) => RANK[String(r[sev]).toLowerCase()] ?? 9;
  const cols = [sev, ...[...new Set(rows.flatMap(Object.keys))].filter((k) => k !== sev)].slice(0, 6);
  const cell = (v) => v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
  return h("div", { class: "table" }, h("table", {},
    h("thead", {}, h("tr", {}, cols.map((c) => h("th", {}, c)))),
    h("tbody", {}, [...rows].sort((a, b) => rank(a) - rank(b)).map((r) => h("tr", {}, cols.map((c, i) =>
      h("td", {}, i === 0 ? h("span", { class: "badge sev-" + rank(r) }, cell(r[c])) : cell(r[c]))))))));
}

async function showArtifact(rid, name, title) {
  const href = `/api/artifact/${encodeURIComponent(rid)}/${encodeURIComponent(name)}`;
  const r = await fetch(href);
  if (!r.ok) return toast("Could not load " + name, true);
  const text = await r.text();
  const size = new Blob([text]).size;
  open = { kind: "run", rid };
  $("detailTitle").textContent = name;
  $("detailBody").dataset.for = "";
  $("detailBody").replaceChildren(
    h("p", { class: "meta" }, `${title}, ${size < 1024 ? size + " B" : (size / 1024).toFixed(1) + " KB"}, ${text.split("\n").length} lines, `,
      h("a", { href, target: "_blank", rel: "noopener" }, "raw")),
    /\.(md|markdown)$/i.test(name) ? markdown(text) : /\.json$/i.test(name) ? jsonView(text) : h("pre", {}, text),
    h("button", { class: "button ghost", type: "button", on: { click: () => showRun(rid) } }, "Back to run"));
}

// ------------------------------------------------------------------ compose

const mode = () => document.querySelector("input[name=mode]:checked").value;

function setMode(m) {
  document.querySelector(`input[name=mode][value=${m}]`).checked = true;
  const prompt = m === "prompt";
  $("projectField").hidden = prompt;
  $("generate").textContent = prompt ? "Write prompt" : "Generate plan";
  if (prompt && origin) { origin = null; drawOrigin(); }
  reading();
}

function followUp(p, text) {
  origin = { name: p.name, topic: p.topic || p.name };
  setMode("plan");
  if (p.project && S.projects.includes(p.project)) { $("project").value = p.project; deployHint(); }
  if (text) $("topic").value = text;
  $("detail").close();
  drawOrigin();
  reading();
  $("compose").scrollIntoView({ block: "start" });
  $("topic").focus();
}

function drawOrigin() {
  $("origin").hidden = !origin;
  $("origin").replaceChildren(...(origin ? ["↳ Follows up: ", h("strong", {}, origin.topic), " ",
    h("button", { class: "x", type: "button", "aria-label": "Do not follow up this plan", on: { click: () => { origin = null; drawOrigin(); } } }, "×")] : []));
}

function newTask() {
  for (const d of document.querySelectorAll("dialog[open]")) d.close();
  $("compose").scrollIntoView({ block: "start" });
  $("topic").focus();
}

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
    const attachments = await Promise.all(picked.map(readFile));
    const prompt = mode() === "prompt";
    await api("/api/generate", { topic: $("topic").value, project: prompt ? "" : $("project").value, mode: mode(),
      origin: origin ? origin.name : undefined, attachments });
    $("topic").value = "";
    picked = [];
    origin = null;
    drawFiles();
    drawOrigin();
    reading();
    toast(prompt ? "Writing the prompt." : "Planning started.");
    await refresh();
  } catch (err) {
    $("composeError").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}

function kb(n) {
  return n < 1024 * 1024 ? Math.max(1, Math.round(n / 1024)) + " KB" : (n / 1048576).toFixed(1) + " MB";
}

function drawFiles() {
  $("filelist").replaceChildren(...picked.map((f, i) => h("li", {}, h("span", {}, f.name), h("small", { class: "meta" }, kb(f.size)),
    h("button", { class: "x", type: "button", "aria-label": "Remove " + f.name, on: { click: () => { picked.splice(i, 1); drawFiles(); } } }, "×"))));
}

function addFiles(files) {
  for (const f of files) {
    // pasted screenshots all arrive as "image.png"; the server numbers duplicates
    if (!picked.some((p) => p.name === f.name && p.size === f.size && p.lastModified === f.lastModified)) picked.push(f);
  }
  drawFiles();
}

// ------------------------------------------------------------------ command palette

let pal = [], palAt = 0;

function paletteItems(q) {
  if (!S) return [];
  const items = [{ label: "New task", hint: "N", run: newTask }];
  for (const d of S.drafts) items.push({ label: d.topic || d.name, hint: "draft", run: () => show({ kind: "draft", name: d.name }) });
  for (const p of [...S.plans].reverse()) items.push({ label: p.topic || p.name, hint: { needs: "needs you", running: "running", done: "done" }[laneOf(p)], run: () => show({ kind: "plan", name: p.name }) });
  for (const p of S.prompts || []) items.push({ label: p.topic, hint: "copy prompt", run: () => copy(p.prompt) });
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  return items.filter((it) => words.every((w) => `${it.label} ${it.hint}`.toLowerCase().includes(w))).slice(0, 30);
}

function drawPalette() {
  pal = paletteItems($("paletteInput").value);
  palAt = Math.max(0, Math.min(palAt, pal.length - 1));
  $("paletteList").replaceChildren(...(pal.length ? pal.map((it, i) => h("li", { id: "pal" + i, role: "option", "aria-selected": String(i === palAt),
    class: i === palAt ? "on" : "", on: { click: () => pick(i), mousemove: () => { if (palAt !== i) { palAt = i; drawPalette(); } } } },
    h("span", {}, it.label), h("small", {}, it.hint))) : [h("li", { class: "none" }, "Nothing matches.")]));
  const input = $("paletteInput");
  if (pal.length) input.setAttribute("aria-activedescendant", "pal" + palAt); else input.removeAttribute("aria-activedescendant");
  const on = $("pal" + palAt);
  if (on) on.scrollIntoView({ block: "nearest" });
}

function openPalette() {
  $("paletteInput").value = "";
  palAt = 0;
  drawPalette();
  $("palette").showModal();
  $("paletteInput").focus();
}

function pick(i) {
  const it = pal[i];
  $("palette").close();
  if (it) it.run();
}

// ------------------------------------------------------------------ start

document.addEventListener("DOMContentLoaded", () => {
  $("compose").addEventListener("submit", generate);
  $("topic").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("compose").requestSubmit();
  });
  $("topic").addEventListener("input", reading);
  for (const r of document.querySelectorAll("input[name=mode]")) r.addEventListener("change", () => setMode(mode()));
  $("files").addEventListener("change", () => { addFiles($("files").files); $("files").value = ""; });
  $("topic").addEventListener("paste", (e) => { if (e.clipboardData.files.length) { e.preventDefault(); addFiles(e.clipboardData.files); } });
  const form = $("compose");
  form.addEventListener("dragover", (e) => { if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); form.classList.add("drop"); } });
  form.addEventListener("dragleave", (e) => { if (!form.contains(e.relatedTarget)) form.classList.remove("drop"); });
  form.addEventListener("drop", (e) => { e.preventDefault(); form.classList.remove("drop"); addFiles(e.dataTransfer.files); });
  $("reload").addEventListener("click", () => location.reload());
  $("closeDetail").addEventListener("click", () => $("detail").close());
  $("detail").addEventListener("close", () => { open = null; $("detailBody").dataset.for = ""; });
  $("paletteOpen").addEventListener("click", openPalette);
  $("palette").addEventListener("click", (e) => { if (e.target === $("palette")) $("palette").close(); });
  $("paletteInput").addEventListener("input", () => { palAt = 0; drawPalette(); });
  $("paletteInput").addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      palAt = (palAt + (e.key === "ArrowDown" ? 1 : -1) + pal.length) % Math.max(pal.length, 1);
      drawPalette();
    } else if (e.key === "Enter") { e.preventDefault(); pick(palAt); }
  });
  // Escape closes one layer at a time: the browser closes only the topmost modal dialog.
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if ($("palette").open) $("palette").close(); else openPalette();
      return;
    }
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable;
    if (!typing && !e.ctrlKey && !e.metaKey && !e.altKey && e.key.toLowerCase() === "n" && !document.querySelector("dialog[open]")) {
      e.preventDefault();
      newTask();
    }
  });
  refresh();
  const ev = new EventSource("/api/events");
  ev.onmessage = () => refresh();
});
