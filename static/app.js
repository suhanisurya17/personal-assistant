"use strict";

/* ============================================================
   Widget registry: add a widget by adding an entry here.
   ============================================================ */
const WIDGETS = {
  brief:     { title: "Daily summary",   render: renderBrief },
  next:      { title: "Next up",         render: renderNext },
  schedule:  { title: "Today",           render: renderSchedule },
  attention: { title: "Needs attention", render: renderAttention },
  news:      { title: "News",            render: renderNews },
  photos:    { title: "Photos",          render: renderPhotos },
};

const DEFAULT_LAYOUT = [
  { id: "brief", size: 2 },
  { id: "next", size: 1 },
  { id: "schedule", size: 1 },
  { id: "attention", size: 2 },
  { id: "news", size: 2 },
  { id: "photos", size: 1 },
];

const ENDPOINTS = {
  schedule: "/api/schedule",
  attention: "/api/attention",
  news: "/api/news",
  brief: "/api/brief",
  photos: "/api/photos",
};

const grid = document.getElementById("grid");
const state = {
  layout: [],
  editing: false,
  data: {},   // endpoint key -> Promise (shared by widgets so each endpoint is fetched once)
  cards: {},  // widget id -> element
  tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
};

/* ============================================================
   Helpers
   ============================================================ */
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = (url) => (/^https?:\/\//i.test(url || "") ? url : null);
const store = {
  get(key, fallback) { try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ } },
};

function fmtTime(iso) {
  return new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", timeZone: state.tz })
    .format(new Date(iso)).replace(":00", "");
}
function fmtDuration(minutes) {
  const h = Math.floor(minutes / 60), m = Math.round(minutes % 60);
  if (!h) return `${m} min`;
  return m ? `${h}h ${m}m` : `${h}h`;
}
function todayYMD() {
  return new Intl.DateTimeFormat("en-CA", { timeZone: state.tz }).format(new Date());
}
function addDays(ymd, n) {
  const d = new Date(`${ymd}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
function fmtDay(ymd) {
  const today = todayYMD();
  if (ymd === today) return "today";
  if (ymd === addDays(today, 1)) return "tomorrow";
  return new Intl.DateTimeFormat("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" })
    .format(new Date(`${ymd}T12:00:00Z`));
}

async function getJSON(url, options) {
  const res = await fetch(url, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = body.detail || {};
    const err = new Error(detail.message || `Request failed (${res.status})`);
    err.code = detail.code;
    throw err;
  }
  return body;
}
function load(key, { force = false, query = "" } = {}) {
  if (force || !state.data[key]) {
    state.data[key] = getJSON(ENDPOINTS[key] + query);
    state.data[key].catch(() => {}); // widgets render the error themselves
  }
  return state.data[key];
}

const skeleton = `<div class="skeleton"><span></span><span></span><span></span></div>`;
function errorBox(err) {
  const hints = {
    google_reauthorization_required: "Google needs you to sign in again. Run python app.py once, finish the browser sign-in, then refresh.",
    google_credentials_missing: "Add your Google OAuth client file and set GOOGLE_CREDENTIALS_FILE in .env.",
    google_authentication_error: "Google sign-in failed. Delete token.json and sign in again.",
  };
  return `<div class="error"><b>Couldn’t load this</b>${esc(hints[err.code] || err.message)}</div>`;
}

/* ============================================================
   Widgets: each receives {body, foot, meta, force} and fills them.
   ============================================================ */
async function renderBrief({ body, foot, force }) {
  // The server caches the brief per day; only "Rewrite" (force) asks the model again.
  const b = await load("brief", { force, query: force ? "?refresh=true" : "" });
  const key = `focus:${b.day}`;
  const done = new Set(store.get(key, []));
  body.innerHTML = `
    <h2 class="brief-headline">${esc(b.headline)}</h2>
    <p class="brief-summary">${esc(b.summary)}</p>
    ${b.focus.length ? `<ul class="focus">${b.focus.map((f) => `
      <li><label><input type="checkbox" data-focus="${esc(f)}" ${done.has(f) ? "checked" : ""}><span>${esc(f)}</span></label></li>`).join("")}
    </ul>` : ""}`;
  body.querySelectorAll("input[data-focus]").forEach((box) =>
    box.addEventListener("change", () => {
      box.checked ? done.add(box.dataset.focus) : done.delete(box.dataset.focus);
      store.set(key, [...done]);
    }));
  foot.innerHTML = `<span>${b.ai ? `Written ${fmtTime(b.generated_at)}` : "Basic summary · AI unavailable right now"}</span>
    <button class="link-btn" type="button">${b.ai ? "Rewrite" : "Try AI again"}</button>`;
  foot.querySelector("button").addEventListener("click", () => renderWidget("brief", { force: true }));
}

async function renderNext({ body, meta }) {
  const s = await load("schedule");
  const now = new Date();
  const timed = s.events.filter((e) => !e.all_day && new Date(e.end) > now);
  const ev = timed[0];
  if (!ev) {
    const t = s.tomorrow.find((e) => !e.all_day);
    body.innerHTML = `<p class="next-count">All done</p>
      <p class="muted">Nothing left on today’s calendar.${t ? ` Tomorrow starts with <b>${esc(t.title)}</b> at ${fmtTime(t.start)}.` : ""}</p>`;
    meta.textContent = "";
    return;
  }
  const live = new Date(ev.start) <= now;
  const mins = Math.max(1, Math.round(((live ? new Date(ev.end) : new Date(ev.start)) - now) / 6e4));
  body.innerHTML = `
    <p class="next-count num">${live ? "Now" : `in ${fmtDuration(mins)}`}</p>
    <p class="next-title">${esc(ev.title)}</p>
    <p class="muted" style="margin:0">${fmtTime(ev.start)} – ${fmtTime(ev.end)}${ev.location ? ` · ${esc(ev.location)}` : ""}</p>
    ${live ? `<p class="faint" style="margin:6px 0 0">Ends in ${fmtDuration(mins)}</p>` : ""}`;
  meta.textContent = timed.length > 1 ? `then ${timed[1].title.slice(0, 22)} at ${fmtTime(timed[1].start)}` : "";
}

async function renderSchedule({ body, foot, meta }) {
  const s = await load("schedule");
  const now = new Date();
  meta.textContent = `${s.events.length} event${s.events.length === 1 ? "" : "s"}`;
  const allDay = s.events.filter((e) => e.all_day).map((e) => ({ type: "allday", e }));
  const rows = [
    ...s.events.filter((e) => !e.all_day).map((e) => ({ type: "event", at: new Date(e.start), e })),
    // Free blocks shrink as the day goes on, even between data fetches.
    ...s.free.map((f) => {
      const start = new Date(Math.max(new Date(f.start), now));
      return { type: "free", at: start, f: { ...f, start: start.toISOString(), minutes: Math.round((new Date(f.end) - start) / 6e4) } };
    }).filter((row) => row.f.minutes >= 30),
  ].sort((a, b) => a.at - b.at);

  const html = [...allDay, ...rows].map((row) => {
    if (row.type === "free") {
      return `<li class="free"><span class="time">${fmtTime(row.f.start)}</span>
        <span class="ev-title"><b>${fmtDuration(row.f.minutes)} free</b> until ${fmtTime(row.f.end)}</span></li>`;
    }
    const e = row.e;
    const cls = e.all_day ? "" : new Date(e.end) <= now ? "past" : new Date(e.start) <= now ? "now" : "";
    const href = safeUrl(e.link);
    return `<li class="${cls}">
      <span class="time">${e.all_day ? "All day" : fmtTime(e.start)}</span>
      <span><span class="ev-title">${href ? `<a href="${esc(href)}" target="_blank" rel="noopener">${esc(e.title)}</a>` : esc(e.title)}</span>
      <span class="ev-sub">${e.all_day ? "" : `until ${fmtTime(e.end)}`}${e.location ? ` · ${esc(e.location)}` : ""}</span></span>
    </li>`;
  }).join("");
  body.innerHTML = html ? `<ul class="list timeline">${html}</ul>` : `<p class="empty">Nothing scheduled today.</p>`;

  const first = s.tomorrow.find((e) => !e.all_day);
  foot.innerHTML = `<p class="tomorrow">${s.tomorrow.length
    ? `Tomorrow · ${s.tomorrow.length} event${s.tomorrow.length === 1 ? "" : "s"}${first ? `, first at ${fmtTime(first.start)}` : ""}`
    : "Tomorrow is clear"}</p>`;
}

const KIND_LABEL = { deadline: "Deadline", meeting: "Meeting", reply: "Reply" };
async function renderAttention({ body, meta, foot }) {
  const a = await load("attention");
  const urgent = a.items.filter((i) => i.urgent).length;
  meta.innerHTML = urgent ? `<span class="urgent-dot"></span>${urgent} urgent` : `${a.items.length} item${a.items.length === 1 ? "" : "s"}`;
  if (!a.items.length) {
    body.innerHTML = `<p class="empty">${a.error ? "Couldn’t check new emails just now." : "Nothing needs you right now."}</p>`;
  } else {
    body.innerHTML = `<ul class="list attention">${a.items.map((i) => {
      let linked = "";
      if (i.due) {
        const when = i.kind === "meeting" ? `Meeting ${fmtDay(i.due)}` : `Due ${fmtDay(i.due)}`;
        const evs = i.day_events.slice(0, 3).map((e) => `${e.start ? fmtTime(e.start) + " " : ""}${esc(e.title)}`);
        const extra = i.day_events.length > 3 ? ` +${i.day_events.length - 3}` : "";
        linked = `<span class="linked"><b>${when}</b> · ${evs.length ? `that day: ${evs.join(", ")}${extra}` : "calendar is clear that day"}</span>`;
      }
      return `<li class="${i.urgent ? "urgent" : ""}"><a href="${esc(safeUrl(i.link) || "#")}" target="_blank" rel="noopener">
        <span class="kind">${i.urgent ? `<span class="urgent-dot"></span>` : ""}${KIND_LABEL[i.kind]}</span>
        <span><span class="reason">${esc(i.reason)}</span>
          <span class="from">${esc(i.sender_name)} · ${esc(i.subject)}</span>${linked}</span>
      </a></li>`;
    }).join("")}</ul>`;
  }
  foot.innerHTML = `<span>${a.error ? `Some new emails weren’t checked: ${esc(a.error)}` : `AI-sorted from your last ${a.checked} inbox emails`}</span>
    <a class="link-btn" href="https://mail.google.com/" target="_blank" rel="noopener">Open Gmail</a>`;
}

async function renderNews({ body, meta, foot }) {
  const n = await load("news");
  meta.textContent = n.source ? n.source.replace(/\s*<.*>$/, "").replace(/"/g, "") : "";
  if (!n.stories.length) {
    body.innerHTML = `<p class="empty">${n.error
      ? `Found this week’s issue but couldn’t summarize it (${esc(n.error)}).`
      : "No TLDR issue in your inbox from the last week."}</p>`;
  } else {
    body.innerHTML = `<ol class="list news">${n.stories.map((s, idx) => {
      const href = safeUrl(s.url);
      const head = `<h3>${esc(s.title)}</h3>`;
      return `<li><span class="n">0${idx + 1}</span>${href ? `<a href="${esc(href)}" target="_blank" rel="noopener">${head}</a>` : head}
        <p>${esc(s.summary)}</p></li>`;
    }).join("")}</ol>`;
  }
  foot.innerHTML = n.issue_id
    ? `<span>${esc(n.issue_subject)}</span><a class="link-btn" href="https://mail.google.com/mail/u/0/#inbox/${esc(n.issue_id)}" target="_blank" rel="noopener">Read full issue</a>`
    : "";
}

/* ---------- photos (Google Photos Picker) ---------- */
const slideshow = { timer: null, index: 0, paused: false, picking: false };

function photoUrl(p) { return `/api/photos/file/${encodeURIComponent(p.file)}`; }
function fmtMonth(iso) {
  return iso ? new Intl.DateTimeFormat("en-US", { month: "short", year: "numeric" }).format(new Date(iso)) : "";
}

async function renderPhotos({ body, foot, meta, force }) {
  if (slideshow.picking) return; // don't wipe the picker status mid-flow
  clearInterval(slideshow.timer);
  const set = await load("photos", { force });
  const colour = store.get("photos:colour", false);
  if (!set.photos.length) {
    meta.textContent = "";
    body.innerHTML = `<div class="photo-empty">
      <p class="muted">Pick some favourites from Google Photos and they’ll rotate here.</p>
      <button class="btn" type="button" data-pick>Choose photos</button>
      <p class="faint photo-status" role="status"></p></div>`;
    foot.innerHTML = "";
    body.querySelector("[data-pick]").addEventListener("click", () => pickPhotos(body));
    return;
  }
  const photos = set.photos;
  slideshow.index = slideshow.index % photos.length;
  meta.textContent = `${photos.length} photo${photos.length === 1 ? "" : "s"}`;
  body.innerHTML = `
    <div class="photo-frame ${colour ? "colour" : ""}" tabindex="0" aria-roledescription="slideshow" aria-label="Photos">
      <img class="photo-layer on" alt="">
      <img class="photo-layer" alt="">
      <button class="photo-nav prev" type="button" aria-label="Previous photo">‹</button>
      <button class="photo-nav next" type="button" aria-label="Next photo">›</button>
      <span class="photo-date"></span>
    </div>
    <p class="faint photo-status" role="status"></p>`;
  const frame = body.querySelector(".photo-frame");
  const layers = [...frame.querySelectorAll(".photo-layer")];
  const date = frame.querySelector(".photo-date");
  const show = (i, instant = false) => {
    slideshow.index = (i + photos.length) % photos.length;
    const p = photos[slideshow.index];
    const [current, nextLayer] = layers[0].classList.contains("on") ? layers : [layers[1], layers[0]];
    const swap = () => {
      nextLayer.classList.add("on"); current.classList.remove("on");
      date.textContent = fmtMonth(p.created);
    };
    nextLayer.onload = swap;
    nextLayer.src = photoUrl(p);
    if (instant && nextLayer.complete) swap();
    // Warm the cache for the following photo so the fade is smooth.
    new Image().src = photoUrl(photos[(slideshow.index + 1) % photos.length]);
  };
  show(slideshow.index, true);
  frame.querySelector(".prev").addEventListener("click", () => show(slideshow.index - 1));
  frame.querySelector(".next").addEventListener("click", () => show(slideshow.index + 1));
  frame.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") show(slideshow.index - 1);
    if (e.key === "ArrowRight") show(slideshow.index + 1);
  });
  frame.addEventListener("mouseenter", () => { slideshow.paused = true; });
  frame.addEventListener("mouseleave", () => { slideshow.paused = false; });
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (photos.length > 1 && !reduceMotion) {
    slideshow.timer = setInterval(() => { if (!slideshow.paused && !document.hidden) show(slideshow.index + 1); }, 8000);
  }
  foot.innerHTML = `<button class="link-btn" type="button" data-colour>${colour ? "Black & white" : "Colour"}</button>
    <button class="link-btn" type="button" data-pick>Choose different photos</button>`;
  foot.querySelector("[data-colour]").addEventListener("click", (e) => {
    const on = !frame.classList.contains("colour");
    frame.classList.toggle("colour", on);
    store.set("photos:colour", on);
    e.target.textContent = on ? "Black & white" : "Colour";
  });
  foot.querySelector("[data-pick]").addEventListener("click", () => pickPhotos(body));
}

async function pickPhotos(body) {
  if (slideshow.picking) return;
  const status = (text) => { const el = body.querySelector(".photo-status"); if (el) el.textContent = text; };
  // Open the tab right away (inside the click) so the browser doesn't block it as a popup.
  const tab = window.open("", "_blank");
  if (tab) tab.document.write("<p style='font:16px system-ui;padding:24px'>Connecting to Google Photos…</p>");
  slideshow.picking = true;
  status("Connecting… If this is your first time, approve Google Photos access in the browser.");
  try {
    const session = await getJSON("/api/photos/session", { method: "POST" });
    if (tab && !tab.closed) tab.location.href = session.picker_url;
    else window.open(session.picker_url, "_blank");
    status("Pick your photos in the Google Photos tab, then click Done there.");
    const deadline = Date.now() + 20 * 60_000;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, session.poll_seconds * 1000));
      const { done } = await getJSON(`/api/photos/session/${encodeURIComponent(session.id)}`);
      if (done) {
        status("Saving your photos…");
        await getJSON(`/api/photos/session/${encodeURIComponent(session.id)}/import`, { method: "POST" });
        slideshow.picking = false;
        slideshow.index = 0;
        return renderWidget("photos", { force: true });
      }
    }
    status("Timed out waiting for a selection. Try again when you’re ready.");
  } catch (err) {
    if (tab && !tab.closed) tab.close();
    status(err.message);
  }
  slideshow.picking = false;
}

/* ============================================================
   Cards & layout
   ============================================================ */
function makeCard(id) {
  const card = document.createElement("article");
  card.className = "card";
  card.dataset.id = id;
  card.innerHTML = `
    <div class="card-head">
      <span class="handle" aria-hidden="true">⠿</span>
      <h2 class="card-title">${esc(WIDGETS[id].title)}</h2>
      <span class="card-meta"></span>
      <div class="edit-controls">
        <button class="icon-btn move" type="button" data-move="-1" aria-label="Move earlier">↑</button>
        <button class="icon-btn move" type="button" data-move="1" aria-label="Move later">↓</button>
        ${[1, 2, 3].map((n) => `<button class="icon-btn" type="button" data-size="${n}" aria-label="Size ${"SML"[n - 1]}">${"SML"[n - 1]}</button>`).join("")}
        <button class="icon-btn" type="button" data-hide aria-label="Hide ${esc(WIDGETS[id].title)}">×</button>
      </div>
    </div>
    <div class="card-body">${skeleton}</div>
    <div class="card-foot"></div>`;
  card.querySelectorAll("[data-size]").forEach((b) => b.addEventListener("click", () => setEntry(id, { size: Number(b.dataset.size) })));
  card.querySelector("[data-hide]").addEventListener("click", () => setEntry(id, { hidden: true }));
  card.querySelectorAll("[data-move]").forEach((b) => b.addEventListener("click", () => moveBy(id, Number(b.dataset.move))));
  wireDrag(card);
  return card;
}

async function renderWidget(id, { force = false } = {}) {
  const card = state.cards[id];
  const body = card.querySelector(".card-body");
  const parts = { body, foot: card.querySelector(".card-foot"), meta: card.querySelector(".card-meta"), force };
  if (force) body.innerHTML = skeleton;
  try {
    await WIDGETS[id].render(parts);
  } catch (err) {
    body.innerHTML = errorBox(err);
    parts.meta.textContent = "";
    parts.foot.innerHTML = "";
  }
}

function applyLayout() {
  for (const entry of state.layout) {
    const card = state.cards[entry.id];
    card.dataset.size = entry.size;
    card.hidden = entry.hidden;
    card.draggable = state.editing;
    card.querySelectorAll("[data-size]").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.size) === entry.size)));
    grid.appendChild(card); // appending in layout order reorders the DOM
  }
  const tray = document.getElementById("tray");
  tray.innerHTML = state.layout.filter((e) => e.hidden).map((e) =>
    `<button class="chip" type="button" data-show="${e.id}">+ ${esc(WIDGETS[e.id].title)}</button>`).join("");
  tray.querySelectorAll("[data-show]").forEach((b) => b.addEventListener("click", () => setEntry(b.dataset.show, { hidden: false })));
}

let saveTimer;
function saveLayout() {
  store.set("layout", state.layout);
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    fetch("/api/layout", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ layout: state.layout }) }).catch(() => {});
  }, 400);
}
function setEntry(id, patch) {
  Object.assign(state.layout.find((e) => e.id === id), patch);
  applyLayout(); saveLayout();
}
function moveBy(id, delta) {
  const visible = state.layout.filter((e) => !e.hidden);
  const target = visible[visible.findIndex((e) => e.id === id) + delta];
  if (target) reorder(id, target.id, delta > 0 ? "after" : "before");
}
function reorder(id, targetId, where) {
  if (id === targetId) return;
  const [entry] = state.layout.splice(state.layout.findIndex((e) => e.id === id), 1);
  let to = state.layout.findIndex((e) => e.id === targetId);
  if (where === "after") to += 1;
  state.layout.splice(to, 0, entry);
  applyLayout(); saveLayout();
}
function normalizeLayout(saved) {
  const known = (saved || []).filter((e) => WIDGETS[e.id]);
  const ids = new Set(known.map((e) => e.id));
  // Widgets added after the layout was saved show up at the end.
  return [...known, ...DEFAULT_LAYOUT.filter((e) => !ids.has(e.id))]
    .map((e) => ({ id: e.id, size: [1, 2, 3].includes(e.size) ? e.size : 1, hidden: !!e.hidden }));
}

/* ---------- drag and drop ---------- */
let dragId = null;
function wireDrag(card) {
  card.addEventListener("dragstart", (e) => {
    if (!state.editing) return e.preventDefault();
    dragId = card.dataset.id;
    card.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", dragId);
  });
  card.addEventListener("dragend", () => {
    dragId = null;
    document.querySelectorAll(".card").forEach((c) => c.classList.remove("dragging", "drop-before", "drop-after"));
  });
  card.addEventListener("dragover", (e) => {
    if (!dragId || dragId === card.dataset.id) return;
    e.preventDefault();
    const r = card.getBoundingClientRect();
    const before = e.clientX < r.left + r.width / 2;
    card.classList.toggle("drop-before", before);
    card.classList.toggle("drop-after", !before);
  });
  card.addEventListener("dragleave", () => card.classList.remove("drop-before", "drop-after"));
  card.addEventListener("drop", (e) => {
    e.preventDefault();
    const where = card.classList.contains("drop-before") ? "before" : "after";
    card.classList.remove("drop-before", "drop-after");
    if (dragId) reorder(dragId, card.dataset.id, where);
  });
}

function setEditing(on) {
  state.editing = on;
  document.body.classList.toggle("editing", on);
  document.getElementById("edit-bar").hidden = !on;
  const toggle = document.getElementById("edit-toggle");
  toggle.setAttribute("aria-pressed", String(on));
  toggle.textContent = on ? "Done" : "Customize";
  applyLayout();
}

/* ============================================================
   Boot
   ============================================================ */
function paintHeader() {
  const now = new Date();
  const hour = Number(new Intl.DateTimeFormat("en-US", { hour: "numeric", hourCycle: "h23", timeZone: state.tz }).format(now));
  document.getElementById("greeting").textContent = hour < 5 ? "Up late" : hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
  document.getElementById("today-date").textContent = new Intl.DateTimeFormat("en-US", { weekday: "long", month: "long", day: "numeric", timeZone: state.tz }).format(now);
}

// Refresh re-reads Gmail/Calendar. It never forces the brief: that stays cached for the day.
function refreshAll({ force = false } = {}) {
  if (force) for (const key of ["schedule", "attention", "news"]) delete state.data[key];
  load("schedule").then((s) => { state.tz = s.timezone || state.tz; paintHeader(); }).catch(() => {});
  for (const entry of state.layout) renderWidget(entry.id);
  document.getElementById("last-sync").textContent = `Updated ${fmtTime(new Date().toISOString())}`;
}

function initTheme() {
  const saved = store.get("theme", null);
  const dark = saved ? saved === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.getElementById("theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    store.set("theme", next);
  });
}

async function boot() {
  initTheme();
  paintHeader();
  let saved = null;
  try { saved = (await getJSON("/api/layout")).layout; } catch { /* fall back to this browser's copy */ }
  state.layout = normalizeLayout(saved || store.get("layout", null) || DEFAULT_LAYOUT);
  for (const entry of state.layout) state.cards[entry.id] = makeCard(entry.id);
  applyLayout();
  refreshAll();

  document.getElementById("edit-toggle").addEventListener("click", () => setEditing(!state.editing));
  document.getElementById("edit-done").addEventListener("click", () => setEditing(false));
  document.getElementById("refresh-all").addEventListener("click", () => refreshAll({ force: true }));
  document.getElementById("reset-layout").addEventListener("click", () => {
    state.layout = normalizeLayout(DEFAULT_LAYOUT); applyLayout(); saveLayout();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.editing) setEditing(false); });

  // Every minute: redraw time-sensitive cards from data already loaded (no requests).
  setInterval(() => { paintHeader(); renderWidget("next"); renderWidget("schedule"); }, 60_000);
  // Every 10 minutes: re-read Gmail/Calendar. Only brand-new emails reach the model.
  setInterval(() => refreshAll({ force: true }), 10 * 60_000);
}

boot();
