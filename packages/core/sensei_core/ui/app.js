/* Sensei web UI (Slice 22): a client of the gateway, nothing more.
 * Every request carries X-Sensei-Token from sessionStorage. All text from the
 * server is inserted with textContent (never innerHTML), so brief and memory
 * text cannot inject markup.
 * Endpoints used: GET /health, POST /kill, POST /revive, GET /costs/today,
 * GET /brief/today, POST /brief/run, GET /memory/pending, POST /memory/confirm,
 * DELETE /memory/{id}.
 */
(function () {
  "use strict";
  var KEY = "sensei.token";
  var root = document.getElementById("app");

  // ---- helpers -------------------------------------------------------------

  function el(tag, attrs, children) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") { n.textContent = attrs[k]; }
      else if (k === "class") { n.className = attrs[k]; }
      else if (k.slice(0, 2) === "on") { n.addEventListener(k.slice(2), attrs[k]); }
      else { n.setAttribute(k, attrs[k]); }
    });
    (children || []).forEach(function (c) { if (c) { n.appendChild(c); } });
    return n;
  }

  function signOut(message) {
    sessionStorage.removeItem(KEY);
    location.reload();
    if (message) { console.warn(message); }
  }

  function api(method, path, body) {
    var opts = { method: method, cache: "no-store", headers: { "X-Sensei-Token": sessionStorage.getItem(KEY) || "" } };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    return fetch(path, opts).then(function (r) {
      if (r.status === 401) { signOut("token rejected"); throw new Error("unauthorized"); }
      return r.json().catch(function () { return {}; }).then(function (data) { return { status: r.status, ok: r.ok, data: data }; });
    });
  }

  function usd(x) { return "$" + (Number(x) || 0).toFixed(4); }

  function setStatus(node, text, kind) { node.textContent = text; node.className = "note " + (kind || ""); }

  // ---- minimal, safe Markdown rendering for the brief -------------------------

  function renderMarkdown(src, into) {
    into.textContent = "";
    var text = src.replace(/^\s*```[a-z]*\s*\n/i, "").replace(/\n```\s*(\n|$)/, "\n");  // drop a wrapping fence
    var list = null, quote = null;
    function close() { list = null; quote = null; }
    text.split("\n").forEach(function (line) {
      var m;
      if (/^\s*```/.test(line)) { return; }
      if (!line.trim()) { close(); return; }
      if ((m = line.match(/^(#{1,4})\s+(.*)$/))) { close(); into.appendChild(el("h" + Math.min(m[1].length + 1, 5), { text: m[2] })); return; }
      if ((m = line.match(/^\s*>\s?(.*)$/))) {
        list = null;
        if (!quote) { quote = el("blockquote"); into.appendChild(quote); }
        quote.appendChild(el("div", { text: m[1] }));
        return;
      }
      if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {
        quote = null;
        if (!list) { list = el("ul"); into.appendChild(list); }
        list.appendChild(el("li", { text: m[1].replace(/\*\*/g, "") }));
        return;
      }
      close();
      into.appendChild(el("p", { text: line.replace(/\*\*/g, "") }));
    });
  }

  // ---- layout -------------------------------------------------------------

  var killState = el("span", { class: "pill", text: "…" });
  var killBtn = el("button", { class: "danger", text: "Kill", onclick: toggleKill });
  var killNote = el("div", { class: "note" });

  var briefStatus = el("div", { class: "badge", text: "No brief yet" });
  var briefMeta = el("div", { class: "note" });
  var briefBody = el("article", { class: "brief" });
  var briefBtn = el("button", { class: "primary", text: "Run today's brief", onclick: runBrief });

  var pendingList = el("ul", { class: "approvals" });
  var pendingNote = el("div", { class: "note" });

  var costTotal = el("div", { class: "big", text: "—" });
  var costRoutes = el("ul", { class: "routes" });
  var costNote = el("div", { class: "note" });

  root.appendChild(el("header", {}, [
    el("h1", { text: "Sensei 🥋" }),
    el("div", { class: "kill" }, [killState, killBtn]),
  ]));
  root.appendChild(killNote);
  root.appendChild(el("section", { id: "brief" }, [
    el("div", { class: "row" }, [el("h2", { text: "Morning brief" }), briefStatus]),
    briefMeta, briefBody, briefBtn,
  ]));
  root.appendChild(el("section", { id: "approvals" }, [
    el("div", { class: "row" }, [el("h2", { text: "Approvals" }), el("button", { class: "ghost", text: "Refresh", onclick: loadPending })]),
    pendingNote, pendingList,
  ]));
  root.appendChild(el("section", { id: "costs" }, [
    el("h2", { text: "Today's spend" }), costTotal, costRoutes, costNote,
  ]));
  root.appendChild(el("footer", {}, [el("button", { class: "ghost", text: "Sign out", onclick: function () { signOut(); } })]));
  root.hidden = false;

  // ---- kill switch ----------------------------------------------------------

  var killed = false;
  function showKill(k) {
    killed = !!k;
    killState.textContent = killed ? "KILLED — runs refused" : "Running";
    killState.className = "pill " + (killed ? "off" : "on");
    killBtn.textContent = killed ? "Revive" : "Kill";
    killBtn.className = killed ? "primary" : "danger";
    briefBtn.disabled = killed;
  }
  function loadHealth() {
    return fetch("/health", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (d) { showKill(d.killed); });
  }
  function toggleKill() {
    killBtn.disabled = true;
    api("POST", killed ? "/revive" : "/kill").then(function (r) {
      showKill(r.data.killed);
      setStatus(killNote, r.data.killed ? "Kill switch engaged: the gateway refuses new runs." : "Revived: runs allowed again.", r.data.killed ? "warn" : "");
    }).finally(function () { killBtn.disabled = false; });
  }

  // ---- brief ---------------------------------------------------------------

  function showBrief(d) {
    var ok = d.verified === true;
    briefStatus.textContent = ok ? "Verified" : "Unverified";
    briefStatus.className = "badge " + (ok ? "ok" : "bad");
    var meta = d.day + " · " + d.model + " · " + d.attempts + (d.attempts === 1 ? " try" : " tries");
    if (!ok && d.failed_checks && d.failed_checks.length) { meta += " · failed: " + d.failed_checks.join(", "); }
    briefMeta.textContent = meta;
    if (typeof d.text === "string") { renderMarkdown(d.text, briefBody); }
  }
  function loadBrief() {
    return api("GET", "/brief/today").then(function (r) {
      if (r.ok) { showBrief(r.data); }
      else { briefStatus.textContent = "No brief yet"; briefStatus.className = "badge"; briefMeta.textContent = r.data.error || ""; briefBody.textContent = ""; }
    });
  }
  function runBrief() {
    briefBtn.disabled = true;
    briefBtn.textContent = "Writing… (local model, ~30 s)";
    api("POST", "/brief/run").then(function (r) {
      if (!r.ok) { briefMeta.textContent = "Refused: " + (r.data.error || ("HTTP " + r.status)); return; }
      return loadBrief();
    }).finally(function () { briefBtn.textContent = "Run today's brief"; briefBtn.disabled = killed; loadPending(); loadCosts(); });
  }

  // ---- approvals (pending memory, e.g. proposed by the Custodian) -------------

  function loadPending() {
    return api("GET", "/memory/pending").then(function (r) {
      pendingList.textContent = "";
      var items = Array.isArray(r.data) ? r.data : [];
      setStatus(pendingNote, items.length ? items.length + " awaiting your decision" : "Nothing awaiting approval.");
      items.forEach(function (e) {
        var actions = el("div", { class: "actions" }, [
          el("button", { class: "primary", text: "Confirm", onclick: function () { decide(e, "confirm", li); } }),
          el("button", { class: "danger", text: "Reject", onclick: function () { decide(e, "reject", li); } }),
        ]);
        var li = el("li", { "data-id": e.id }, [
          el("div", { class: "fact", text: e.fact }),
          el("div", { class: "meta", text: e.category + " · " + e.status + " · from " + e.origin }),
          actions,
        ]);
        pendingList.appendChild(li);
      });
    });
  }
  function decide(entry, action, li) {
    if (action === "reject" && !confirm("Reject and permanently delete this entry?\n\n" + entry.fact)) { return; }
    li.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
    var call = action === "confirm"
      ? api("POST", "/memory/confirm", { entry_id: entry.id })
      : api("DELETE", "/memory/" + encodeURIComponent(entry.id));
    call.then(function (r) {
      setStatus(pendingNote, r.ok ? (action === "confirm" ? "Confirmed: now active." : "Rejected and deleted.") : ("Failed: " + (r.data.error || r.status)), r.ok ? "" : "warn");
      return loadPending();
    });
  }

  // ---- costs -----------------------------------------------------------------

  function loadCosts() {
    return api("GET", "/costs/today").then(function (r) {
      costTotal.textContent = usd(r.data.total_est_cost_usd);
      costRoutes.textContent = "";
      var routes = r.data.by_route || {};
      Object.keys(routes).forEach(function (k) {
        costRoutes.appendChild(el("li", {}, [el("span", { text: k }), el("span", { text: usd(routes[k]) })]));
      });
      if (!Object.keys(routes).length) { costRoutes.appendChild(el("li", {}, [el("span", { text: "No model calls today" })])); }
      setStatus(costNote, "Estimated · UTC day " + r.data.day_utc);
    });
  }

  loadHealth().then(function () { return Promise.all([loadBrief(), loadPending(), loadCosts()]); })
    .catch(function (e) { setStatus(killNote, "Could not reach the gateway: " + e.message, "warn"); });
})();
