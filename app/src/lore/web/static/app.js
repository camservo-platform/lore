"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  user: null,
  speechAvailable: false,
  admin: false,
  mode: "text",
  campaign: null,      // {id, name, setting}
  beforeAdmin: null,   // the campaign open when admin was toggled on
  busy: false,
  myTurns: new Set(),  // turn ids started from this tab (skip their echo on the feed)
  feed: null,
  voice: null,         // the GM's TTS voice (null: server default)
  sheets: new Map(),   // character name -> expanded? (survives sidebar refreshes)
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

// v2: speech became the default, so earlier saved choices start over once.
const MODE_KEY = "lore.mode.v2";

function setMode(mode) {
  if (mode === "speech" && !state.speechAvailable) mode = "text";
  state.mode = mode;
  try { localStorage.setItem(MODE_KEY, mode); } catch {}
  document.querySelectorAll(".mode button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.mode === mode)));
  $("composer").hidden = mode === "speech";
  $("talk").hidden = mode !== "speech";
  if (mode === "text") {
    talk.stop();
    speaker.stop();
  }
}

document.querySelectorAll(".mode button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));

// ---------- lobby ---------------------------------------------------------------------

function showView(name) {
  if (name !== "table") talk.stop();
  for (const view of ["lobby", "table", "admin"]) $(view).hidden = view !== name;
  $("admin-link").setAttribute("aria-pressed", String(name === "admin"));
}

async function showLobby() {
  state.beforeAdmin = null;
  closeFeed();
  speaker.stop();
  state.campaign = null;
  showView("lobby");
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

// The button says what will happen: a themed world, or one the GM invents.
$("forge").theme.addEventListener("input", (e) => {
  $("forge-button").textContent = e.target.value.trim() ? "Forge world" : "Surprise me";
});

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
    $("forge-button").textContent = "Surprise me";
    progress.hidden = true;
    enterCampaign(campaign, true);
  }
});

// ---------- table ---------------------------------------------------------------------

async function enterCampaign(campaign, fresh) {
  talk.stop();
  if (state.campaign?.id !== campaign.id) state.sheets.clear();
  state.campaign = campaign;
  showView("table");
  $("campaign-title").textContent = campaign.name;
  $("transcript").innerHTML = "";
  history.replaceState(null, "", `/?campaign=${campaign.id}`);
  openFeed();
  const lines = fresh ? [] : await recentLines(campaign);
  await refreshState();
  if (fresh) {
    takeTurn("");  // the GM opens the first scene
  } else if (lines.length) {
    for (const line of lines) addMessage(line.role, line.text, line.role === "player" ? state.user : undefined);
  } else {
    addMessage("gm", state.mode === "speech"
      ? "Welcome back. Start the conversation and speak to continue, or ask for a recap."
      : "Welcome back. Say something, or press Send with an empty message for a recap.");
  }
}

// Your own recent lines at this table, so returning picks up where you left off.
async function recentLines(campaign) {
  try {
    return await (await api(`/api/campaigns/${campaign.id}/lines?count=40`)).json();
  } catch (e) {
    toast(`Couldn't load your recent lines: ${e.message}`);
    return [];
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
        } else if (ev.type === "tool") {
          // Narration comes before record-keeping, so speak what's there without waiting.
          if (state.mode === "speech") speaker.flush();
          setActivity(TOOL_VERBS[ev.name] || "working");
        } else if (ev.type === "error") addMessage("error", ev.text);
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

// Party with full character sheets: yours first and expanded, others collapsed.
function renderParty(characters) {
  const party = $("party");
  party.innerHTML = characters.length ? "" : '<li class="meta">No characters yet.</li>';
  const mine = (c) => c.player === state.user;
  const ordered = [...characters].sort((a, b) => mine(b) - mine(a));
  for (const c of ordered) {
    const li = document.createElement("li");
    if (c.status !== "alive") li.className = "down";
    const pct = Math.max(0, Math.min(100, (100 * c.hp) / c.max_hp));
    const who = c.player ? (mine(c) ? "you" : escapeHtml(c.player)) : "NPC";
    const open = state.sheets.has(c.name) ? state.sheets.get(c.name) : mine(c);
    const identity = [c.race, c.class].filter(Boolean).map(escapeHtml).join(" ");
    const attributes = Object.entries(c.attributes || {});
    li.innerHTML = `
      <details ${open ? "open" : ""}>
        <summary>
          <div><span class="name">${escapeHtml(c.name)}</span>
            <span class="meta">${who}${c.status !== "alive" ? " · " + c.status : ""}</span></div>
          <div class="hp"><div style="width:${pct}%"></div></div>
          <div class="meta">${c.hp}/${c.max_hp} HP${c.temp_hp ? ` (+${c.temp_hp} temp)` : ""}${
            c.location ? " · " + escapeHtml(c.location) : ""}</div>
        </summary>
        <div class="sheet">
          <div class="meta">Level ${c.level}${identity ? " " + identity : ""}</div>
          <dl class="stats">
            <div><dt>Defense</dt><dd>${c.defense}</dd></div>
            <div><dt>Gold</dt><dd>${c.gold}</dd></div>
            ${attributes.map(([k, v]) => `<div><dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd></div>`).join("")}
          </dl>
          ${c.conditions.length ? `<div class="conditions">${c.conditions.map((x) => `<span>${escapeHtml(x)}</span>`).join("")}</div>` : ""}
          <div class="inventory-title">Inventory</div>
          <ul class="inventory">${c.inventory.length
            ? c.inventory.map((i) => `<li title="${escapeHtml(i.description || "")}"><span>${escapeHtml(i.name)}</span>${
                i.quantity > 1 ? `<span class="qty">×${i.quantity}</span>` : ""}</li>`).join("")
            : '<li class="meta">Empty</li>'}</ul>
        </div>
      </details>`;
    li.querySelector("details").addEventListener("toggle", (e) => state.sheets.set(c.name, e.target.open));
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
    if (ev.type === "event" && ev.event.type === "campaign_deleted") {
      toast(ev.event.summary);
      showLobby();
      return;
    }
    if (ev.type === "event" && ev.event.type === "campaign_renamed" && state.campaign) {
      state.campaign.name = ev.event.data.new;
      $("campaign-title").textContent = state.campaign.name;
    }
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
  queued: 0,      // chunks fetched or playing
  speaking: false,
  silenced: false, // stopped by the player: the rest of this reply stays quiet
  started: false,  // has this reply's first chunk gone out yet
  loading: new Set(), // audio elements fetched but not finished, aborted on stop
  queue: Promise.resolve(),
  audio: null,
  generation: 0,
  begin() {
    this.pending = "";
    this.silenced = false;
    this.started = false;
  },
  feed(text) {
    if (this.silenced) return;
    this.pending += text;
    const cut = this.lastBoundary(this.pending);
    // Get the first sentence out quickly; after that, longer chunks sound more natural.
    if (cut > (this.started ? 160 : 40)) {
      this.say(this.pending.slice(0, cut));
      this.pending = this.pending.slice(cut);
    }
  },
  flush() {
    if (!this.silenced && this.pending.trim()) this.say(this.pending);
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
    this.started = true;
    const generation = this.generation;
    this.queued++;
    // The element starts downloading (streamed) right away, so later chunks are ready by
    // the time earlier ones finish; each plays once everything before it has.
    const voice = state.voice ? `&voice=${encodeURIComponent(state.voice)}` : "";
    const el = new Audio(`/api/tts?text=${encodeURIComponent(clean)}${voice}`);
    el.preload = "auto";
    this.loading.add(el);
    this.queue = this.queue.then(async () => {
      if (generation === this.generation) await this.play(el);
      if (generation === this.generation && --this.queued === 0) this.setSpeaking(false);
    });
  },
  play(el) {
    return new Promise((resolve) => {
      this.audio = el;
      const done = () => { this.audio = null; this.loading.delete(el); resolve(); };
      el.onended = done;
      el.onerror = () => { toast("Speech failed for part of the reply."); done(); };
      this.setSpeaking(true);
      el.play().catch(done);
    });
  },
  setSpeaking(speaking) {
    if (speaking === this.speaking) return;
    this.speaking = speaking;
    $("stop-voice").hidden = !speaking;
    talk.playback(speaking);
  },
  stop() {
    this.silenced = true;
    this.generation++;
    this.queued = 0;
    this.setSpeaking(false);
    this.pending = "";
    if (this.audio) { this.audio.pause(); this.audio.dispatchEvent(new Event("ended")); }
    // Abort downloads of chunks that will never play (each is a TTS request).
    for (const el of this.loading) { el.removeAttribute("src"); el.load(); }
    this.loading.clear();
    this.queue = Promise.resolve();
  },
};

// ---------- conversation (speech in) -------------------------------------------------

// Resamples the mic to 16 kHz 16-bit mono PCM and posts 80 ms chunks, the format
// Deepgram Flux expects.
const PCM_WORKLET = `
class Pcm16 extends AudioWorkletProcessor {
  constructor() { super(); this.step = sampleRate / 16000; this.pos = 0; this.out = new Int16Array(1280); this.n = 0; }
  process(inputs) {
    const input = inputs[0][0];
    if (!input) return true;
    while (this.pos < input.length) {
      const i = Math.floor(this.pos), frac = this.pos - i;
      const a = input[i], b = i + 1 < input.length ? input[i + 1] : a;
      const v = Math.max(-1, Math.min(1, a + (b - a) * frac));
      this.out[this.n++] = v < 0 ? v * 0x8000 : v * 0x7fff;
      if (this.n === this.out.length) {
        this.port.postMessage(this.out.buffer, [this.out.buffer]);
        this.out = new Int16Array(1280);
        this.n = 0;
      }
      this.pos += this.step;
    }
    this.pos -= input.length;
    return true;
  }
}
registerProcessor("pcm16", Pcm16);`;

// A live, hands-free conversation: the GM answers when you pause, and talking over it
// stops its voice (the server decides when speech counts as an interruption).
const talk = {
  active: false,
  ws: null,
  ctx: null,
  stream: null,
  gm: null,      // the GM message being streamed
  gmText: "",
  async start() {
    if (this.active || !state.campaign) return;
    this.active = true;
    this.setUi("Connecting…");
    try {
      const { ticket } = await (await api("/api/voice/ticket", {
        method: "POST", body: JSON.stringify({ campaign_id: state.campaign.id, campaign: state.campaign.name }),
      })).json();
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      this.ws = new WebSocket(`${scheme}://${location.host}/ws/voice?ticket=${ticket}`);
      this.ws.binaryType = "arraybuffer";
      this.ws.onmessage = (msg) => this.handle(JSON.parse(msg.data));
      this.ws.onclose = () => this.stop();
      await new Promise((resolve, reject) => {
        this.ws.onopen = resolve;
        this.ws.onerror = () => reject(new Error("couldn't open the voice connection"));
      });
      this.ctx = new AudioContext();
      const url = URL.createObjectURL(new Blob([PCM_WORKLET], { type: "application/javascript" }));
      await this.ctx.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);
      const node = new AudioWorkletNode(this.ctx, "pcm16");
      node.port.onmessage = (e) => this.ws?.readyState === WebSocket.OPEN && this.ws.send(e.data);
      const mute = this.ctx.createGain();
      mute.gain.value = 0;  // keeps the worklet pulled by the graph without playing the mic back
      this.ctx.createMediaStreamSource(this.stream).connect(node).connect(mute).connect(this.ctx.destination);
    } catch (e) {
      toast(`Couldn't start the conversation: ${e.message}`);
      this.stop();
    }
  },
  stop() {
    if (!this.active) return;
    this.active = false;
    try { this.ws?.readyState === WebSocket.OPEN && this.ws.send(JSON.stringify({ type: "stop" })); } catch {}
    this.ws?.close();
    this.stream?.getTracks().forEach((t) => t.stop());
    this.ctx?.close();
    this.ws = this.stream = this.ctx = null;
    this.finishReply();
    this.caption("");
    this.setUi(null);
  },
  playback(speaking) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "playback", speaking }));
  },
  handle(ev) {
    if (ev.type === "ready") this.setUi("Listening…");
    else if (ev.type === "heard") this.caption(ev.final ? "" : ev.text);
    else if (ev.type === "barge_in") speaker.stop();
    else if (ev.type === "turn") {
      this.finishReply();
      state.myTurns.add(ev.id);
      if (ev.said) addMessage("player", ev.said, state.user);
      this.gm = addMessage("gm", "");
      this.gm.classList.add("streaming");
      this.gmText = "";
      speaker.begin();
      setActivity("thinking");
    } else if (ev.type === "text" && this.gm) {
      this.gmText += ev.text;
      this.gm.querySelector(".body").innerHTML = renderMarkdown(this.gmText);
      this.gm.scrollIntoView({ block: "end" });
      setActivity("");
      speaker.feed(ev.text);
    } else if (ev.type === "tool") {
      speaker.flush();  // narration comes before record-keeping: speak it now
      setActivity(TOOL_VERBS[ev.name] || "working");
    } else if (ev.type === "error") addMessage("error", ev.text);
    else if (ev.type === "done") {
      speaker.flush();
      this.finishReply();
      refreshState();
    }
  },
  finishReply() {
    if (!this.gm) return;
    this.gm.classList.remove("streaming");
    if (!this.gmText.trim()) this.gm.remove();
    this.gm = null;
    setActivity("");
  },
  caption(text) {
    const el = $("heard");
    el.textContent = text;
    el.hidden = !text;
  },
  setUi(status) {
    $("mic").setAttribute("aria-pressed", String(this.active));
    $("mic-label").textContent = this.active ? "End conversation" : "Start conversation";
    $("talk-status").textContent = status || "";
  },
};

$("mic").addEventListener("click", () => (talk.active ? talk.stop() : talk.start()));

// ---------- GM voice -----------------------------------------------------------------

const VOICE_KEY = "lore.voice";
const PREVIEW = "Welcome, traveler. Pull up a chair; I'll be your Game Master tonight.";

async function loadVoices() {
  const select = $("gm-voice");
  try {
    const { default: fallback, voices } = await (await api("/api/voices")).json();
    let saved = null;
    try { saved = localStorage.getItem(VOICE_KEY); } catch {}
    select.innerHTML = "";
    for (const v of voices) {
      const traits = [v.accent, v.gender, ...v.traits].filter(Boolean).join(", ");
      select.add(new Option(traits ? `${v.name} (${traits})` : v.name, v.id));
    }
    if (![...select.options].some((o) => o.value === fallback)) select.add(new Option(fallback, fallback), 0);
    const pick = [...select.options].some((o) => o.value === saved) ? saved : fallback;
    select.value = pick;
    state.voice = pick;
  } catch (e) {
    $("voice-picker").hidden = true;
  }
}

$("gm-voice").addEventListener("change", (e) => {
  state.voice = e.target.value;
  try { localStorage.setItem(VOICE_KEY, state.voice); } catch {}
  previewVoice();
});

function previewVoice() {
  speaker.stop();
  speaker.begin();
  speaker.say(PREVIEW);
}

$("preview-voice").addEventListener("click", previewVoice);

// Silence the GM's current reply (the conversation, if any, keeps listening).
$("stop-voice").addEventListener("click", () => speaker.stop());
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && speaker.speaking) speaker.stop();
});

// ---------- admin ---------------------------------------------------------------------

const SQL_EXAMPLES = {
  "Campaigns": "SELECT id, name, created_at, left(setting, 80) AS setting FROM campaigns ORDER BY created_at DESC",
  "Characters": "SELECT c.name AS campaign, ch.name, p.username AS player, ch.hp, ch.max_hp, ch.status, ch.location\nFROM characters ch JOIN campaigns c ON c.id = ch.campaign_id LEFT JOIN players p ON p.id = ch.player_id\nORDER BY c.name, ch.name",
  "Recent events": "SELECT e.id, c.name AS campaign, e.actor, e.type, e.summary, e.occurred_at\nFROM events e JOIN campaigns c ON c.id = e.campaign_id ORDER BY e.id DESC LIMIT 50",
  "Lore": "SELECT c.name AS campaign, l.kind, l.title, l.tags, left(l.content, 120) AS content\nFROM lore_entries l JOIN campaigns c ON c.id = l.campaign_id ORDER BY c.name, l.kind, l.title",
  "Nearest lore pairs": "SELECT a.title, b.title AS nearest, round((1 - (a.embedding <=> b.embedding))::numeric, 3) AS similarity\nFROM lore_entries a JOIN LATERAL (\n  SELECT title, embedding FROM lore_entries b\n  WHERE b.campaign_id = a.campaign_id AND b.id <> a.id ORDER BY a.embedding <=> b.embedding LIMIT 1\n) b ON true ORDER BY similarity DESC",
  "Sessions": "SELECT s.id, c.name AS campaign, s.started_at, s.ended_at, s.summary\nFROM game_sessions s JOIN campaigns c ON c.id = s.campaign_id ORDER BY s.started_at DESC",
};

async function showAdmin() {
  closeFeed();
  speaker.stop();
  if (state.campaign) state.beforeAdmin = state.campaign;
  state.campaign = null;
  showView("admin");
  $("campaign-title").textContent = "Admin";
  history.replaceState(null, "", "/?admin");
  try {
    const data = await (await api("/api/admin/overview")).json();
    renderSchema(data.tables);
    renderLoreStats(data.lore);
  } catch (e) {
    toast(`Couldn't load the schema: ${e.message}`);
  }
}

// A toggle: clicking it again goes back to where you were (the world you had open, or the lobby).
$("admin-link").addEventListener("click", () => {
  if ($("admin").hidden) return showAdmin();
  const back = state.beforeAdmin;
  state.beforeAdmin = null;
  return back ? enterCampaign(back, false) : showLobby();
});

const TAB_LOADERS = {
  worlds: () => loadWorldsAdmin(),
  players: () => loadPlayers(),
  tables: () => loadTables(),
  usage: () => loadUsage(),
};
let playersTimer = null;

function openTab(name) {
  document.querySelectorAll(".tabs button").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === name)));
  for (const t of document.querySelectorAll(".tabs button")) $(`tab-${t.dataset.tab}`).hidden = t.dataset.tab !== name;
  clearInterval(playersTimer);
  if (name === "players") playersTimer = setInterval(() => !$("admin").hidden && loadPlayers(), 15000);
  TAB_LOADERS[name]?.();
}

document.querySelectorAll(".tabs button").forEach((tab) => tab.addEventListener("click", () => openTab(tab.dataset.tab)));

for (const [label, sql] of Object.entries(SQL_EXAMPLES)) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = label;
  b.addEventListener("click", () => { $("sql").value = sql; $("sql-form").requestSubmit(); });
  $("sql-examples").append(b);
}

function renderSchema(tables) {
  const list = $("schema");
  list.innerHTML = "";
  for (const t of tables) {
    const li = document.createElement("li");
    li.innerHTML = `<details><summary>${escapeHtml(t.name)} <span>${t.rows ?? "?"} rows</span></summary>${
      t.columns.map((c) => `<code>${escapeHtml(c.name)} · ${escapeHtml(c.type)}</code>`).join("")}</details>`;
    li.querySelector("summary").addEventListener("dblclick", () => {
      $("sql").value = `SELECT * FROM ${t.name} LIMIT 100`;
      $("sql-form").requestSubmit();
    });
    list.append(li);
  }
}

function renderLoreStats(rows) {
  const list = $("lore-stats");
  const select = $("vector-campaign");
  list.innerHTML = rows.length ? "" : '<li class="meta">No lore yet.</li>';
  select.length = 1;
  const byCampaign = new Map();
  for (const r of rows) {
    if (!byCampaign.has(r.campaign_id)) byCampaign.set(r.campaign_id, { name: r.campaign, kinds: [] });
    byCampaign.get(r.campaign_id).kinds.push(`${r.kind} ${r.entries}`);
  }
  for (const [id, c] of byCampaign) {
    const li = document.createElement("li");
    li.innerHTML = `<strong>${escapeHtml(c.name)}</strong><code>${escapeHtml(c.kinds.join(" · "))}</code>`;
    list.append(li);
    select.add(new Option(c.name, id));
  }
}

$("sql").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
    e.preventDefault();
    $("sql-form").requestSubmit();
  }
});

$("allow-writes").addEventListener("change", (e) => {
  if (e.target.checked && !confirm("Allow this console to change data? Writes take effect immediately and can't be undone.")) {
    e.target.checked = false;
  }
});

$("sql-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const out = $("sql-result");
  const button = e.target.querySelector("button[type=submit]");
  button.disabled = true;
  out.innerHTML = '<div class="status">Running…</div>';
  try {
    const r = await (await api("/api/admin/sql", {
      method: "POST", body: JSON.stringify({ sql: $("sql").value, allow_writes: $("allow-writes").checked }),
    })).json();
    if (r.error) {
      out.innerHTML = `<div class="error">${escapeHtml(r.error)}</div>`;
      return;
    }
    let html = `<div class="status">${escapeHtml(r.status || "")}${r.truncated ? " · showing the first 500 rows" : ""}${r.wrote ? " · writes allowed" : ""}</div>`;
    if (r.columns.length) {
      html += '<div class="grid-wrap"><table class="grid"><thead><tr>' +
        r.columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("") + "</tr></thead><tbody>" +
        r.rows.map((row) => "<tr>" + row.map((v) => v === null
          ? '<td class="null">null</td>'
          : `<td>${escapeHtml(typeof v === "object" ? JSON.stringify(v) : String(v))}</td>`).join("") + "</tr>").join("") +
        "</tbody></table></div>";
    }
    out.innerHTML = html;
    if (r.wrote) showAdmin();  // row counts may have changed
  } catch (err) {
    out.innerHTML = `<div class="error">${escapeHtml(err.message)}</div>`;
  } finally {
    button.disabled = false;
  }
});

$("vector-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const out = $("vector-result");
  out.innerHTML = '<li class="meta">Searching…</li>';
  try {
    const hits = await (await api("/api/admin/vector", {
      method: "POST",
      body: JSON.stringify({
        query: $("vector-query").value, campaign_id: $("vector-campaign").value,
        kind: $("vector-kind").value, limit: $("vector-limit").value,
      }),
    })).json();
    out.innerHTML = hits.length ? "" : '<li class="meta">No lore matches those filters.</li>';
    for (const h of hits) {
      const li = document.createElement("li");
      const pct = Math.max(0, Math.min(100, h.similarity * 100));
      li.innerHTML = `
        <div class="head"><span class="title">${escapeHtml(h.title)}</span>
          <span class="meta">${escapeHtml(h.kind)} · ${escapeHtml(h.campaign)}${h.tags.length ? " · " + escapeHtml(h.tags.join(", ")) : ""}</span>
          <span class="score">${h.similarity.toFixed(3)}</span></div>
        <div class="bar"><div style="width:${pct}%"></div></div>
        <p>${escapeHtml(h.content)}</p>`;
      out.append(li);
    }
  } catch (err) {
    out.innerHTML = `<li class="meta">${escapeHtml(err.message)}</li>`;
  }
});

// Admin: worlds and characters ----------------------------------------------------------

const ago = (iso) => {
  if (!iso) return "never";
  const s = (Date.now() - (typeof iso === "number" ? iso * 1000 : Date.parse(iso))) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};

async function adminPost(path, body) {
  const resp = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || data.detail || resp.statusText);
  return data;
}

async function loadWorldsAdmin() {
  const box = $("worlds-admin");
  box.innerHTML = '<p class="hint">Loading…</p>';
  let worlds;
  try { worlds = await (await api("/api/admin/worlds")).json(); } catch (e) { box.innerHTML = `<p class="error">${escapeHtml(e.message)}</p>`; return; }
  box.innerHTML = worlds.length ? "" : '<p class="hint">No worlds yet.</p>';
  for (const w of worlds) {
    const el = document.createElement("div");
    el.className = "world-admin";
    el.innerHTML = `
      <div class="head"><strong>${escapeHtml(w.name)}</strong>
        <span class="meta">${w.characters.length} characters · ${w.lore} lore · ${w.events} events · last activity ${ago(w.last_activity)}</span>
        <button class="small-button" data-act="rename">Rename</button>
        <button class="small-button danger" data-act="delete">Delete…</button></div>
      ${w.characters.length ? `<div class="grid-wrap"><table class="grid"><thead><tr>
        <th>Character</th><th>Player</th><th class="num">Lvl</th><th class="num">HP</th><th class="num">Def</th><th class="num">Gold</th><th>Status</th><th>Location</th><th></th>
      </tr></thead><tbody>${w.characters.map((c) => `<tr>
        <td>${escapeHtml(c.name)}</td><td>${c.player ? escapeHtml(c.player) : '<span class="meta">NPC</span>'}</td>
        <td class="num">${c.level}</td><td class="num">${c.hp}/${c.max_hp}${c.temp_hp ? ` +${c.temp_hp}` : ""}</td>
        <td class="num">${c.defense}</td><td class="num">${c.gold}</td><td>${c.status}</td><td>${escapeHtml(c.location || "")}</td>
        <td><button class="small-button" data-char="${c.id}">Edit</button></td></tr>`).join("")}</tbody></table></div>` : ""}`;
    el.querySelector('[data-act="rename"]').addEventListener("click", async () => {
      const name = prompt(`Rename “${w.name}” to:`, w.name);
      if (!name || name.trim() === w.name) return;
      try { await adminPost(`/api/admin/worlds/${w.id}/rename`, { name: name.trim() }); toast("World renamed."); loadWorldsAdmin(); }
      catch (e) { toast(e.message); }
    });
    el.querySelector('[data-act="delete"]').addEventListener("click", async () => {
      const typed = prompt(`This permanently deletes “${w.name}”: its characters, chronicle, lore and GM conversation.\n\nType the world's name to confirm:`);
      if (typed === null) return;
      try { await adminPost(`/api/admin/worlds/${w.id}/delete`, { confirm: typed }); toast(`Deleted ${w.name}.`); loadWorldsAdmin(); }
      catch (e) { toast(e.message); }
    });
    el.querySelectorAll("[data-char]").forEach((b) => b.addEventListener("click", () =>
      editCharacter(w, w.characters.find((c) => String(c.id) === b.dataset.char))));
    box.append(el);
  }
}

const CHARACTER_NUMBERS = ["level", "hp", "max_hp", "temp_hp", "defense", "gold"];

function editCharacter(world, c) {
  const form = $("character-form");
  $("character-context").textContent = `${world.name} · changes are logged in the world's chronicle`;
  $("character-error").hidden = true;
  for (const f of ["name", "player", "status", ...CHARACTER_NUMBERS]) form[f].value = c[f] ?? "";
  const dialog = $("character-dialog");
  form.onsubmit = async (e) => {
    if (e.submitter?.value !== "save") return;
    e.preventDefault();
    const changes = {};
    for (const f of ["name", "player", "status"]) if ((form[f].value.trim() || null) !== (c[f] || null)) changes[f] = form[f].value.trim();
    for (const f of CHARACTER_NUMBERS) if (Number(form[f].value) !== c[f]) changes[f] = Number(form[f].value);
    if (!Object.keys(changes).length) { dialog.close(); return; }
    try {
      const event = await adminPost(`/api/admin/characters/${c.id}`, changes);
      dialog.close();
      toast(event.summary);
      loadWorldsAdmin();
    } catch (err) {
      $("character-error").textContent = err.message;
      $("character-error").hidden = false;
    }
  };
  dialog.showModal();
}

// Admin: players, tables, usage ---------------------------------------------------------

async function loadPlayers() {
  const table = $("players-table");
  try {
    const people = await (await api("/api/admin/presence")).json();
    table.innerHTML = "<thead><tr><th>Player</th><th>Status</th><th>Where</th><th>Voice</th><th>Last seen</th></tr></thead><tbody>" +
      (people.length ? people.map((p) => `<tr>
        <td><span class="dot ${p.online ? "on" : ""}"></span>${escapeHtml(p.user)}</td>
        <td>${p.online ? "online" : "away"}</td>
        <td>${p.where === "table" ? escapeHtml(p.campaign || "a deleted world") : escapeHtml(p.where || "")}</td>
        <td>${p.voice ? "🎙 in conversation" : ""}</td>
        <td>${ago(p.last_seen)}</td></tr>`).join("") : '<tr><td colspan="5" class="null">Nobody yet.</td></tr>') + "</tbody>";
  } catch (e) {
    table.innerHTML = `<tr><td class="null">${escapeHtml(e.message)}</td></tr>`;
  }
}

async function loadTables() {
  const table = $("tables-table");
  let tables;
  try { tables = await (await api("/api/admin/tables")).json(); } catch (e) { table.innerHTML = `<tr><td>${escapeHtml(e.message)}</td></tr>`; return; }
  table.innerHTML = "<thead><tr><th>World</th><th class=\"num\">Messages</th><th class=\"num\">Size</th><th>Mode</th><th>Last turn</th><th>Turn</th><th></th></tr></thead><tbody>" +
    (tables.length ? tables.map((t) => `<tr>
      <td>${escapeHtml(t.campaign)}</td><td class="num">${t.messages}</td><td class="num">${(t.bytes / 1024).toFixed(0)} KB</td>
      <td>${t.mode || ""}</td><td>${t.idle_seconds === null ? "" : ago(Date.now() / 1000 - t.idle_seconds)}</td>
      <td>${t.turn_running ? `running (lock expires in ${t.lock_expires_in}s)` : "idle"}</td>
      <td>${t.turn_running ? `<button class="small-button" data-unlock="${t.campaign_id}">Clear stuck turn</button>` : ""}
          <button class="small-button danger" data-reset="${t.campaign_id}">Reset conversation</button></td></tr>`).join("")
      : '<tr><td colspan="7" class="null">No GM conversations yet.</td></tr>') + "</tbody>";
  table.querySelectorAll("[data-unlock]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Clear the turn lock? Only do this if the GM is stuck; a turn that is really running will still finish.")) return;
    await adminPost(`/api/admin/tables/${b.dataset.unlock}/unlock`, {}).catch((e) => toast(e.message));
    loadTables();
  }));
  table.querySelectorAll("[data-reset]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Reset this world's GM conversation? The GM forgets the chat; the world, characters and chronicle stay.")) return;
    await adminPost(`/api/admin/tables/${b.dataset.reset}/reset`, {}).catch((e) => toast(e.message));
    loadTables();
  }));
}

const money = (n) => `$${n < 1 ? n.toFixed(3) : n.toFixed(2)}`;
const tokens = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n));

async function loadUsage() {
  const box = $("usage-summary");
  let u;
  try { u = await (await api(`/api/admin/usage?days=${$("usage-days").value}`)).json(); } catch (e) { box.innerHTML = `<p class="error">${escapeHtml(e.message)}</p>`; return; }
  const t = u.totals;
  const models = Object.entries(t.models);
  box.innerHTML = `
    <dl class="usage-totals">
      <div><dt>Estimated LLM cost</dt><dd>${money(t.cost)}</dd></div>
      <div><dt>GM turns</dt><dd>${t.turns}</dd></div>
      <div><dt>Speech characters</dt><dd>${tokens(t.tts_characters)}</dd></div>
      <div><dt>Voice conversation</dt><dd>${Math.round(t.voice_seconds / 60)} min</dd></div>
    </dl>
    ${models.length ? `<div class="grid-wrap"><table class="grid"><thead><tr><th>Model</th><th class="num">Input</th><th class="num">Output</th><th class="num">Cache read</th><th class="num">Cache write</th></tr></thead><tbody>${
      models.map(([m, k]) => `<tr><td>${escapeHtml(m)}</td><td class="num">${tokens(k.input)}</td><td class="num">${tokens(k.output)}</td><td class="num">${tokens(k.cache_read)}</td><td class="num">${tokens(k.cache_write)}</td></tr>`).join("")}</tbody></table></div>` : ""}
    ${u.unpriced_models.length ? `<p class="hint">No price on file for ${u.unpriced_models.map(escapeHtml).join(", ")}; not included in the estimate.</p>` : ""}
    <div class="grid-wrap"><table class="grid"><thead><tr><th>Day</th><th class="num">Est. cost</th><th>Turns by player</th><th class="num">Speech chars</th><th class="num">Voice min</th></tr></thead><tbody>${
      u.days.filter((d) => d.cost || Object.keys(d.turns).length || d.tts_characters || d.voice_seconds).map((d) => `<tr>
        <td>${d.date}</td><td class="num">${money(d.cost)}</td>
        <td>${Object.entries(d.turns).map(([p, n]) => `${escapeHtml(p)} ${n}`).join(", ")}</td>
        <td class="num">${tokens(d.tts_characters)}</td><td class="num">${Math.round(d.voice_seconds / 60)}</td></tr>`).join("") ||
      '<tr><td colspan="5" class="null">No usage recorded in this period.</td></tr>'}</tbody></table></div>`;
}

$("usage-days").addEventListener("change", loadUsage);

// ---------- start ---------------------------------------------------------------------

(async function init() {
  try {
    const me = await (await api("/api/me")).json();
    state.user = me.user;
    state.speechAvailable = me.speech;
    state.admin = me.admin;
    $("user").textContent = me.user;
    $("admin-link").hidden = !me.admin;
  } catch (e) {
    toast(e.message);
  }
  if (state.speechAvailable) loadVoices();
  if (!state.speechAvailable) {
    const b = document.querySelector('.mode button[data-mode="speech"]');
    b.disabled = true;
    b.title = "Speech needs a Deepgram API key on the server.";
  }
  let saved = "speech";
  try { saved = localStorage.getItem(MODE_KEY) || "speech"; } catch {}
  setMode(saved);

  const params = new URLSearchParams(location.search);
  const wanted = Number(params.get("campaign"));
  if (params.has("admin") && state.admin) return showAdmin();
  await showLobby();
  if (wanted) {
    const campaigns = await (await api("/api/campaigns")).json();
    const c = campaigns.find((c) => c.id === wanted);
    if (c) enterCampaign(c, false);
  }
})();
