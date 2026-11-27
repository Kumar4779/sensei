/*
 * Talk to Sensei (Demo Slices 1–2, restyled). Loaded by the public shell with X-Sensei-Token.
 * Every request carries the token from sessionStorage. Every string from the
 * server is set with textContent, never as HTML; icons are built with DOM calls.
 *
 * LIVE SPEECH: hold the orb (or the space bar) to talk; the recording is POSTed
 * to the server, which runs Deepgram → the voice loop → ElevenLabs and returns
 * the reply's audio, played here and then dropped.
 * SIMULATED: typed input; no audio anywhere. The badge says which, always.
 *
 * The ledger is Server-Sent Events read through fetch (so the token stays in a
 * header): the session's trace, as the server writes it.
 */
(function () {
  "use strict";
  var KEY = "sensei.demo.token";
  var SVGNS = "http://www.w3.org/2000/svg";
  var root = document.getElementById("app");
  var state = { status: null, sid: null, scenario: null, stream: null, recorder: null, chunks: [], busy: false, holding: false,
                mic: null, micTimer: null, audio: null };
  var MIC_IDLE_MS = 30000;  // keep the microphone open between presses, so a press records at once; release when idle

  // Icon paths (24×24, stroked). Static constants, drawn with DOM calls.
  var ICONS = {
    sun: ["M12 4v2", "M12 18v2", "M4 12h2", "M18 12h2", "M6.3 6.3l1.4 1.4", "M16.3 16.3l1.4 1.4", "M6.3 17.7l1.4-1.4", "M16.3 7.7l1.4-1.4", "M12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6"],
    send: ["M4 12l16-8-6 16-2.5-6.5z", "M11.5 13.5L20 4"],
    card: ["M3 7h18v10H3z", "M3 10h18", "M7 14h4"],
    gate: ["M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"],
    proposed: ["M12 3v3", "M5 8l2 2", "M19 8l-2 2", "M8 15a4 4 0 1 1 8 0c0 1.5-1 2.5-1.5 3h-5c-.5-.5-1.5-1.5-1.5-3z", "M10 21h4"],
    echoed: ["M4 5h16v10H9l-5 4z", "M8 9h8", "M8 12h5"],
    approved: ["M5 12.5l4.5 4.5L19 7.5"],
    denied: ["M6 6l12 12", "M18 6L6 18"],
    executed: ["M13 3L5 14h6l-1 7 8-11h-6z"],
    verified: ["M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z", "M8.5 12l2.5 2.5 4.5-5"],
    violation: ["M12 4l9 16H3z", "M12 10v4", "M12 17v.5"],
    error: ["M12 4l9 16H3z", "M12 10v4", "M12 17v.5"],
    turn: ["M12 4a3 3 0 0 1 3 3v5a3 3 0 0 1-6 0V7a3 3 0 0 1 3-3z", "M6 11a6 6 0 0 0 12 0", "M12 17v3"],
    route: ["M4 6h10a4 4 0 0 1 0 8H8", "M11 11l-3 3 3 3", "M17 18h3"],
    model: ["M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z", "M18 15l.8 2.2L21 18l-2.2.8L18 21l-.8-2.2L15 18l2.2-.8z"],
    minor: ["M12 12h.01"]
  };
  var CARD_ICONS = { brief: "sun", note: "send", pay: "card" };

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined && text !== null) { e.textContent = String(text); }
    return e;
  }

  function icon(name) {
    var svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    (ICONS[name] || ICONS.minor).forEach(function (d) {
      var p = document.createElementNS(SVGNS, "path");
      p.setAttribute("d", d);
      svg.appendChild(p);
    });
    return svg;
  }

  function api(method, path, body, contentType) {
    var headers = { "X-Sensei-Token": sessionStorage.getItem(KEY) || "" };
    var opts = { method: method, cache: "no-store", headers: headers };
    if (body !== undefined) {
      if (contentType) { headers["Content-Type"] = contentType; opts.body = body; }
      else { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    }
    return fetch(path, opts).then(function (r) {
      if (r.status === 401) { sessionStorage.removeItem(KEY); location.reload(); throw new Error("unauthorized"); }
      return r.json().then(function (d) { if (!r.ok) { throw new Error(d.error || ("HTTP " + r.status)); } return d; });
    });
  }

  // ---------- layout ----------
  var header = el("header", "top");
  var brand = el("div", "brand");
  brand.appendChild(el("span", "mark"));
  brand.appendChild(el("span", null, "Sensei"));
  brand.appendChild(el("span", "tagline", "asks before it acts"));
  var modeBox = el("div", "mode");
  var badge = el("div", "badge", "…");
  badge.tabIndex = 0;
  var badgeDetail = el("p", "badge-detail");
  modeBox.appendChild(badge);
  modeBox.appendChild(badgeDetail);
  header.appendChild(brand);
  header.appendChild(modeBox);

  // The top of the page: copy and scenario cards on the left, Sensei standing full-length on the right.
  var top = el("div", "top-grid");
  var hero = el("section", "hero");
  var h1 = el("h1");
  h1.appendChild(document.createTextNode("Talk to Sensei. "));
  h1.appendChild(el("span", "grad", "Watch it ask first."));
  hero.appendChild(h1);
  hero.appendChild(el("p", null, "Speak a request. Anything irreversible is read back to you and waits for your yes, and every permission decision streams into the ledger as it happens."));
  // Sensei, head to shoes. Fetched with the token like the rest of the app, then shown from a blob URL.
  // The frame has the image's own aspect ratio and a fixed height, so nothing is cropped and nothing shifts on load.
  var figure = el("figure", "hero-figure");
  var avatarFrame = el("span", "avatar");
  var avatar = el("img");
  avatar.alt = "Sensei";
  avatar.decoding = "async";
  avatarFrame.appendChild(avatar);
  figure.appendChild(avatarFrame);
  fetch("/demo/app/sensei-avatar.webp", { headers: { "X-Sensei-Token": sessionStorage.getItem(KEY) || "" }, cache: "no-store" })
    .then(function (r) { if (!r.ok) { throw new Error("avatar HTTP " + r.status); } return r.blob(); })
    .then(function (blob) { avatar.src = URL.createObjectURL(blob); avatarFrame.classList.add("loaded"); })
    .catch(function () { avatarFrame.classList.add("missing"); });  // decorative fallback: the gradient frame stays

  var cards = el("section", "cards");
  var main = el("main", "grid");

  var convo = el("section", "panel convo");
  var convoHead = el("div", "panel-head");
  var convoTitle = el("div");
  convoTitle.appendChild(el("h2", null, "Conversation"));
  var scenarioLabel = el("p", "sub", "");
  convoTitle.appendChild(scenarioLabel);
  convoHead.appendChild(convoTitle);
  convo.appendChild(convoHead);
  var log = el("div", "log");
  log.setAttribute("aria-live", "polite");
  var dock = el("div", "dock");
  var hint = el("p", "hint");
  var controls = el("div", "controls");
  var errBox = el("p", "err");
  errBox.setAttribute("role", "alert");
  dock.appendChild(hint);
  dock.appendChild(controls);
  dock.appendChild(errBox);
  convo.appendChild(log);
  convo.appendChild(dock);

  var ledgerPanel = el("aside", "panel ledger");
  var ledgerHead = el("div", "panel-head");
  ledgerHead.appendChild(el("h2", null, "Permission ledger"));
  var liveDot = el("span", "live-dot", "idle");
  ledgerHead.appendChild(liveDot);
  ledgerPanel.appendChild(ledgerHead);
  ledgerPanel.appendChild(el("p", "sub", "Straight from this session's trace: every gate decision, read-back, approval, decline and execution, in order."));
  var steps = el("ol", "steps");
  ledgerPanel.appendChild(steps);

  main.appendChild(convo);
  main.appendChild(ledgerPanel);

  var foot = el("footer", "foot");
  var demoData = el("p", "demo-data");
  var spendBox = el("div", "spend-box");
  var spend = el("p", "spend");
  var meter = el("div", "meter");
  var meterFill = el("i");
  meter.appendChild(meterFill);
  spendBox.appendChild(spend);
  spendBox.appendChild(meter);
  foot.appendChild(demoData);
  foot.appendChild(spendBox);

  top.appendChild(hero);
  top.appendChild(figure);
  top.appendChild(cards);
  root.appendChild(header);
  root.appendChild(top);
  root.appendChild(main);
  root.appendChild(foot);

  function showError(message) { errBox.textContent = message || ""; }

  function emptyLog() {
    log.textContent = "";
    log.appendChild(el("p", "empty", state.status && state.status.mode === "live"
      ? "Hold the orb and say the line on the card, or ask Sensei anything. It answers out loud."
      : "Type the line on the card and press Send."));
  }

  function emptyLedger() {
    steps.textContent = "";
    steps.appendChild(el("li", "empty", "Nothing yet. Each gate decision appears here the moment it's made."));
  }

  // ---------- conversation ----------
  function bubble(who, text, meta) {
    var empty = log.querySelector(".empty");
    if (empty) { empty.remove(); }
    var b = el("div", "bubble " + who);
    b.appendChild(el("span", "who", who === "you" ? "You (heard)" : "Sensei"));
    b.appendChild(el("p", "text", text));
    var m = el("div", "meta");
    (meta || []).forEach(function (chip) {
      m.appendChild(el("span", "chip" + (chip.cls ? " " + chip.cls : ""), chip.text || chip));
    });
    b.appendChild(m);
    log.appendChild(b);
    log.scrollTop = log.scrollHeight;
    return b;
  }

  function ms(v) { return v === null || v === undefined ? "–" : Math.round(v) + " ms"; }

  // ---------- controls: the orb (live) or a composer (simulated) ----------
  var talk = el("button", "talk");
  talk.type = "button";
  talk.setAttribute("aria-label", "Hold to talk");
  var talkLabel = el("span", "label", "Hold to talk");
  talk.appendChild(talkLabel);
  var textInput = el("input", "typed");
  var sendBtn = el("button", "send", "Send");
  sendBtn.type = "button";

  function setOrb(mode) {
    talk.classList.remove("busy", "speaking", "recording");
    if (mode) { talk.classList.add(mode); }
    var labels = { recording: "Listening…", busy: "Thinking", speaking: "Speaking" };
    talkLabel.textContent = labels[mode] || "Hold to talk";
  }

  function stopReply() {
    if (state.audio) { state.audio.pause(); state.audio = null; }
  }

  function play(base64, type, onPlaying) {
    stopReply();
    var bytes = Uint8Array.from(atob(base64), function (c) { return c.charCodeAt(0); });
    var url = URL.createObjectURL(new Blob([bytes], { type: type }));
    var audio = new Audio(url);
    state.audio = audio;
    function done() { URL.revokeObjectURL(url); if (state.audio === audio) { state.audio = null; } if (!state.recorder && !state.busy) { setOrb(null); } }
    audio.addEventListener("playing", function () { if (state.audio === audio && !state.recorder) { setOrb("speaking"); } onPlaying(); }, { once: true });
    audio.addEventListener("ended", done, { once: true });
    audio.addEventListener("pause", done, { once: true });
    return audio.play();
  }

  function handleTurn(d, started) {
    var heardMeta = [];
    if (d.confidence !== null && d.confidence !== undefined) { heardMeta.push("confidence " + Number(d.confidence).toFixed(2)); }
    if (d.real_path) { heardMeta.push("STT " + ms(d.real_path.stt_latency_ms) + " (trace)"); }
    bubble("you", d.heard || "(nothing heard)", heardMeta);
    var meta = [{ text: "outcome: " + d.outcome }, { text: "hard gate " + d.hard_gate, cls: d.hard_gate === "PASS" ? "good" : "bad" }];
    if (d.model && d.outcome === "answered") {
      meta.push({ text: "answered by " + d.model.model + " · ≈$" + Number(d.model.est_cost_usd).toFixed(5) + " · read-only", cls: "model" });
    }
    if (d.real_path) {
      meta.push("time to first audio " + ms(d.real_path.time_to_first_audio_ms) + " (trace, server)");
      meta.push("TTS first byte " + ms(d.real_path.tts_first_byte_ms) + " (trace)");
    }
    var b = bubble("sensei", d.reply, meta);
    if (d.error) { showError("The reply couldn't be spoken: " + d.error); }
    if (d.audio) {
      play(d.audio, d.audio_type, function () {
        b.querySelector(".meta").appendChild(el("span", "chip browser", "audio started in page " + ms(performance.now() - started) + " after you stopped"));
      }).catch(function () { setOrb(null); showError("The browser blocked playback; click the page and try again."); });
    } else { setOrb(null); }
    if (d.spend) { showSpend(d.spend); }
  }

  function showSpend(sp) {
    spend.textContent = "Estimated provider spend today: $" + sp.today_usd.toFixed(4) + " of the $" +
      sp.daily_cap_usd.toFixed(2) + " daily cap (resets 00:00 UTC) · this server run: $" + sp.estimated_usd.toFixed(4) +
      " of $" + sp.cap_usd.toFixed(2);
    meterFill.style.width = Math.min(100, 100 * sp.today_usd / sp.daily_cap_usd).toFixed(1) + "%";
  }

  function setBusy(on) {
    sendBtn.disabled = on;
    if (state.status && state.status.mode === "live") {
      talk.disabled = on && !state.recorder;
      if (on && !state.recorder) { setOrb("busy"); }
    }
  }

  function send(body, contentType) {
    if (!state.sid || state.busy) { return; }
    state.busy = true;
    setBusy(true);
    showError("");
    var started = performance.now();
    api("POST", "/api/sessions/" + state.sid + "/turn", body, contentType).then(function (d) {
      state.busy = false;
      setBusy(false);
      handleTurn(d, started);
    }).catch(function (e) { state.busy = false; setBusy(false); setOrb(null); showError(e.message); });
  }

  function micLive() {
    return state.mic && state.mic.getAudioTracks().some(function (t) { return t.readyState === "live"; });
  }

  function getMic() {
    clearTimeout(state.micTimer);
    if (micLive()) { return Promise.resolve(state.mic); }
    return navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
      .then(function (stream) { state.mic = stream; return stream; });
  }

  function releaseMicLater() {
    clearTimeout(state.micTimer);
    state.micTimer = setTimeout(function () {
      if (state.mic && !state.recorder) { state.mic.getTracks().forEach(function (t) { t.stop(); }); state.mic = null; }
    }, MIC_IDLE_MS);
  }

  function startRecording() {
    if (state.busy || state.recorder) { return; }
    state.holding = true;
    stopReply();  // never record Sensei's own reply into your turn
    getMic().then(function (stream) {
      var type = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" :
        (MediaRecorder.isTypeSupported("audio/mp4") ? "audio/mp4" : "");
      var rec = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
      state.chunks = [];
      rec.addEventListener("dataavailable", function (e) { if (e.data.size) { state.chunks.push(e.data); } });
      rec.addEventListener("stop", function () {
        releaseMicLater();
        var blob = new Blob(state.chunks, { type: rec.mimeType || "audio/webm" });
        state.chunks = [];
        state.recorder = null;
        setOrb(null);
        if (blob.size) { send(blob, (rec.mimeType || "audio/webm").split(";")[0]); } else { setBusy(false); }
      });
      state.recorder = rec;
      rec.start();
      setOrb("recording");
      if (!state.holding) { rec.stop(); }  // released before the microphone was ready
    }).catch(function (e) { state.holding = false; setOrb(null); showError("Microphone unavailable: " + e.message); });
  }

  function stopRecording() { state.holding = false; if (state.recorder && state.recorder.state === "recording") { state.recorder.stop(); } }

  function buildControls(status) {
    controls.textContent = "";
    if (status.mode === "live") {
      // Pointer capture: once pressed, the orb gets the release even if the pointer drifts off it.
      talk.addEventListener("pointerdown", function (e) {
        if (e.button !== 0) { return; }
        e.preventDefault();
        try { talk.setPointerCapture(e.pointerId); } catch (x) { /* not supported: pointerup still ends it */ }
        startRecording();
      });
      talk.addEventListener("pointerup", stopRecording);
      talk.addEventListener("pointercancel", stopRecording);
      talk.addEventListener("lostpointercapture", stopRecording);
      talk.addEventListener("contextmenu", function (e) { e.preventDefault(); });  // long-press on touch
      window.addEventListener("blur", stopRecording);  // switching away ends a held turn
      document.addEventListener("keydown", function (e) { if (e.code === "Space" && !e.repeat && e.target === document.body) { e.preventDefault(); startRecording(); } });
      document.addEventListener("keyup", function (e) { if (e.code === "Space" && e.target === document.body) { e.preventDefault(); stopRecording(); } });
      controls.appendChild(talk);
      dock.insertBefore(el("span", "keyhint", "Hold the orb, or hold the space bar"), errBox);
    } else {
      textInput.placeholder = "Type what you would say…";
      textInput.setAttribute("aria-label", "Typed utterance");
      textInput.maxLength = 500;
      textInput.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); sendTyped(); } });
      sendBtn.addEventListener("click", sendTyped);
      controls.appendChild(textInput);
      controls.appendChild(sendBtn);
    }
  }

  function sendTyped() {
    var text = textInput.value.trim();
    if (!text) { return; }
    textInput.value = "";
    send({ text: text });
  }

  // ---------- scenarios ----------
  function buildCards(status) {
    cards.textContent = "";
    status.scenarios.forEach(function (s) {
      var c = el("button", "card");
      c.type = "button";
      c.dataset.id = s.id;
      var ic = el("span", "icon");
      ic.appendChild(icon(CARD_ICONS[s.id] || "minor"));
      c.appendChild(ic);
      c.appendChild(el("span", "kind", s.kind));
      c.appendChild(el("strong", null, s.title));
      var say = el("ol", "say");
      s.say.forEach(function (line) { say.appendChild(el("li", null, "“" + line + "”")); });
      c.appendChild(say);
      c.appendChild(el("p", "watch", s.watch));
      c.addEventListener("click", function () { startScenario(s.id); });
      cards.appendChild(c);
    });
  }

  function startScenario(id) {
    if (state.busy) { return; }
    api("POST", "/api/sessions", { scenario: id, replaces: state.sid }).then(function (d) {
      state.sid = d.session_id;
      state.scenario = d.scenario;
      Array.prototype.forEach.call(cards.children, function (c) { c.classList.toggle("active", c.dataset.id === id); });
      emptyLog();
      emptyLedger();
      showError("");
      scenarioLabel.textContent = d.scenario.title + " · " + d.scenario.kind;
      var lines = d.scenario.say.map(function (l) { return "“" + l + "”"; }).join(", then ");
      hint.textContent = (state.status.mode === "live" ? "Say " : "Type ") + lines;
      if (state.status.mode !== "live") { textInput.value = d.scenario.say[0]; textInput.focus(); }
      openLedger(d.session_id);
    }).catch(function (e) { showError(e.message); });
  }

  // ---------- the ledger stream ----------
  function openLedger(sid) {
    if (state.stream) { state.stream.abort(); }
    var ctrl = new AbortController();
    state.stream = ctrl;
    fetch("/api/sessions/" + sid + "/ledger?after=0", {
      headers: { "X-Sensei-Token": sessionStorage.getItem(KEY) || "" }, cache: "no-store", signal: ctrl.signal
    }).then(function (r) {
      if (!r.ok) { throw new Error("ledger stream: HTTP " + r.status); }
      liveDot.textContent = "streaming";
      liveDot.classList.add("on");
      var reader = r.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) { liveDot.textContent = "idle"; liveDot.classList.remove("on"); return; }
          buffer += decoder.decode(chunk.value, { stream: true });
          var parts = buffer.split("\n\n");
          buffer = parts.pop();
          parts.forEach(function (block) {
            var data = block.split("\n").filter(function (l) { return l.indexOf("data: ") === 0; })
              .map(function (l) { return l.slice(6); }).join("\n");
            if (data && sid === state.sid) { addStep(JSON.parse(data)); }
          });
          return pump();
        });
      }
      return pump();
    }).catch(function (e) {
      if (e.name !== "AbortError") { liveDot.textContent = "offline"; liveDot.classList.remove("on"); showError(e.message); }
    });
  }

  var SHOWN = { gate: 1, proposed: 1, echoed: 1, approved: 1, denied: 1, executed: 1, verified: 1, violation: 1, turn: 1, error: 1,
                route: 1, model: 1 };

  function addStep(r) {
    var s = r.step || { kind: "minor", title: r.event_type, detail: "" };
    if (!SHOWN[s.kind]) { return; }
    var empty = steps.querySelector(".empty");
    if (empty) { empty.remove(); }
    var li = el("li", "step " + s.kind);
    var node = el("span", "node");
    node.appendChild(icon(s.kind));
    li.appendChild(node);
    var body = el("div", "step-body");
    var head = el("div", "step-head");
    head.appendChild(el("strong", null, s.title));
    head.appendChild(el("time", null, new Date(r.timestamp).toLocaleTimeString()));
    body.appendChild(head);
    if (s.detail) { body.appendChild(el("p", "detail", s.detail)); }
    body.appendChild(el("code", "event", "#" + r.index + " " + r.event_type));
    li.appendChild(body);
    steps.appendChild(li);
    steps.scrollTop = steps.scrollHeight;
  }

  // ---------- start ----------
  api("GET", "/api/status").then(function (status) {
    state.status = status;
    badge.textContent = status.mode_label;
    badge.classList.add(status.mode);
    badgeDetail.textContent = status.mode_detail + " STT: " + (status.stt.provider || "–") + (status.stt.model ? " " + status.stt.model : "") +
      " · TTS: " + (status.tts.provider || "–") + (status.tts.model ? " " + status.tts.model : "");
    demoData.textContent = status.demo_data;
    if (status.spend) { showSpend(status.spend); } else { spend.textContent = "Simulated: no provider calls, no spend."; meter.hidden = true; }
    buildCards(status);
    buildControls(status);
    startScenario(status.scenarios[0].id);
  }).catch(function (e) { showError(e.message); });
})();
