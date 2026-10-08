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
// Characters' speech arrives as <say who="Name" voice="...">words</say> (see gm.SYSTEM).
const SAY = /<say\b([^>]*)>([\s\S]*?)(?:<\/say>|$)/g;
// A tag still arriving at the end of streamed text: "<", "<sa", "<say who=", "</sa"...
const PARTIAL_TAG = /<\/?(s(a(y[^>]*)?)?)?$/;

function sayAttr(attrs, name) {
  const m = new RegExp(`${name}\\s*=\\s*"([^"]*)"`).exec(attrs);
  return m ? m[1] : "";
}

function inlineMarkdown(html) {
  return html
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
}

// Just enough markdown for narration (paragraphs, line breaks, bold, italics), with
// characters' lines shown under their names.
function renderMarkdown(text) {
  const lines = [];
  text = text.replace(PARTIAL_TAG, "").replace(SAY, (_, attrs, words) => {
    lines.push({ who: sayAttr(attrs, "who"), words: words.trim().replace(/^["“]|["”]$/g, "") });
    return `\u0000${lines.length - 1}\u0000`;
  });
  return text
    .trim()
    .split(/\n{2,}/)
    .map((p) => {
      const html = inlineMarkdown(escapeHtml(p))
        .replace(/^#+\s*/gm, "")
        .replace(/\n/g, "<br>")
        .replace(/\u0000(\d+)\u0000/g, (_, i) => {
          const line = lines[Number(i)];
          return `<span class="npc-line">${line.who ? `<span class="npc-name">${escapeHtml(line.who)}</span>` : ""}` +
            `“${inlineMarkdown(escapeHtml(line.words))}”</span>`;
        });
      return `<p>${html}</p>`;
    })
    .join("");
}

function plainText(text) {
  return text.replace(/<\/?say[^>]*>/g, "").replace(/[*_#`>]/g, "").replace(/\s+/g, " ").trim();
}

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: options.body ? { "Content-Type": "application/json" } : {},
    ...options,
  });
  if (resp.status === 401) {
    location.href = "/login";
    throw new Error("Signed out.");
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch {}
    const err = new Error(detail);
    err.status = resp.status;
    throw err;
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

$("user").addEventListener("click", async () => {
  if (!confirm(`Sign out ${state.user}?`)) return;
  talk.stop();
  await fetch("/auth/logout", { method: "POST" }).catch(() => {});
  location.href = "/login";
});

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
    takeTurn("", { intro: true });  // a quick how-to-play, then the opening scene and starter quest
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

async function takeTurn(message, { intro = false } = {}) {
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
    await retryWhileRestarting(() => postStream(`/api/campaigns/${state.campaign.id}/turn`,
      { campaign: state.campaign.name, message, mode: state.mode, intro },
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
      }));
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

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// A server that's restarting for a deploy refuses new work with 503 before doing any of
// it, so trying again (once traffic has moved to the new one) is safe.
async function retryWhileRestarting(fn, attempts = 4) {
  for (let i = 1; ; i++) {
    try {
      return await fn();
    } catch (err) {
      if (err.status !== 503 || i >= attempts) throw err;
      await sleep(1000 * i);
    }
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
    renderDiceCharacters(data.characters);
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

// ---------- dice -------------------------------------------------------------------

function rollCard(ev) {
  const d = ev.data || {};
  const faces = (d.rolls || []).map((g) => `${g.dice}: ${g.results.join(", ")}`).join(" · ");
  const mod = d.modifier ? ` ${d.modifier > 0 ? "+" : "−"}${Math.abs(d.modifier)}` : "";
  const el = document.createElement("div");
  el.className = "roll-card";
  el.innerHTML = `<span class="total">${d.total ?? "?"}</span>
    <span><strong>${escapeHtml(ev.summary.split(" rolled ")[0])}</strong> rolled ${escapeHtml(d.notation || "")}${
      d.reason ? ` for ${escapeHtml(d.reason)}` : ""}<br><span class="faces">${escapeHtml(faces)}${mod}</span></span>`;
  // The GM rolls before narrating: put the card above the reply that's still streaming.
  const streaming = [...$("transcript").querySelectorAll(".msg.gm.streaming")].pop();
  if (streaming) $("transcript").insertBefore(el, streaming);
  else $("transcript").append(el);
  el.scrollIntoView({ block: "end" });
}

function renderDiceCharacters(characters) {
  const select = $("dice-character");
  const keep = select.value;
  const mine = characters.filter((c) => c.player === state.user);
  select.innerHTML = '<option value="">No character</option>' +
    mine.map((c) => `<option>${escapeHtml(c.name)}</option>`).join("");
  select.value = [...select.options].some((o) => o.value === keep) ? keep : (mine[0]?.name || "");
}

async function rollDice(notation) {
  if (!state.campaign || !notation) return;
  try {
    await adminlessPost(`/api/campaigns/${state.campaign.id}/roll`, {
      campaign: state.campaign.name, notation,
      reason: $("dice-reason").value.trim(), character: $("dice-character").value || null,
    });
    $("dice-reason").value = "";
  } catch (e) {
    toast(e.message);
  }
}

async function adminlessPost(path, body) {
  const resp = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (resp.status === 401) { location.href = "/login"; throw new Error("Signed out."); }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || data.detail || resp.statusText);
  return data;
}

document.querySelectorAll("[data-die]").forEach((b) => b.addEventListener("click", () => rollDice(b.dataset.die)));
$("dice-form").addEventListener("submit", (e) => {
  e.preventDefault();
  rollDice($("dice-notation").value.trim());
});

function eventItem(ev, fresh) {
  const li = document.createElement("li");
  if (fresh) li.className = "fresh";
  li.textContent = ev.summary;
  li.title = `${ev.actor} · ${new Date(ev.occurred_at).toLocaleString()}`;
  return li;
}

// Live updates: game events from anyone, and turns taken by other players. The feed
// resumes where it left off after a reconnect (say, a deploy moving us to a new server):
// the browser sends the last id itself, and `resume` does the same when we reopen it.
function openFeed(resume = false) {
  closeFeed();
  if (!resume) state.feedLast = null;
  const last = state.feedLast ? `?last=${encodeURIComponent(state.feedLast)}` : "";
  const feed = new EventSource(`/api/campaigns/${state.campaign.id}/feed${last}`);
  feed.onmessage = (msg) => {
    if (msg.lastEventId) state.feedLast = msg.lastEventId;
    const ev = JSON.parse(msg.data);
    if (ev.type === "hello") {
      if (ev.resumed) refreshState();  // catch up on anything older than the stream keeps
      return;
    }
    if (ev.type === "event" && ev.event.type === "campaign_deleted") {
      toast(ev.event.summary);
      showLobby();
      return;
    }
    if (ev.type === "event" && ev.event.type === "campaign_renamed" && state.campaign) {
      state.campaign.name = ev.event.data.new;
      $("campaign-title").textContent = state.campaign.name;
    }
    if (ev.type === "event" && ev.event.type === "roll" && !ev.replay) rollCard(ev.event);
    if (ev.type === "event") {
      $("log").prepend(eventItem(ev.event, true));
      clearTimeout(openFeed.refresh);
      openFeed.refresh = setTimeout(refreshState, 800);
    } else if (ev.type === "chat" && !state.myTurns.has(ev.turn)) {
      addMessage("player", ev.message, ev.user);
      addMessage("gm", ev.reply);
      if (state.mode === "speech" && !ev.replay) { speaker.begin(); speaker.feed(ev.reply); speaker.flush(); }
    }
  };
  // EventSource retries dropped streams by itself, but gives up for good on an error
  // response (a proxy's 502 mid-deploy, say), so reopen it.
  feed.onerror = () => {
    if (feed.readyState !== EventSource.CLOSED || state.feed !== feed) return;
    setTimeout(() => state.feed === feed && openFeed(true), 2000);
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
// Splits streamed narration into chunks per speaker: the narrator in the player's chosen
// voice, each <say> character in the voice the world gave them.
const speaker = {
  pending: "",      // text for the current speaker not yet sent to TTS
  raw: "",          // incoming text not yet parsed for <say> tags
  npc: null,        // {who, voice} while inside a <say>, else null (narrator)
  queued: 0,      // chunks fetched or playing
  speaking: false,
  silenced: false, // stopped by the player: the rest of this reply stays quiet
  started: false,  // has this reply's first chunk gone out yet
  loading: new Set(), // audio elements fetched but not finished, aborted on stop
  queue: Promise.resolve(),
  audio: null,
  generation: 0,
  begin() {
    this.pending = this.raw = "";
    this.npc = null;
    this.silenced = false;
    this.started = false;
  },
  feed(text) {
    if (this.silenced) return;
    this.raw += text;
    this.parse();
    const cut = this.lastBoundary(this.pending);
    // Get the first sentence out quickly; after that, longer chunks sound more natural.
    if (cut > (this.started ? 160 : 40)) {
      this.say(this.pending.slice(0, cut), this.npc);
      this.pending = this.pending.slice(cut);
    }
  },
  // Moves parsed text from `raw` into `pending`, sending each finished speaker's part.
  parse() {
    for (;;) {
      const marker = this.npc ? "</say>" : "<say";
      const at = this.raw.indexOf(marker);
      if (at === -1) {
        const partial = PARTIAL_TAG.exec(this.raw);
        const cut = partial ? partial.index : this.raw.length;
        this.pending += this.raw.slice(0, cut);
        this.raw = this.raw.slice(cut);
        return;
      }
      this.pending += this.raw.slice(0, at);
      if (this.npc) {
        this.raw = this.raw.slice(at + marker.length);
        this.endSpeaker(null);
      } else {
        const close = this.raw.indexOf(">", at);
        if (close === -1) { this.raw = this.raw.slice(at); return; }  // tag still arriving
        const attrs = this.raw.slice(at + marker.length, close);
        this.raw = this.raw.slice(close + 1);
        this.endSpeaker({ who: sayAttr(attrs, "who"), voice: sayAttr(attrs, "voice") });
      }
    }
  },
  endSpeaker(next) {
    if (this.pending.trim()) this.say(this.pending, this.npc);
    this.pending = "";
    this.npc = next;
  },
  flush() {
    if (!this.silenced) {
      this.parse();
      this.pending += this.raw.replace(PARTIAL_TAG, "");
      if (this.pending.trim()) this.say(this.pending, this.npc);
    }
    this.pending = this.raw = "";
    this.npc = null;
  },
  lastBoundary(text) {
    let end = -1;
    const re = /[.!?…]["'”’)\]]*\s/g;
    let m;
    while ((m = re.exec(text)) && m.index < 1800) end = m.index + m[0].length;
    return end;
  },
  say(text, npc = null) {
    const clean = plainText(text);
    if (!clean) return;
    this.started = true;
    const generation = this.generation;
    this.queued++;
    // The element starts downloading (streamed) right away, so later chunks are ready by
    // the time earlier ones finish; each plays once everything before it has.
    const params = new URLSearchParams({ text: clean });
    if (state.voice) params.set("voice", state.voice);
    if (npc?.who && state.campaign) {
      params.set("npc", npc.who);
      params.set("npc_voice", npc.voice || "");
      params.set("campaign_id", state.campaign.id);
    }
    const el = new Audio(`/api/tts?${params}`);
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
    this.pending = this.raw = "";
    this.npc = null;
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
      await this.connect();
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
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
  // Opens the socket (a fresh one-time ticket each time); the mic and audio graph carry
  // over, since the worklet always sends to the current this.ws.
  async connect() {
    const { ticket } = await (await retryWhileRestarting(() => api("/api/voice/ticket", {
      method: "POST", body: JSON.stringify({ campaign_id: state.campaign.id, campaign: state.campaign.name }),
    }))).json();
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${location.host}/ws/voice?ticket=${ticket}`);
    ws.binaryType = "arraybuffer";
    ws.onmessage = (msg) => this.handle(JSON.parse(msg.data));
    this.ws = ws;
    await new Promise((resolve, reject) => {
      ws.onopen = () => {
        ws.onclose = (e) => this.dropped(ws, e);
        resolve();
      };
      ws.onclose = () => reject(new Error("couldn't open the voice connection"));
    });
  },
  // The server closes with 1012 when it restarts for a deploy (after any reply in
  // progress), and connections can drop for other reasons too: reconnect quietly.
  async dropped(ws, e) {
    if (!this.active || ws !== this.ws) return;  // we hung up, or already reconnected
    if (e.code === 4401) {
      toast("The voice connection was refused.");
      this.stop();
      return;
    }
    this.finishReply();
    this.caption("");
    this.setUi("Reconnecting…");
    for (let i = 0; i < 5 && this.active; i++) {
      await sleep(i ? 1000 * 2 ** (i - 1) : 250);
      if (!this.active) return;
      try {
        await this.connect();
        return;
      } catch {}
    }
    if (this.active) {
      toast("Lost the voice connection.");
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

const ERROR_KINDS = {
  billing: "billing (out of credit?)", auth: "authentication (API key?)", rate_limit: "rate limits",
  overloaded: "model overloaded", server: "model server errors", connection: "network", other: "other errors",
};

// Model errors in the last day, shown above the admin tabs (and to admins on sign-in).
async function loadHealth() {
  const banner = $("health-banner");
  try {
    const h = await (await api("/api/admin/health")).json();
    const day = Object.entries(h.last_day);
    if (!day.length) { banner.hidden = true; return h; }
    const count = (o) => Object.values(o).reduce((a, b) => a + b, 0);
    const hour = count(h.last_hour);
    banner.className = `health ${hour ? "bad" : "warn"}`;
    banner.innerHTML = `<strong>⚠ ${count(h.last_day)} model request${count(h.last_day) === 1 ? "" : "s"} failed in the last 24 h${
      hour ? ` (${hour} in the last hour)` : ""}:</strong> ${day.map(([k, n]) => `${escapeHtml(ERROR_KINDS[k] || k)} ×${n}`).join(", ")}.
      <br><span class="hint">Latest, ${ago(h.last.at)}: ${escapeHtml(h.last.message)}</span>`;
    banner.hidden = false;
    return h;
  } catch {
    banner.hidden = true;
    return null;
  }
}

async function showAdmin() {
  closeFeed();
  speaker.stop();
  if (state.campaign) state.beforeAdmin = state.campaign;
  state.campaign = null;
  showView("admin");
  $("campaign-title").textContent = "Admin";
  loadHealth();
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

// Search lore by meaning, or (empty query, one world chosen) browse it; edit, merge or
// delete entries from the results.
$("vector-form").addEventListener("submit", (e) => {
  e.preventDefault();
  loadLoreResults();
});

async function loadLoreResults() {
  const out = $("vector-result");
  const query = $("vector-query").value.trim();
  const campaignId = $("vector-campaign").value;
  if (!query && !campaignId) {
    out.innerHTML = '<li class="meta">Type something to search for, or pick a world to browse all of its lore.</li>';
    return;
  }
  out.innerHTML = `<li class="meta">${query ? "Searching…" : "Loading…"}</li>`;
  try {
    const hits = query
      ? await (await api("/api/admin/vector", {
          method: "POST",
          body: JSON.stringify({ query, campaign_id: campaignId, kind: $("vector-kind").value, limit: $("vector-limit").value }),
        })).json()
      : await (await api(`/api/admin/lore?${new URLSearchParams({ campaign_id: campaignId, kind: $("vector-kind").value })}`)).json();
    out.innerHTML = hits.length ? "" : '<li class="meta">No lore matches those filters.</li>';
    for (const h of hits) {
      const li = document.createElement("li");
      const score = h.similarity === undefined ? "" : `<span class="score">${h.similarity.toFixed(3)}</span>`;
      const bar = h.similarity === undefined ? "" :
        `<div class="bar"><div style="width:${Math.max(0, Math.min(100, h.similarity * 100))}%"></div></div>`;
      li.innerHTML = `
        <div class="head"><span class="title">${escapeHtml(h.title)}</span>
          <span class="meta">${escapeHtml(h.kind)} · ${escapeHtml(h.campaign)}${h.tags.length ? " · " + escapeHtml(h.tags.join(", ")) : ""}</span>
          ${score}</div>
        ${bar}
        <p>${escapeHtml(h.content)}</p>
        <div class="lore-actions">
          <button class="small-button" data-act="edit">Edit</button>
          <button class="small-button" data-act="merge">Merge into…</button>
          <button class="small-button danger" data-act="delete">Delete</button>
        </div>`;
      li.querySelector('[data-act="edit"]').addEventListener("click", () => editLore(h));
      li.querySelector('[data-act="merge"]').addEventListener("click", () => mergeLore(h));
      li.querySelector('[data-act="delete"]').addEventListener("click", async () => {
        if (!confirm(`Delete the ${h.kind} “${h.title}”? The Game Master will no longer know it.`)) return;
        try { await adminPost(`/api/admin/lore/${h.id}/delete`, {}); toast("Lore deleted."); loadLoreResults(); }
        catch (err) { toast(err.message); }
      });
      out.append(li);
    }
  } catch (err) {
    out.innerHTML = `<li class="meta">${escapeHtml(err.message)}</li>`;
  }
}

function editLore(entry) {
  const form = $("lore-form");
  $("lore-context").textContent = `${entry.campaign} · saving re-embeds the entry for search`;
  $("lore-error").hidden = true;
  form.kind.value = entry.kind;
  form.title.value = entry.title;
  form.tags.value = entry.tags.join(", ");
  form.content.value = entry.content;
  const dialog = $("lore-dialog");
  form.onsubmit = async (e) => {
    if (e.submitter?.value !== "save") return;
    e.preventDefault();
    try {
      await adminPost(`/api/admin/lore/${entry.id}`, {
        kind: form.kind.value, title: form.title.value, tags: form.tags.value, content: form.content.value,
      });
      dialog.close();
      toast("Lore saved.");
      loadLoreResults();
    } catch (err) {
      $("lore-error").textContent = err.message;
      $("lore-error").hidden = false;
    }
  };
  dialog.showModal();
}

async function mergeLore(entry) {
  const others = (await (await api(`/api/admin/lore?campaign_id=${entry.campaign_id}`)).json()).filter((o) => o.id !== entry.id);
  if (!others.length) { toast("There's nothing else in this world to merge into."); return; }
  const select = $("merge-target");
  select.innerHTML = others.map((o) => `<option value="${o.id}">${escapeHtml(o.kind)}: ${escapeHtml(o.title)}</option>`).join("");
  $("merge-context").textContent = `“${entry.title}” is appended to the entry you pick, its tags are combined, and “${entry.title}” is removed.`;
  const dialog = $("merge-dialog");
  $("merge-form").onsubmit = async (e) => {
    if (e.submitter?.value !== "merge") return;
    e.preventDefault();
    try {
      await adminPost(`/api/admin/lore/${entry.id}/merge`, { into: Number(select.value) });
      dialog.close();
      toast("Lore merged.");
      loadLoreResults();
    } catch (err) { toast(err.message); }
  };
  dialog.showModal();
}

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
        <button class="small-button" data-act="voices">NPC voices</button>
        <button class="small-button" data-act="rename">Rename</button>
        <button class="small-button danger" data-act="delete">Delete…</button></div>
      ${w.characters.length ? `<div class="grid-wrap"><table class="grid"><thead><tr>
        <th>Character</th><th>Player</th><th class="num">Lvl</th><th class="num">HP</th><th class="num">Def</th><th class="num">Gold</th><th>Status</th><th>Location</th><th></th>
      </tr></thead><tbody>${w.characters.map((c) => `<tr>
        <td>${escapeHtml(c.name)}</td><td>${c.player ? escapeHtml(c.player) : '<span class="meta">NPC</span>'}</td>
        <td class="num">${c.level}</td><td class="num">${c.hp}/${c.max_hp}${c.temp_hp ? ` +${c.temp_hp}` : ""}</td>
        <td class="num">${c.defense}</td><td class="num">${c.gold}</td><td>${c.status}</td><td>${escapeHtml(c.location || "")}</td>
        <td><button class="small-button" data-char="${c.id}">Edit</button></td></tr>`).join("")}</tbody></table></div>` : ""}`;
    el.querySelector('[data-act="voices"]').addEventListener("click", () => toggleNpcVoices(el, w));
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

// Which voice each NPC in a world speaks with (assigned the first time they talk).
async function toggleNpcVoices(card, world) {
  const open = card.querySelector(".npc-voices");
  if (open) { open.remove(); return; }
  const box = document.createElement("div");
  box.className = "npc-voices";
  box.innerHTML = '<p class="hint">Loading…</p>';
  card.append(box);
  try {
    const [assigned, catalogue] = await Promise.all([
      (await api(`/api/admin/worlds/${world.id}/npc-voices`)).json(),
      (await api("/api/voices")).json(),
    ]);
    if (!assigned.length) {
      box.innerHTML = '<p class="hint">No characters have spoken yet. Voices are assigned the first time a character speaks.</p>';
      return;
    }
    const options = catalogue.voices.map((v) => `<option value="${v.id}">${escapeHtml(v.name)} (${escapeHtml([v.accent, v.gender].filter(Boolean).join(", "))})</option>`).join("");
    box.innerHTML = '<div class="grid-wrap"><table class="grid"><thead><tr><th>Character</th><th>Voice</th><th></th></tr></thead><tbody>' +
      assigned.map((a) => `<tr><td>${escapeHtml(a.name)}</td><td><select data-npc="${escapeHtml(a.name)}">${options}</select></td>
        <td><button class="small-button" data-hear="${escapeHtml(a.name)}">▶</button></td></tr>`).join("") + "</tbody></table></div>";
    for (const select of box.querySelectorAll("select")) {
      select.value = assigned.find((a) => a.name === select.dataset.npc).voice;
      select.addEventListener("change", async () => {
        try { await adminPost(`/api/admin/worlds/${world.id}/npc-voices`, { name: select.dataset.npc, voice: select.value }); toast(`${select.dataset.npc} now speaks with ${select.selectedOptions[0].text}.`); }
        catch (e) { toast(e.message); }
      });
    }
    for (const b of box.querySelectorAll("[data-hear]")) {
      b.addEventListener("click", () => {
        const voiceId = box.querySelector(`select[data-npc="${CSS.escape(b.dataset.hear)}"]`).value;
        speaker.stop(); speaker.begin();
        const audio = new Audio(`/api/tts?${new URLSearchParams({ text: `I am ${b.dataset.hear}. What brings you here?`, voice: voiceId })}`);
        audio.play().catch(() => {});
      });
    }
  } catch (e) {
    box.innerHTML = `<p class="error">${escapeHtml(e.message)}</p>`;
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
    if (me.admin) {
      loadHealth().then((h) => {
        const n = h ? Object.values(h.last_hour).reduce((a, b) => a + b, 0) : 0;
        if (n) toast(`⚠ ${n} Game Master request${n === 1 ? "" : "s"} failed in the last hour. See Admin.`);
      });
    }
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
