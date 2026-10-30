"use strict";
/*
 * MPC One Push - tablet UI.
 * Everything goes through the bridge (bridge/server.py) to AbletonOSC.
 * State is polled rather than listened to: AbletonOSC's track listeners are
 * bound to track indices and go stale when tracks are inserted or moved.
 */

const $ = (id) => document.getElementById(id);
const F = (x) => ({ f: x });             // force an OSC float
const PROBE_MS = 4000;                   // no replies for this long = disconnected

const S = {
  tracks: [],
  numTracks: -1,
  numScenes: -1,
  scenes: [],
  playing: false,
  tempo: 120,
  metro: false,
  rec: false,
  sel: 0,
  page: "session",
  lastMsg: 0,
  everConnected: false,
  selectionBackoffUntil: 0,
  dev: { track: -1, idx: 0, names: [], pnames: [], pvals: [], pmin: [], pmax: [], pq: [], pstr: [], bank: 0, stale: false },
  br: { path: [] },
  touching: new Set(),                   // controls under a finger: don't overwrite from polls
};

function newTrack() {
  return { name: "", color: 0, armable: false, arm: false, mute: false, solo: false,
           vol: 0.85, volStr: "", pan: 0, playing: -1, fired: -1, clips: [] };
}

// ---------------------------------------------------------------- transport to bridge

let outq = [];
let outTimer = null;
function send(a, ...v) {
  outq.push({ a, v });
  if (!outTimer) outTimer = setTimeout(flush, 8);
}
function flush() {
  const body = JSON.stringify(outq);
  outq = [];
  outTimer = null;
  fetch("/osc", { method: "POST", headers: { "Content-Type": "application/json" }, body }).catch(() => {});
}

// Throttle continuous controls (faders, knobs) to ~30 messages a second.
const throttles = {};
function sendThrottled(key, a, ...v) {
  const t = throttles[key] || (throttles[key] = { last: 0, timer: null, args: null });
  t.args = [a, ...v];
  const now = performance.now();
  if (now - t.last > 33) {
    t.last = now;
    send(...t.args);
  } else if (!t.timer) {
    t.timer = setTimeout(() => { t.timer = null; t.last = performance.now(); send(...t.args); }, 33);
  }
}

// ---------------------------------------------------------------- incoming

const H = {};
// track_data replies all share one address, so remember what each request asked for.
const trackDataQ = [];

function connect() {
  const es = new EventSource("/events");
  es.onmessage = (e) => {
    let m;
    try { m = JSON.parse(e.data); } catch { return; }
    S.lastMsg = Date.now();
    const h = H[m.a];
    if (h) {
      try { h(m.v); } catch (err) { console.error(m.a, err); }
    }
  };
}

// Errors almost always mean our picture of the set is stale (a track or device was
// just deleted, or master/return is selected). Resync at once instead of repeating
// the failing request, because every error costs a traceback in AbletonOSC's log.
let lastErrorResync = 0;
H["/live/error"] = ([msg]) => {
  console.warn("Live:", msg);
  const now = Date.now();
  if (/not in list/.test(msg)) S.selectionBackoffUntil = now + 5000;
  if (now - lastErrorResync < 500) return;
  lastErrorResync = now;
  send("/live/song/get/num_tracks");
  send("/live/song/get/num_scenes");
  if (S.page === "device") {
    S.dev.stale = true;
    send("/live/track/get/devices/name", S.dev.track);
  }
};

H["/live/song/get/is_playing"] = ([v]) => { S.playing = !!v; renderTransport(); };
H["/live/song/get/tempo"] = ([v]) => { if (!S.touching.has("tempo")) { S.tempo = v; renderTransport(); } };
H["/live/song/get/metronome"] = ([v]) => { S.metro = !!v; renderTransport(); };
H["/live/song/get/session_record"] = ([v]) => { S.rec = !!v; renderTransport(); };

H["/live/view/get/selected_track"] = ([t]) => {
  if (t !== S.sel) { S.sel = t; renderSelection(); if (S.page === "device") loadDevices(); }
};
H["/live/view/get/selected_device"] = ([t, d]) => {
  if (t === S.dev.track && d !== S.dev.idx && d >= 0) { S.dev.idx = d; S.dev.bank = 0; loadParams(); }
};

H["/live/song/get/num_tracks"] = ([n]) => {
  if (n === S.numTracks) return;
  S.numTracks = n;
  while (S.tracks.length < n) S.tracks.push(newTrack());
  S.tracks.length = n;
  buildGrid();
  buildMixer();
};

H["/live/song/get/num_scenes"] = ([n]) => {
  if (n !== S.numScenes) { S.numScenes = n; buildGrid(); }
  requestClipData();
};

H["/live/song/get/scenes/name"] = (names) => {
  S.scenes = names;
  document.querySelectorAll(".scene").forEach((el, i) => { el.textContent = names[i] || "Scene " + (i + 1); });
};

const SLOW_PROPS = ["track.name", "track.color", "track.can_be_armed",
                    "clip_slot.has_clip", "clip.name", "clip.color"];
const FAST_PROPS = ["track.playing_slot_index", "track.fired_slot_index", "track.mute", "track.solo"];
const CHUNK = 2;

function requestClipData() {
  dropStaleTrackData();
  for (let a = 0; a < S.numTracks; a += CHUNK) {
    const b = Math.min(a + CHUNK, S.numTracks);
    trackDataQ.push({ kind: "slow", a, b, ns: S.numScenes, at: Date.now() });
    send("/live/song/get/track_data", a, b, ...SLOW_PROPS);
  }
}

function requestFast() {
  if (S.numTracks <= 0) return;
  dropStaleTrackData();
  trackDataQ.push({ kind: "fast", a: 0, b: S.numTracks, at: Date.now() });
  send("/live/song/get/track_data", 0, S.numTracks, ...FAST_PROPS);
}

function dropStaleTrackData() {
  while (trackDataQ.length && Date.now() - trackDataQ[0].at > 2000) trackDataQ.shift();
}

function expectedLen(q) {
  const per = q.kind === "fast" ? FAST_PROPS.length : 3 + 3 * q.ns;
  return (q.b - q.a) * per;
}

H["/live/song/get/track_data"] = (v) => {
  // Find the request this reply answers; anything before it was lost.
  let q;
  while ((q = trackDataQ.shift())) {
    if (expectedLen(q) === v.length) break;
  }
  if (!q) return;
  let i = 0;
  for (let t = q.a; t < q.b; t++) {
    const tr = S.tracks[t];
    if (!tr) { i += expectedLen(q) / (q.b - q.a); continue; }
    if (q.kind === "fast") {
      tr.playing = v[i++]; tr.fired = v[i++]; tr.mute = !!v[i++]; tr.solo = !!v[i++];
      updateTrack(t);
    } else {
      tr.name = v[i++]; tr.color = v[i++] || 0; tr.armable = !!v[i++];
      const has = v.slice(i, i + q.ns); i += q.ns;
      const names = v.slice(i, i + q.ns); i += q.ns;
      const colors = v.slice(i, i + q.ns); i += q.ns;
      tr.clips = has.map((h, s) => ({ has: !!h, name: names[s] || "", color: colors[s] || 0 }));
      updateTrack(t);
      updateClips(t);
    }
  }
};

H["/live/track/get/arm"] = ([t, v]) => { if (S.tracks[t]) { S.tracks[t].arm = !!v; updateTrack(t); updateClips(t); } };
H["/live/track/get/volume_string"] = ([t, v, s]) => {
  const tr = S.tracks[t];
  if (!tr || typeof v !== "number") return;
  if (!S.touching.has("vol" + t)) tr.vol = v;
  tr.volStr = s;
  updateStrip(t);
};
H["/live/track/get/panning"] = ([t, v]) => {
  const tr = S.tracks[t];
  if (tr && !S.touching.has("pan" + t)) { tr.pan = v; updateStrip(t); }
};

// ---------------------------------------------------------------- polling

function visible() { return document.visibilityState === "visible"; }

setInterval(() => {
  if (!visible()) return;
  send("/live/song/get/is_playing");
  send("/live/song/get/tempo");
  send("/live/song/get/metronome");
  send("/live/song/get/session_record");
  requestFast();
}, 250);

setInterval(() => {
  if (!visible() || Date.now() < S.selectionBackoffUntil) return;
  send("/live/view/get/selected_track");
}, 1000);

setInterval(() => {
  if (!visible()) return;
  send("/live/song/get/num_tracks");
  send("/live/song/get/num_scenes");      // its reply triggers requestClipData()
  send("/live/song/get/scenes/name");
  S.tracks.forEach((tr, t) => {
    if (tr.armable) send("/live/track/get/arm", t);
    if (S.page === "mixer") {
      send("/live/track/get/volume_string", t);
      send("/live/track/get/panning", t);
    }
  });
  if (S.page === "device") send("/live/track/get/devices/name", S.sel);
}, 1500);

setInterval(() => {
  if (!visible() || S.page !== "device" || S.dev.track < 0 || !S.dev.names.length || S.dev.stale) return;
  send("/live/device/get/parameters/value", S.dev.track, S.dev.idx);
  if (Date.now() >= S.selectionBackoffUntil) send("/live/view/get/selected_device");
}, 300);

setInterval(() => {
  const ok = Date.now() - S.lastMsg < PROBE_MS;
  $("conn").className = "conn " + (ok ? "ok" : "bad");
  if (ok && !S.everConnected) {
    S.everConnected = true;
    send("/live/api/set/log_level", "warning");   // AbletonOSC logs every poll at "info"
    showPage(S.page);
  }
  if (!ok && S.everConnected) S.everConnected = false;
  if (!ok) showHelp();
}, 1000);

// ---------------------------------------------------------------- helpers

function hex(c) { return "#" + (c >>> 0 & 0xffffff).toString(16).padStart(6, "0"); }
function luminance(c) { return (((c >> 16) & 255) * 0.299 + ((c >> 8) & 255) * 0.587 + (c & 255) * 0.114); }

let toastTimer = null;
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
}

// Tap vs long-press on one element.
function onTap(el, tap, longPress) {
  let timer = null, fired = false, sx = 0, sy = 0;
  el.addEventListener("pointerdown", (e) => {
    fired = false; sx = e.clientX; sy = e.clientY;
    if (longPress) timer = setTimeout(() => { fired = true; longPress(e); }, 500);
  });
  el.addEventListener("pointermove", (e) => {
    if (Math.abs(e.clientX - sx) + Math.abs(e.clientY - sy) > 12) clearTimeout(timer);
  });
  el.addEventListener("pointerup", (e) => {
    clearTimeout(timer);
    if (!fired && Math.abs(e.clientX - sx) + Math.abs(e.clientY - sy) <= 12) tap(e);
  });
  el.addEventListener("pointercancel", () => clearTimeout(timer));
  el.addEventListener("contextmenu", (e) => e.preventDefault());
}

// Drag handler: calls move(dx, dy, e, rect) relative to the press point.
function onDrag(el, key, { start, move, end } = {}) {
  let sx = 0, sy = 0, active = false;
  el.addEventListener("pointerdown", (e) => {
    active = true; sx = e.clientX; sy = e.clientY;
    el.setPointerCapture(e.pointerId);
    S.touching.add(key);
    if (start) start(e, el.getBoundingClientRect());
  });
  el.addEventListener("pointermove", (e) => {
    if (active && move) move(e.clientX - sx, e.clientY - sy, e, el.getBoundingClientRect());
  });
  const stop = (e) => {
    if (!active) return;
    active = false;
    setTimeout(() => S.touching.delete(key), 400);   // let Live catch up before polls win again
    if (end) end(e);
  };
  el.addEventListener("pointerup", stop);
  el.addEventListener("pointercancel", stop);
}

function selectTrack(t) {
  S.sel = t;
  send("/live/view/set/selected_track", t);
  renderSelection();
  if (S.page === "device") loadDevices();
}

// ---------------------------------------------------------------- transport bar

function renderTransport() {
  $("t-play").classList.toggle("on", S.playing);
  $("t-rec").classList.toggle("on", S.rec);
  $("t-metro").classList.toggle("on", S.metro);
  $("t-bpm").textContent = Number(S.tempo).toFixed(2);
}

onTap($("t-play"), () => send(S.playing ? "/live/song/stop_playing" : "/live/song/start_playing"));
onTap($("t-stop"), () => send("/live/song/stop_playing"));
onTap($("t-rec"), () => send("/live/song/set/session_record", S.rec ? 0 : 1));
onTap($("t-metro"), () => send("/live/song/set/metronome", S.metro ? 0 : 1));
onTap($("t-undo"), () => send("/live/song/undo"));
onTap($("t-redo"), () => send("/live/song/redo"));
onTap($("t-stopall"), () => send("/live/song/stop_all_clips"));
onTap($("t-full"), () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen?.().catch(() => {});
});

{
  let base = 120, lastTap = 0;
  onDrag($("t-tempo"), "tempo", {
    start: () => { base = S.tempo; },
    move: (dx, dy) => {
      S.tempo = Math.min(999, Math.max(20, Math.round((base - dy / 4) * 100) / 100));
      renderTransport();
      sendThrottled("tempo", "/live/song/set/tempo", F(S.tempo));
    },
    end: () => {
      const now = Date.now();
      if (now - lastTap < 350) send("/live/song/tap_tempo");
      lastTap = now;
    },
  });
}

function renderSelection() {
  const tr = S.tracks[S.sel];
  const el = $("t-track");
  el.textContent = tr ? (S.sel + 1) + " " + tr.name : "–";
  el.style.borderLeftColor = tr ? hex(tr.color) : "";
  document.querySelectorAll(".gh[data-t]").forEach((h) => h.classList.toggle("sel", +h.dataset.t === S.sel));
  document.querySelectorAll(".strip").forEach((s) => s.classList.toggle("sel", +s.dataset.t === S.sel));
}

// ---------------------------------------------------------------- session grid

function buildGrid() {
  const g = $("grid");
  g.innerHTML = "";
  if (S.numTracks < 0 || S.numScenes < 0) return;
  g.style.gridTemplateColumns = `90px repeat(${S.numTracks}, var(--cell))`;

  const corner = document.createElement("div");
  corner.className = "gh corner";
  corner.textContent = "Scenes";
  g.appendChild(corner);
  for (let t = 0; t < S.numTracks; t++) {
    const h = document.createElement("div");
    h.className = "gh";
    h.dataset.t = t;
    onTap(h, () => selectTrack(t));
    g.appendChild(h);
  }
  for (let s = 0; s < S.numScenes; s++) {
    const sc = document.createElement("button");
    sc.className = "scene";
    sc.textContent = S.scenes[s] || "Scene " + (s + 1);
    onTap(sc, () => send("/live/scene/fire", s));
    g.appendChild(sc);
    for (let t = 0; t < S.numTracks; t++) {
      const c = document.createElement("button");
      c.className = "cell empty";
      c.id = `c${t}_${s}`;
      c.innerHTML = '<span class="nm"></span>';
      onTap(c,
        () => send("/live/clip_slot/fire", t, s),
        () => { selectTrack(t); send("/live/view/set/selected_clip", t, s); toast("Selected clip " + (t + 1) + "/" + (s + 1)); });
      g.appendChild(c);
    }
  }
  const sa = document.createElement("button");
  sa.className = "stopcell corner";
  sa.textContent = "Stop all";
  onTap(sa, () => send("/live/song/stop_all_clips"));
  g.appendChild(sa);
  for (let t = 0; t < S.numTracks; t++) {
    const b = document.createElement("button");
    b.className = "stopcell";
    b.textContent = "■";
    onTap(b, () => send("/live/track/stop_all_clips", t));
    g.appendChild(b);
  }
  for (let t = 0; t < S.numTracks; t++) { updateTrack(t); updateClips(t); }
  renderSelection();
}

function updateTrack(t) {
  const tr = S.tracks[t];
  const h = document.querySelector(`.gh[data-t="${t}"]`);
  if (h) {
    h.textContent = tr.name || "Track " + (t + 1);
    h.style.borderBottomColor = hex(tr.color);
  }
  for (let s = 0; s < S.numScenes; s++) {
    const c = document.getElementById(`c${t}_${s}`);
    if (!c) continue;
    c.classList.toggle("playing", tr.playing === s);
    c.classList.toggle("fired", tr.fired === s && tr.playing !== s);
  }
  if (t === S.sel) renderSelection();
  updateStrip(t);
}

function updateClips(t) {
  const tr = S.tracks[t];
  for (let s = 0; s < S.numScenes; s++) {
    const c = document.getElementById(`c${t}_${s}`);
    if (!c) continue;
    const clip = tr.clips[s];
    const has = clip && clip.has;
    c.classList.toggle("empty", !has);
    c.classList.toggle("armed", !has && tr.arm);
    c.style.background = has ? hex(clip.color || tr.color) : "";
    c.style.color = has ? (luminance(clip.color || tr.color) > 140 ? "#000" : "#fff") : "";
    c.firstChild.textContent = has ? clip.name : "";
  }
}

// ---------------------------------------------------------------- mixer

const ZERO_DB = 0.85;   // Live's volume parameter value for 0 dB

function buildMixer() {
  const m = $("mixer");
  m.innerHTML = "";
  for (let t = 0; t < S.numTracks; t++) {
    const st = document.createElement("div");
    st.className = "strip";
    st.dataset.t = t;
    st.innerHTML = `
      <div class="name"></div>
      <div class="panbar"><div class="fill"></div><div class="mid"></div></div>
      <div class="fader"><div class="fill"></div><div class="zero" style="bottom:${ZERO_DB * 100}%"></div></div>
      <div class="db"></div>
      <div class="row"><button class="mute">M</button><button class="solo">S</button></div>
      <div class="row"><button class="arm">●</button><button class="selb">Sel</button></div>`;
    m.appendChild(st);

    const tr = () => S.tracks[t];
    onTap(st.querySelector(".name"), () => selectTrack(t));
    onTap(st.querySelector(".selb"), () => selectTrack(t));
    onTap(st.querySelector(".mute"), () => { tr().mute = !tr().mute; send("/live/track/set/mute", t, tr().mute ? 1 : 0); updateStrip(t); });
    onTap(st.querySelector(".solo"), () => { tr().solo = !tr().solo; send("/live/track/set/solo", t, tr().solo ? 1 : 0); updateStrip(t); });
    onTap(st.querySelector(".arm"), () => {
      if (!tr().armable) return toast("This track can't be armed");
      tr().arm = !tr().arm; send("/live/track/set/arm", t, tr().arm ? 1 : 0); updateStrip(t); updateClips(t);
    });

    const fader = st.querySelector(".fader");
    let v0 = 0;
    onDrag(fader, "vol" + t, {
      start: () => { v0 = tr().vol; },
      move: (dx, dy, e, r) => {
        // Relative drag: a full fader height = full range, so touching never jumps the level.
        tr().vol = Math.min(1, Math.max(0, v0 - dy / r.height));
        updateStrip(t);
        sendThrottled("vol" + t, "/live/track/set/volume", t, F(tr().vol));
      },
      end: () => send("/live/track/get/volume_string", t),
    });

    const pan = st.querySelector(".panbar");
    let p0 = 0, lastTap = 0;
    onDrag(pan, "pan" + t, {
      start: () => { p0 = tr().pan; },
      move: (dx, dy, e, r) => {
        tr().pan = Math.min(1, Math.max(-1, p0 + (dx / r.width) * 2));
        updateStrip(t);
        sendThrottled("pan" + t, "/live/track/set/panning", t, F(tr().pan));
      },
      end: () => {
        const now = Date.now();
        if (now - lastTap < 350) { tr().pan = 0; send("/live/track/set/panning", t, F(0)); updateStrip(t); }
        lastTap = now;
      },
    });
  }
  for (let t = 0; t < S.numTracks; t++) updateStrip(t);
  renderSelection();
}

function updateStrip(t) {
  const st = document.querySelector(`.strip[data-t="${t}"]`);
  const tr = S.tracks[t];
  if (!st || !tr) return;
  st.style.borderTopColor = hex(tr.color);
  st.querySelector(".name").textContent = tr.name || "Track " + (t + 1);
  st.querySelector(".fader .fill").style.height = (tr.vol * 100) + "%";
  const pf = st.querySelector(".panbar .fill");
  const p = Math.max(-1, Math.min(1, tr.pan));
  pf.style.left = (p < 0 ? 50 + p * 50 : 50) + "%";
  pf.style.width = (Math.abs(p) * 50) + "%";
  st.querySelector(".db").textContent = S.touching.has("vol" + t) ? "" : (tr.volStr || "");
  st.querySelector(".mute").classList.toggle("on", tr.mute);
  st.querySelector(".solo").classList.toggle("on", tr.solo);
  const arm = st.querySelector(".arm");
  arm.classList.toggle("on", tr.arm);
  arm.style.opacity = tr.armable ? 1 : 0.25;
}

// ---------------------------------------------------------------- device

const BANK = 8;

function loadDevices() {
  S.dev.track = S.sel;
  S.dev.names = [];
  S.dev.idx = 0;
  S.dev.bank = 0;
  renderDevice();
  send("/live/track/get/devices/name", S.sel);
  send("/live/view/get/selected_device");
}

H["/live/track/get/devices/name"] = ([t, ...names]) => {
  if (t !== S.dev.track) return;
  S.dev.stale = false;
  const changed = names.join("\u0001") !== S.dev.names.join("\u0001");
  if (!changed) return;
  S.dev.names = names;
  if (S.dev.idx >= names.length) S.dev.idx = 0;
  renderChain();
  if (names.length) loadParams(); else renderDevice();
};

function loadParams() {
  const d = S.dev;
  d.pnames = []; d.pvals = []; d.pmin = []; d.pmax = []; d.pq = []; d.pstr = [];
  renderChain();
  renderDevice();
  for (const what of ["name", "value", "min", "max", "is_quantized"]) {
    send("/live/device/get/parameters/" + what, d.track, d.idx);
  }
}

function paramsReply(key) {
  return ([t, d, ...vals]) => {
    if (t !== S.dev.track || d !== S.dev.idx) return;
    if (key === "pvals") {
      const shown = bankIndices();
      vals.forEach((v, p) => {
        if (S.touching.has("p" + p)) return;
        // Value text ("1.26 kHz") is only re-fetched for visible knobs that moved.
        if (S.dev.pvals[p] !== v && shown.includes(p)) send("/live/device/get/parameter/value_string", t, d, p);
        S.dev.pvals[p] = v;
      });
      updateParams();
      return;
    }
    S.dev[key] = vals;
    renderDevice();
  };
}
H["/live/device/get/parameters/name"] = paramsReply("pnames");
H["/live/device/get/parameters/value"] = paramsReply("pvals");
H["/live/device/get/parameters/min"] = paramsReply("pmin");
H["/live/device/get/parameters/max"] = paramsReply("pmax");
H["/live/device/get/parameters/is_quantized"] = paramsReply("pq");
H["/live/device/get/parameter/value_string"] = ([t, d, p, s]) => {
  if (t !== S.dev.track || d !== S.dev.idx) return;
  S.dev.pstr[p] = s;
  updateParams();
};

// Parameter 0 is "Device On"; the rest are paged in banks of 8.
function bankIndices() {
  const n = Math.max(0, S.dev.pnames.length - 1);
  const out = [];
  for (let i = 0; i < BANK; i++) {
    const p = 1 + S.dev.bank * BANK + i;
    if (p <= n) out.push(p);
  }
  return out;
}

function renderChain() {
  const c = $("dev-chain");
  c.innerHTML = "";
  S.dev.names.forEach((name, i) => {
    const b = document.createElement("button");
    b.className = "pill" + (i === S.dev.idx ? " sel" : "");
    b.textContent = name;
    onTap(b, () => {
      S.dev.idx = i; S.dev.bank = 0;
      send("/live/view/set/selected_device", S.dev.track, i);
      loadParams();
    });
    c.appendChild(b);
  });
}

function renderDevice() {
  const d = S.dev;
  const tr = S.tracks[d.track];
  $("dev-name").textContent = d.names.length ? d.names[d.idx] : (tr ? tr.name + ": no devices" : "No device");
  const banks = Math.max(1, Math.ceil((d.pnames.length - 1) / BANK));
  if (d.bank >= banks) d.bank = banks - 1;
  $("dev-bank").textContent = (d.bank + 1) + "/" + banks;

  const box = $("params");
  box.innerHTML = "";
  const idx = bankIndices();
  for (let i = 0; i < BANK; i++) {
    const p = idx[i];
    const el = document.createElement("div");
    el.className = "param" + (p === undefined ? " empty" : "");
    el.dataset.p = p ?? "";
    el.innerHTML = `<div class="pn"></div>
      <svg viewBox="0 0 100 100"><path class="bg" d="" fill="none" stroke="#2f333a" stroke-width="10" stroke-linecap="round"/>
      <path class="fg" d="" fill="none" stroke="#ff7a1a" stroke-width="10" stroke-linecap="round"/></svg>
      <div class="pv"></div>`;
    box.appendChild(el);
    if (p === undefined) continue;
    el.querySelector(".pn").textContent = d.pnames[p] || "";
    el.querySelector(".bg").setAttribute("d", arc(0, 1));
    let v0 = 0;
    onDrag(el, "p" + p, {
      start: () => { v0 = d.pvals[p] ?? 0; el.classList.add("touch"); },
      move: (dx, dy) => {
        const lo = d.pmin[p] ?? 0, hi = d.pmax[p] ?? 1;
        let v;
        if (d.pq[p]) v = Math.round(v0 - dy / 24);                // one step per 24px
        else v = v0 - (dy / 250) * (hi - lo);                     // 250px = full range
        v = Math.min(hi, Math.max(lo, v));
        d.pvals[p] = v;
        updateParams();
        sendThrottled("p" + p, "/live/device/set/parameter/value", d.track, d.idx, p, d.pq[p] ? v : F(v));
        sendThrottled("ps" + p, "/live/device/get/parameter/value_string", d.track, d.idx, p);
      },
      end: () => { el.classList.remove("touch"); send("/live/device/get/parameter/value_string", d.track, d.idx, p); },
    });
  }
  updateParams();
}

function arc(from, to) {
  // 270-degree knob arc, from/to in 0..1
  const a0 = (135 + from * 270) * Math.PI / 180;
  const a1 = (135 + to * 270) * Math.PI / 180;
  const r = 40, cx = 50, cy = 50;
  const x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0);
  const x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
  const large = (to - from) * 270 > 180 ? 1 : 0;
  if (to - from < 0.002) return `M ${x0} ${y0}`;
  return `M ${x0} ${y0} A ${r} ${r} 0 ${large} 1 ${x1} ${y1}`;
}

function updateParams() {
  const d = S.dev;
  $("dev-on").classList.toggle("on", !!d.pvals[0]);
  document.querySelectorAll(".param[data-p]").forEach((el) => {
    if (el.dataset.p === "") return;
    const p = +el.dataset.p;
    const lo = d.pmin[p] ?? 0, hi = d.pmax[p] ?? 1, v = d.pvals[p] ?? lo;
    const norm = hi > lo ? (v - lo) / (hi - lo) : 0;
    el.querySelector(".fg").setAttribute("d", arc(0, Math.min(1, Math.max(0, norm))));
    el.querySelector(".pv").textContent = d.pstr[p] ?? (Math.round(v * 100) / 100);
  });
}

onTap($("dev-on"), () => {
  const d = S.dev;
  if (!d.names.length) return;
  d.pvals[0] = d.pvals[0] ? 0 : 1;
  send("/live/device/set/parameter/value", d.track, d.idx, 0, d.pvals[0]);
  updateParams();
});
function requestBankStrings() {
  bankIndices().forEach((p) => send("/live/device/get/parameter/value_string", S.dev.track, S.dev.idx, p));
}
onTap($("dev-prev"), () => { if (S.dev.bank > 0) { S.dev.bank--; renderDevice(); requestBankStrings(); } });
onTap($("dev-next"), () => {
  const banks = Math.ceil((S.dev.pnames.length - 1) / BANK);
  if (S.dev.bank < banks - 1) { S.dev.bank++; renderDevice(); requestBankStrings(); }
});

// ---------------------------------------------------------------- browser

function browse() {
  const p = S.br.path;
  $("br-path").textContent = p.length ? [ROOT_LABELS[p[0]] || p[0].replace(/^place:/, ""), ...p.slice(1)].join(" › ") : "Browser";
  $("br-list").innerHTML = '<div class="br-hint">Loading…</div>';
  if (!p.length) send("/live/browser/list_roots");
  else send("/live/browser/list_children", ...p);
}

const ROOT_LABELS = { plugins: "Plug-ins", audio_effects: "Audio Effects", instruments: "Instruments",
  user_library: "User Library", midi_effects: "MIDI Effects", drums: "Drums",
  max_for_live: "Max for Live", sounds: "Sounds", packs: "Packs", current_project: "Current Project",
  samples: "Samples", clips: "Clips" };

function renderBrowser(items, isRoot) {
  const list = $("br-list");
  list.innerHTML = "";
  if (!items.length) list.innerHTML = '<div class="br-hint">Empty</div>';
  items.forEach((raw) => {
    const folder = isRoot || raw.startsWith("[F] ");
    const name = isRoot ? raw : raw.replace(/^\[F\] /, "");
    const b = document.createElement("button");
    b.className = "br-item" + (folder ? " folder" : "");
    b.textContent = isRoot ? (ROOT_LABELS[raw] || raw.replace(/^place:/, "")) : name;
    onTap(b, () => {
      if (folder) { S.br.path.push(name); browse(); return; }
      const tr = S.tracks[S.sel];
      toast("Loading " + name + " on " + (tr ? tr.name : "track " + (S.sel + 1)) + "…");
      send("/live/browser/load_path", S.sel, ...S.br.path, name);   // exact item, works for Places too
    });
    list.appendChild(b);
  });
}

H["/live/browser/list_roots"] = (items) => { if (!S.br.path.length) renderBrowser(items, true); };
H["/live/browser/list_children"] = (items) => { if (S.br.path.length) renderBrowser(items, false); };
H["/live/browser/load_device"] = ([t, name, status]) => {
  toast(status === "ok" ? "Loaded " + name : "Couldn't load " + name + " (" + status + ")");
  if (S.page === "device") loadDevices();
};

onTap($("br-up"), () => { S.br.path.pop(); browse(); });

// ---------------------------------------------------------------- pages

function showPage(name) {
  S.page = name;
  document.querySelectorAll(".page").forEach((p) => p.classList.toggle("active", p.id === "page-" + name));
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.page === name));
  if (name === "device") loadDevices();
  if (name === "browser") browse();
  if (name === "mixer") S.tracks.forEach((_, t) => { send("/live/track/get/volume_string", t); send("/live/track/get/panning", t); });
}

function showHelp() {
  document.querySelectorAll(".page").forEach((p) => p.classList.toggle("active", p.id === "page-help"));
}

document.querySelectorAll(".tab").forEach((b) => onTap(b, () => {
  if (Date.now() - S.lastMsg >= PROBE_MS) { showHelp(); return; }
  showPage(b.dataset.page);
}));

// Keep the screen awake on tablets that support it.
async function keepAwake() {
  try { await navigator.wakeLock?.request("screen"); } catch { /* not supported or not allowed */ }
}
document.addEventListener("visibilitychange", () => { if (visible()) keepAwake(); });

renderTransport();
connect();
keepAwake();
