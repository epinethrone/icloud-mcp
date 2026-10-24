// Live updates and actions without reloading for the icloud-mcp dashboard. Served from the server itself (CSP script-src 'self').
// Every page works without this file; it only makes them quicker:
//   - every 15 seconds while the page is visible it asks /dashboard/state.json what is waiting; when that changed, it loads
//     the page again in the background and swaps in its content (Overview and Approvals) or only the counts (other pages)
//   - Send posts the request at once and shows the result above the list
//   - Discard waits 5 seconds with Undo before it is sent; leaving the page sends it right away
//   - it registers a service worker (sw.js, caches nothing) so the dashboard can be installed as an app
(function () {
  "use strict";
  var POLL = 15000, UNDO = 5000, ACT = "/dashboard/approvals/act";
  var version = null, lastOk = 0, timer = null, busyRetry = false;
  var notice = null;             // the result of the last Send, kept across content swaps
  var pending = null;            // a discard waiting for its 5 seconds: {data, row, timer}

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/dashboard/sw.js", { scope: "/dashboard" }).catch(function () {});
  }

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  // -- “Live · updated just now” -----------------------------------------------------------------------------------
  function ago(t) {
    var s = Math.round((Date.now() - t) / 1000);
    if (s < 60) return "just now";
    var m = Math.round(s / 60);
    return m < 60 ? m + " min ago" : new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function showStatus(ok) {
    var el = $("[data-live-status]");
    if (!el || !lastOk) return;
    el.hidden = false;
    el.textContent = (ok ? "Live · updated " : "Offline · last updated ") + ago(lastOk);
    el.classList.toggle("off", !ok);
  }

  // -- refreshing ----------------------------------------------------------------------------------------------------
  function busy() {
    var a = document.activeElement, main = $("main");
    if (pending) return true;
    if (a && main && main.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) && a.type !== "submit") return true;
    return !!$("main input[name=override]:checked");
  }

  function keys(root) { return $$("[data-key]", root).map(function (n) { return n.getAttribute("data-key"); }); }

  function swap(doc) {
    var main = $("main"), next = $("main", doc);
    ["aside.side nav", "nav.tabs"].forEach(function (sel) {
      var a = $(sel), b = $(sel, doc);
      if (a && b) a.replaceWith(b);
    });
    document.title = doc.title;
    if (!main || !next || !main.hasAttribute("data-live")) return;
    var before = keys(main), open = $$("details[open]", main).map(function (d) {
      var row = d.closest("[data-key]");
      return row && row.getAttribute("data-key");
    });
    main.replaceWith(next);
    $$("[data-key]", next).forEach(function (n) {
      var k = n.getAttribute("data-key");
      if (before.indexOf(k) < 0 && n.tagName !== "FORM") {
        n.classList.add("fresh");
        var b = $("b", n);
        if (b && !$(".new", b)) { var tag = document.createElement("span"); tag.className = "new"; tag.textContent = "New"; b.appendChild(tag); }
      }
      if (open.indexOf(k) >= 0) { var d = $("details", n); if (d) d.open = true; }
    });
    putNotice();
    if (window.InstallHint) $$("[data-install-hint]", next).forEach(function (n) { window.InstallHint.mount(n); });
  }

  function refresh() {
    if (busy()) { busyRetry = true; return Promise.resolve(); }
    busyRetry = false;
    return fetch(location.href, { headers: { "x-live": "1" }, credentials: "same-origin", cache: "no-store" }).then(function (r) {
      if (r.redirected || !r.ok) { location.reload(); return; }       // signed out or gone: let the server decide
      return r.text().then(function (html) { swap(new DOMParser().parseFromString(html, "text/html")); });
    });
  }

  function poll() {
    if (document.visibilityState !== "visible") return;
    fetch("/dashboard/state.json", { headers: { "x-live": "1" }, credentials: "same-origin", cache: "no-store" }).then(function (r) {
      if (r.status === 401) { stop(); location.reload(); return; }
      if (!r.ok) throw new Error(String(r.status));
      return r.json().then(function (s) {
        lastOk = Date.now();
        var changed = version !== null && s.v !== version;
        version = s.v;
        showStatus(true);
        if (changed || busyRetry) return refresh();
      });
    }).catch(function () { showStatus(false); });
  }

  function start() { if (!timer) { poll(); timer = setInterval(poll, POLL); } }
  function stop() { clearInterval(timer); timer = null; }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") start(); else { stop(); flushDiscard(); }
  });
  window.addEventListener("pagehide", flushDiscard);
  window.addEventListener("online", poll);
  setInterval(function () { showStatus(true); }, 30000);

  // -- results of an action ------------------------------------------------------------------------------------------
  function putNotice() {
    var stack = $("main .stack");
    if (!notice || !stack) return;
    var old = $("main .stack > .banner");
    if (old && old !== notice) old.remove();
    stack.insertBefore(notice, stack.firstChild);
  }

  function say(text, bad) {
    notice = document.createElement("p");
    notice.className = "banner " + (bad ? "bad" : "ok");
    notice.setAttribute("role", bad ? "alert" : "status");
    var t = document.createElement("span"); t.textContent = text; notice.appendChild(t);
    var w = document.createElement("span"); w.className = "when num";
    w.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    notice.appendChild(w);
    putNotice();
  }

  function post(data, keepalive) {
    return fetch(ACT, { method: "POST", body: new URLSearchParams(data), credentials: "same-origin", keepalive: !!keepalive,
                        headers: { "accept": "application/json" } }).then(function (r) {
      var json = (r.headers.get("content-type") || "").indexOf("application/json") >= 0;
      if (!json) { location.reload(); return null; }        // signed out or a refused form: the server's own page explains
      return r.json();
    });
  }

  // -- Discard with Undo --------------------------------------------------------------------------------------------
  var toast = null;

  function showToast(title) {
    if (!toast) {
      toast = document.createElement("div");
      toast.className = "toast";
      toast.setAttribute("role", "status");
      toast.innerHTML = '<div class="toast-row"><p></p><button type="button">Undo</button></div><div class="toast-bar" aria-hidden="true"><i></i></div>';
      $("button", toast).addEventListener("click", undo);
      document.body.appendChild(toast);
    }
    $("p", toast).textContent = "Discarded “" + title + "”. Nothing was sent.";
    var bar = $(".toast-bar i", toast);
    bar.style.transition = "none"; bar.style.width = "100%";
    toast.hidden = false;
    void bar.offsetWidth;                                    // restart the bar's animation
    bar.style.transition = "width " + UNDO + "ms linear"; bar.style.width = "0%";
  }

  function hideToast() { if (toast) toast.hidden = true; }

  function undo() {
    if (!pending) return;
    clearTimeout(pending.timer);
    setGone(pending.row, false);
    pending = null;
    hideToast();
  }

  function setGone(key, gone) {          // the row on the Overview, or the list entry and the open request on Approvals
    $$("[data-key]").forEach(function (n) {
      if (n.getAttribute("data-key") !== key) return;
      n.classList.toggle("gone", gone);
      $$("button", n).forEach(function (b) { b.disabled = gone; });
    });
  }

  function commitDiscard(keepalive) {
    var p = pending;
    if (!p) return;
    clearTimeout(p.timer);
    pending = null;
    hideToast();
    var sent = post(p.data, keepalive);
    if (keepalive) return;
    sent.then(function (res) {
      if (res && !res.ok) { setGone(p.row, false); say(res.error, true); }
      return refresh();
    }).catch(function () { setGone(p.row, false); say("Could not reach the server. Nothing was discarded.", true); });
  }

  function flushDiscard() { if (pending) commitDiscard(true); }

  // -- the forms -----------------------------------------------------------------------------------------------------
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || form.getAttribute("action") !== ACT || !window.fetch || !window.URLSearchParams) return;
    var btn = e.submitter, action = btn && btn.value;
    if (action !== "approve" && action !== "discard") return;
    e.preventDefault();
    var data = new FormData(form);
    data.set("action", action);
    var holder = form.closest("[data-key]"), row = holder ? holder.getAttribute("data-key") : "", title = form.getAttribute("data-title") || "";
    if (action === "discard") {
      if (pending) commitDiscard(false);                     // one Undo at a time; the earlier one goes through now
      setGone(row, true);
      pending = { data: data, row: row, timer: setTimeout(function () { commitDiscard(false); }, UNDO) };
      showToast(title);
      return;
    }
    $$("button", form).forEach(function (b) { b.disabled = true; });
    btn.textContent = "Sending…";
    post(data).then(function (res) {
      if (!res) return;
      if (res.ok) { say(res.note, false); setGone(row, true); } else say(res.error, true);
      return refresh();
    }).catch(function () {
      say("Could not reach the server. It may or may not have been sent: check Activity before you try again.", true);
      return refresh();
    });
  }, true);

  start();
})();
