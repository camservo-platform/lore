"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  user: null,
  speechAvailable: false,
  mode: "text",
  campaign: null,      // {id, name, setting}
  busy: false,
  myTurns: new Set(),  // turn ids started from this tab (skip their echo on the feed)
  feed: null,
};

// ---------- helpers -------------------------------------------------------------------

function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 6000);
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// Just enough markdown for narration: paragraphs, line breaks, bold, italics.
function renderMarkdown(text) {
  return text
    .trim()
    .split(/\n{2,}/)
    .map((p) => {
      const html = escapeHtml(p)
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
        .replace(/^#+\s*/gm, "")
        .replace(/\n/g, "<br>");
      return `<p>${html}</p>`;
    })
    .join("");
}

function plainText(text) {
  return text.replace(/[*_#`>]/g, "").replace(/\s+/g, " ").trim();
}

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: options.body ? { "Content-Type": "application/json" } : {},
    ...options,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return resp;
}

// POST that answers with Server-Sent Events; calls onEvent for each data payload.
async function postStream(path, body, onEvent) {
  const resp = await api(path, { method: "POST", body: JSON.stringify(body) });
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      for (const line of chunk.split("\n")) {
        if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)));
      }
    }
  }
}

// ---------- mode ----------------------------------------------------------------------

function setMode(mode) {
  if (mode === "speech" && !state.speechAvailable) mode = "text";
  state.mode = mode;
  try { localStorage.setItem("lore.mode", mode); } catch {}
  document.querySelectorAll(".mode button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.mode === mode)));
  $("composer").hidden = mode === "speech";
  $("talk").hidden = mode !== "speech";
  if (mode === "text") speaker.stop();
}

document.querySelectorAll(".mode button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));

// ---------- lobby ---------------------------------------------------------------------

async function showLobby() {
  closeFeed();
  speaker.stop();
  state.campaign = null;
  $("table").hidden = true;
  $("lobby").hidden = false;
  $("campaign-title").textContent = "";
  history.replaceState(null, "", "/");
  const list = $("worlds");
  try {
    const campaigns = await (await api("/api/campaigns")).json();
    list.innerHTML = "";
    if (!campaigns.length) list.innerHTML = '<li class="empty">No worlds yet. Forge one to begin.</li>';
    for (const c of campaigns.reverse()) {
      const li = document.createElement("li");
      li.innerHTML = `<button class="world"><strong>${escapeHtml(c.name)}</strong><span>${escapeHtml(c.setting || "")}</span></button>`;
      li.querySelector("button").addEventListener("click", () => enterCampaign(c, false));
      list.append(li);
    }
  } catch (e) {
    list.innerHTML = `<li class="empty">Couldn't load worlds: ${escapeHtml(e.message)}</li>`;
  }
}

$("home").addEventListener("click", showLobby);

$("forge").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const button = form.querySelector("button");
  const progress = $("forge-progress");
  progress.hidden = false;
  progress.innerHTML = "";
  button.disabled = true;
  const add = (cls, text) => {
    const li = document.createElement("li");
    li.className = cls;
    li.textContent = text;
    progress.append(li);
  };
  let campaign = null;
  try {
    await postStream("/api/worlds", { theme: form.theme.value, name: form.name.value }, (ev) => {
      if (ev.type === "status") add("status", ev.text);
      else if (ev.type === "lore") add("", `${ev.kind}: ${ev.title}`);
      else if (ev.type === "error") add("error", ev.text);
      else if (ev.type === "done") campaign = ev.campaign;
    });
  } catch (err) {
    add("error", err.message);
  } finally {
    button.disabled = false;
  }
  if (campaign) {
    form.reset();
    progress.hidden = true;
    enterCampaign(campaign, true);
  }
});

// ---------- table ---------------------------------------------------------------------

async function enterCampaign(campaign, fresh) {
  state.campaign = campaign;
  $("lobby").hidden = true;
  $("table").hidden = false;
  $("campaign-title").textContent = campaign.name;
  $("transcript").innerHTML = "";
  history.replaceState(null, "", `/?campaign=${campaign.id}`);
  openFeed();
  await refreshState();
  if (fresh) {
    takeTurn("");  // the GM opens the first scene
  } else {
    addMessage("gm", "Welcome back. Say something, or press Send with an empty message for a recap.");
  }
}

function addMessage(kind, text, who) {
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  if (who) el.innerHTML = `<div class="who">${escapeHtml(who)}</div>`;
  const body = document.createElement("div");
  body.className = "body";
  if (kind === "gm") body.innerHTML = renderMarkdown(text);
  else body.textContent = text;
  el.append(body);
  $("transcript").append(el);
  el.scrollIntoView({ block: "end" });
  return el;
}

const TOOL_VERBS = {
  roll_dice: "rolling dice", search_lore: "consulting the lore", add_lore: "recording lore",
  get_lore: "consulting the lore", list_lore: "consulting the lore", get_character: "checking a character sheet",
  list_characters: "looking over the party", recent_events: "reviewing the chronicle", apply_damage: "applying damage",
  heal: "healing", add_item: "updating inventory", remove_item: "updating inventory", adjust_gold: "counting coin",
  create_character: "creating a character", log_event: "writing in the chronicle",
};

function setActivity(text) {
  const el = $("activity");
  el.hidden = !text;
  el.textContent = text ? `The Game Master is ${text}…` : "";
}

function setBusy(busy) {
  state.busy = busy;
  $("send").disabled = busy;
  $("mic").disabled = busy && !recorder.active;
}

async function takeTurn(message) {
  if (state.busy || !state.campaign) return;
  setBusy(true);
  if (message) addMessage("player", message, state.user);
  const gm = addMessage("gm", "");
  gm.classList.add("streaming");
  const body = gm.querySelector(".body");
  let text = "";
  setActivity("thinking");
  speaker.begin();
  try {
    await postStream(`/api/campaigns/${state.campaign.id}/turn`,
      { campaign: state.campaign.name, message, mode: state.mode },
      (ev) => {
        if (ev.type === "turn") state.myTurns.add(ev.id);
        else if (ev.type === "text") {
          text += ev.text;
          body.innerHTML = renderMarkdown(text);
          gm.scrollIntoView({ block: "end" });
          setActivity("");
          if (state.mode === "speech") speaker.feed(ev.text);
        } else if (ev.type === "tool") setActivity(TOOL_VERBS[ev.name] || "working");
        else if (ev.type === "error") addMessage("error", ev.text);
      });
  } catch (err) {
    addMessage("error", err.message);
  } finally {
    gm.classList.remove("streaming");
    if (!text.trim()) gm.remove();
    if (state.mode === "speech") speaker.flush();
    setActivity("");
    setBusy(false);
    refreshState();
  }
}

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("message");
  const text = input.value.trim();
  input.value = "";
  takeTurn(text);
});

$("message").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("composer").requestSubmit();
  }
});

$("new-session").addEventListener("click", async () => {
  if (!state.campaign || !confirm("Start a new conversation? The Game Master forgets this chat but keeps the world, characters and chronicle.")) return;
  try {
    await api(`/api/campaigns/${state.campaign.id}/reset`, { method: "POST" });
    $("transcript").innerHTML = "";
    takeTurn("");
  } catch (e) {
    toast(e.message);
  }
});

async function refreshState() {
  if (!state.campaign) return;
  try {
    const data = await (await api(`/api/campaigns/${state.campaign.id}/state?name=${encodeURIComponent(state.campaign.name)}`)).json();
    renderParty(data.characters);
    const log = $("log");
    log.innerHTML = "";
    data.events.slice().reverse().forEach((ev) => log.append(eventItem(ev, false)));
  } catch (e) {
    toast(`Couldn't load the table: ${e.message}`);
  }
}

function renderParty(characters) {
  const party = $("party");
  party.innerHTML = characters.length ? "" : '<li class="meta">No characters yet.</li>';
  for (const c of characters) {
    const li = document.createElement("li");
    if (c.status !== "alive") li.className = "down";
    const pct = Math.max(0, Math.min(100, (100 * c.hp) / c.max_hp));
    li.innerHTML = `
      <div><span class="name">${escapeHtml(c.name)}</span>
        <span class="meta">${c.player ? escapeHtml(c.player) : "NPC"}${c.status !== "alive" ? " · " + c.status : ""}</span></div>
      <div class="hp"><div style="width:${pct}%"></div></div>
      <div class="meta">${c.hp}/${c.max_hp} HP${c.location ? " · " + escapeHtml(c.location) : ""}</div>`;
    party.append(li);
  }
}

function eventItem(ev, fresh) {
  const li = document.createElement("li");
  if (fresh) li.className = "fresh";
  li.textContent = ev.summary;
  li.title = `${ev.actor} · ${new Date(ev.occurred_at).toLocaleString()}`;
  return li;
}

// Live updates: game events from anyone, and turns taken by other players.
function openFeed() {
  closeFeed();
  const feed = new EventSource(`/api/campaigns/${state.campaign.id}/feed`);
  feed.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.type === "event") {
      $("log").prepend(eventItem(ev.event, true));
      clearTimeout(openFeed.refresh);
      openFeed.refresh = setTimeout(refreshState, 800);
    } else if (ev.type === "chat" && !state.myTurns.has(ev.turn)) {
      addMessage("player", ev.message, ev.user);
      addMessage("gm", ev.reply);
      if (state.mode === "speech") { speaker.begin(); speaker.feed(ev.reply); speaker.flush(); }
    }
  };
  state.feed = feed;
}

function closeFeed() {
  if (state.feed) state.feed.close();
  state.feed = null;
}

// ---------- speech out ----------------------------------------------------------------

// Speaks narration as it streams: complete sentences are sent to TTS in chunks and
// played back in order while later text is still arriving.
const speaker = {
  pending: "",
  queue: Promise.resolve(),
  audio: null,
  generation: 0,
  begin() {
    this.pending = "";
  },
  feed(text) {
    this.pending += text;
    const cut = this.lastBoundary(this.pending);
    if (cut > 160) {
      this.say(this.pending.slice(0, cut));
      this.pending = this.pending.slice(cut);
    }
  },
  flush() {
    if (this.pending.trim()) this.say(this.pending);
    this.pending = "";
  },
  lastBoundary(text) {
    let end = -1;
    const re = /[.!?…]["'”’)\]]*\s/g;
    let m;
    while ((m = re.exec(text)) && m.index < 1800) end = m.index + m[0].length;
    return end;
  },
  say(text) {
    const clean = plainText(text);
    if (!clean) return;
    const generation = this.generation;
    // Start fetching now; play once everything queued before it has finished.
    const audio = api("/api/tts", { method: "POST", body: JSON.stringify({ text: clean }) })
      .then((r) => r.blob())
      .catch((e) => { toast(`Speech failed: ${e.message}`); return null; });
    this.queue = this.queue.then(async () => {
      const blob = await audio;
      if (!blob || generation !== this.generation) return;
      await this.play(blob);
    });
  },
  play(blob) {
    return new Promise((resolve) => {
      const url = URL.createObjectURL(blob);
      const el = new Audio(url);
      this.audio = el;
      const done = () => { URL.revokeObjectURL(url); this.audio = null; resolve(); };
      el.onended = done;
      el.onerror = done;
      el.play().catch(done);
    });
  },
  stop() {
    this.generation++;
    this.pending = "";
    if (this.audio) { this.audio.pause(); this.audio.dispatchEvent(new Event("ended")); }
    this.queue = Promise.resolve();
  },
};

// ---------- speech in -----------------------------------------------------------------

const recorder = {
  active: false,
  media: null,
  chunks: [],
  async start() {
    if (this.active || state.busy) return;
    speaker.stop();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      this.media = new MediaRecorder(stream);
    } catch (e) {
      toast(`Microphone unavailable: ${e.message}`);
      return;
    }
    this.chunks = [];
    this.media.ondataavailable = (e) => e.data.size && this.chunks.push(e.data);
    this.media.start();
    this.active = true;
    $("mic").setAttribute("aria-pressed", "true");
    $("mic-label").textContent = "Listening… tap to send";
  },
  async stop() {
    if (!this.active) return;
    this.active = false;
    $("mic").setAttribute("aria-pressed", "false");
    $("mic-label").textContent = "Transcribing…";
    $("mic").disabled = true;
    const stopped = new Promise((resolve) => (this.media.onstop = resolve));
    this.media.stop();
    await stopped;
    this.media.stream.getTracks().forEach((t) => t.stop());
    const blob = new Blob(this.chunks, { type: this.media.mimeType || "audio/webm" });
    try {
      const resp = await api("/api/stt", { method: "POST", body: blob, headers: { "Content-Type": blob.type } });
      const { text } = await resp.json();
      if (text.trim()) await takeTurn(text.trim());
      else toast("I didn't catch that. Try again.");
    } catch (e) {
      toast(`Transcription failed: ${e.message}`);
    } finally {
      $("mic-label").textContent = "Tap to speak";
      $("mic").disabled = state.busy;
    }
  },
};

$("mic").addEventListener("click", () => (recorder.active ? recorder.stop() : recorder.start()));

// Hold space to talk (when not typing).
const typing = () => ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName);
document.addEventListener("keydown", (e) => {
  if (e.code === "Space" && !e.repeat && state.mode === "speech" && !$("table").hidden && !typing()) {
    e.preventDefault();
    recorder.start();
  }
});
document.addEventListener("keyup", (e) => {
  if (e.code === "Space" && state.mode === "speech" && recorder.active && !typing()) {
    e.preventDefault();
    recorder.stop();
  }
});

// ---------- start ---------------------------------------------------------------------

(async function init() {
  try {
    const me = await (await api("/api/me")).json();
    state.user = me.user;
    state.speechAvailable = me.speech;
    $("user").textContent = me.user;
  } catch (e) {
    toast(e.message);
  }
  if (!state.speechAvailable) {
    const b = document.querySelector('.mode button[data-mode="speech"]');
    b.disabled = true;
    b.title = "Speech needs a Deepgram API key on the server.";
  }
  let saved = "text";
  try { saved = localStorage.getItem("lore.mode") || "text"; } catch {}
  setMode(saved);

  const wanted = Number(new URLSearchParams(location.search).get("campaign"));
  await showLobby();
  if (wanted) {
    const campaigns = await (await api("/api/campaigns")).json();
    const c = campaigns.find((c) => c.id === wanted);
    if (c) enterCampaign(c, false);
  }
})();
