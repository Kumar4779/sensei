/*
 * Talk to Sensei (Demo Slice 1). Loaded by the public shell with X-Sensei-Token.
 * Every request carries the token from sessionStorage. Every string from the
 * server is set with textContent, never as HTML.
 *
 * LIVE SPEECH: push-to-talk with MediaRecorder; the recording is POSTed to the
 * server, which runs Deepgram → the voice loop → ElevenLabs and returns the
 * reply's audio, played here and then dropped.
 * SIMULATED: typed input; no audio anywhere. The badge says which, always.
 *
 * The ledger is Server-Sent Events read through fetch (so the token stays in a
 * header): the session's trace, as the server writes it.
 */
(function () {
  "use strict";
  var KEY = "sensei.demo.token";
  var root = document.getElementById("app");
  var state = { status: null, sid: null, scenario: null, stream: null, recorder: null, chunks: [], busy: false };

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined && text !== null) { e.textContent = String(text); }
    return e;
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
  var title = el("div", "brand");
  title.appendChild(el("h1", null, "Talk to Sensei"));
  title.appendChild(el("p", "sub", "Every action passes the same permission gate as the rest of Sensei. Watch it on the right."));
  var badge = el("div", "badge", "…");
  var badgeDetail = el("p", "badge-detail");
  var modeBox = el("div", "mode");
  modeBox.appendChild(badge);
  modeBox.appendChild(badgeDetail);
  header.appendChild(title);
  header.appendChild(modeBox);

  var demoData = el("p", "demo-data");
  var cards = el("section", "cards");
  var main = el("main", "grid");
  var convo = el("section", "panel convo");
  convo.appendChild(el("h2", null, "Conversation"));
  var log = el("div", "log");
  log.setAttribute("aria-live", "polite");
  var controls = el("div", "controls");
  var hint = el("p", "hint");
  var errBox = el("p", "err");
  errBox.setAttribute("role", "alert");
  convo.appendChild(log);
  convo.appendChild(hint);
  convo.appendChild(controls);
  convo.appendChild(errBox);

  var ledgerPanel = el("section", "panel ledger");
  ledgerPanel.appendChild(el("h2", null, "Permission ledger"));
  ledgerPanel.appendChild(el("p", "sub", "Live from this session's trace: every gate decision, echo, approval, decline and execution, in the order it happened."));
  var steps = el("ol", "steps");
  ledgerPanel.appendChild(steps);

  main.appendChild(convo);
  main.appendChild(ledgerPanel);
  root.appendChild(header);
  root.appendChild(demoData);
  root.appendChild(cards);
  root.appendChild(main);

  function showError(message) { errBox.textContent = message || ""; }

  // ---------- conversation ----------
  function bubble(who, text, meta) {
    var b = el("div", "bubble " + who);
    b.appendChild(el("span", "who", who === "you" ? "You (heard)" : "Sensei"));
    b.appendChild(el("p", "text", text));
    if (meta && meta.length) {
      var m = el("div", "meta");
      meta.forEach(function (chip) { m.appendChild(el("span", "chip", chip)); });
      b.appendChild(m);
    }
    log.appendChild(b);
    log.scrollTop = log.scrollHeight;
    return b;
  }

  function ms(v) { return v === null || v === undefined ? "–" : Math.round(v) + " ms"; }

  function play(base64, type, onPlaying) {
    var bytes = Uint8Array.from(atob(base64), function (c) { return c.charCodeAt(0); });
    var url = URL.createObjectURL(new Blob([bytes], { type: type }));
    var audio = new Audio(url);
    audio.addEventListener("playing", onPlaying, { once: true });
    audio.addEventListener("ended", function () { URL.revokeObjectURL(url); }, { once: true });
    return audio.play();
  }

  function handleTurn(d, started) {
    var heardMeta = [];
    if (d.confidence !== null && d.confidence !== undefined) { heardMeta.push("confidence " + Number(d.confidence).toFixed(2)); }
    if (d.real_path) { heardMeta.push("STT " + ms(d.real_path.stt_latency_ms) + " (trace)"); }
    bubble("you", d.heard || "(nothing heard)", heardMeta);
    var meta = ["outcome: " + d.outcome, "hard gate " + d.hard_gate];
    if (d.real_path) {
      meta.push("time to first audio " + ms(d.real_path.time_to_first_audio_ms) + " (trace, server)");
      meta.push("TTS first byte " + ms(d.real_path.tts_first_byte_ms) + " (trace)");
    }
    var b = bubble("sensei", d.reply, meta);
    if (d.error) { showError("The reply could not be spoken: " + d.error); }
    if (d.audio) {
      play(d.audio, d.audio_type, function () {
        var m = b.querySelector(".meta");
        m.appendChild(el("span", "chip browser", "audio started in page " + ms(performance.now() - started) + " after you stopped"));
      }).catch(function () { showError("The browser blocked playback; click the page and try again."); });
    }
    if (d.spend) { spend.textContent = "Estimated spend this server run: $" + d.spend.estimated_usd.toFixed(4) + " of $" + d.spend.cap_usd.toFixed(2) + " cap"; }
  }

  function send(body, contentType) {
    if (!state.sid || state.busy) { return; }
    state.busy = true;
    setBusy(true);
    showError("");
    var started = performance.now();
    api("POST", "/api/sessions/" + state.sid + "/turn", body, contentType).then(function (d) {
      handleTurn(d, started);
    }).catch(function (e) { showError(e.message); }).then(function () { state.busy = false; setBusy(false); });
  }

  // ---------- controls: live push-to-talk, or simulated typing ----------
  var talk = el("button", "talk", "Hold to talk");
  var textInput = el("input", "typed");
  var sendBtn = el("button", "send", "Send");
  var spend = el("p", "spend");

  function setBusy(on) {
    talk.disabled = on && !state.recorder;
    sendBtn.disabled = on;
    talk.classList.toggle("busy", on && !state.recorder);
    if (state.status && state.status.mode === "live") { talk.textContent = state.recorder ? "Release to send" : (on ? "Thinking…" : "Hold to talk"); }
  }

  function startRecording() {
    if (state.busy || state.recorder) { return; }
    state.holding = true;
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      var type = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" :
        (MediaRecorder.isTypeSupported("audio/mp4") ? "audio/mp4" : "");
      var rec = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
      state.chunks = [];
      rec.addEventListener("dataavailable", function (e) { if (e.data.size) { state.chunks.push(e.data); } });
      rec.addEventListener("stop", function () {
        stream.getTracks().forEach(function (t) { t.stop(); });
        var blob = new Blob(state.chunks, { type: rec.mimeType || "audio/webm" });
        state.chunks = [];
        state.recorder = null;
        talk.classList.remove("recording");
        if (blob.size) { send(blob, (rec.mimeType || "audio/webm").split(";")[0]); } else { setBusy(false); }
      });
      state.recorder = rec;
      rec.start();
      talk.classList.add("recording");
      talk.textContent = "Release to send";
      if (!state.holding) { rec.stop(); }  // released before the microphone was ready
    }).catch(function (e) { showError("Microphone unavailable: " + e.message); });
  }

  function stopRecording() { state.holding = false; if (state.recorder && state.recorder.state === "recording") { state.recorder.stop(); } }

  function buildControls(status) {
    controls.textContent = "";
    if (status.mode === "live") {
      talk.addEventListener("pointerdown", function (e) { e.preventDefault(); startRecording(); });
      talk.addEventListener("pointerup", stopRecording);
      talk.addEventListener("pointerleave", stopRecording);
      document.addEventListener("keydown", function (e) { if (e.code === "Space" && !e.repeat && e.target === document.body) { e.preventDefault(); startRecording(); } });
      document.addEventListener("keyup", function (e) { if (e.code === "Space" && e.target === document.body) { e.preventDefault(); stopRecording(); } });
      controls.appendChild(talk);
      controls.appendChild(el("span", "keyhint", "or hold the space bar"));
    } else {
      textInput.placeholder = "Type what you would say…";
      textInput.setAttribute("aria-label", "Typed utterance");
      textInput.maxLength = 500;
      textInput.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); sendTyped(); } });
      sendBtn.addEventListener("click", sendTyped);
      controls.appendChild(textInput);
      controls.appendChild(sendBtn);
    }
    convo.appendChild(spend);
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
      c.appendChild(el("span", "kind", s.kind));
      c.appendChild(el("strong", null, s.title));
      var say = el("ol", "say");
      s.say.forEach(function (line) { say.appendChild(el("li", null, "Say: “" + line + "”")); });
      c.appendChild(say);
      c.appendChild(el("p", "watch", s.watch));
      c.addEventListener("click", function () { startScenario(s.id); });
      cards.appendChild(c);
    });
  }

  function startScenario(id) {
    if (state.busy) { return; }
    api("POST", "/api/sessions", { scenario: id }).then(function (d) {
      state.sid = d.session_id;
      state.scenario = d.scenario;
      Array.prototype.forEach.call(cards.children, function (c) { c.classList.toggle("active", c.dataset.id === id); });
      log.textContent = "";
      steps.textContent = "";
      showError("");
      var lines = d.scenario.say.map(function (l) { return "“" + l + "”"; }).join(", then ");
      hint.textContent = (state.status.mode === "live" ? "Hold the button and say " : "Type ") + lines + ".";
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
      var reader = r.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) { return; }
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
    }).catch(function (e) { if (e.name !== "AbortError") { showError(e.message); } });
  }

  var SHOWN = { gate: 1, proposed: 1, echoed: 1, approved: 1, denied: 1, executed: 1, verified: 1, violation: 1, turn: 1, error: 1 };

  function addStep(r) {
    var s = r.step || { kind: "minor", title: r.event_type, detail: "" };
    if (!SHOWN[s.kind]) { return; }
    var li = el("li", "step " + s.kind);
    var head = el("div", "step-head");
    head.appendChild(el("span", "dot"));
    head.appendChild(el("strong", null, s.title));
    head.appendChild(el("time", null, new Date(r.timestamp).toLocaleTimeString()));
    li.appendChild(head);
    if (s.detail) { li.appendChild(el("p", "detail", s.detail)); }
    li.appendChild(el("code", "event", "#" + r.index + " " + r.event_type));
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
    if (status.spend) { spend.textContent = "Estimated spend this server run: $" + status.spend.estimated_usd.toFixed(4) + " of $" + status.spend.cap_usd.toFixed(2) + " cap"; }
    buildCards(status);
    buildControls(status);
    startScenario(status.scenarios[0].id);
  }).catch(function (e) { showError(e.message); });
})();
