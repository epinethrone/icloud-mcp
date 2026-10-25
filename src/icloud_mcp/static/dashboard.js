// Live updates and actions without reloading for the icloud-mcp dashboard. Served from the server itself (CSP script-src 'self').
// Every page works without this file; it only makes them quicker:
//   - every 15 seconds while the page is visible it asks /dashboard/state.json what is waiting; when that changed, it updates
//     the counts, and on Overview and Approvals loads the page's own address (<main data-live>) again in the background and
//     swaps in its content. It never reloads or re-requests the address a form was posted to.
//   - Send posts the request at once and shows the result above the list
//   - Discard waits 5 seconds with Undo before it is sent; hiding or leaving the page sends it right away
//   - it registers a service worker (sw.js, caches nothing) so the dashboard can be installed as an app
(function () {
  "use strict";
  var POLL = 15000, UNDO = 5000, HANDS_OFF = 4000, ACT = "/dashboard/approvals/act";
  var wrap = document.querySelector(".wrap[data-v]");
  var version = wrap ? wrap.getAttribute("data-v") : null;     // what the page was rendered with
  var lastOk = 0, online = true, timer = null, busyRetry = false, flushed = false, lastPointer = 0;
  var notice = null, noticeAt = 0;  // the result of the last action, kept across content swaps for a minute
  var pending = null;            // a discard waiting for its 5 seconds: {data, row, timer}

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/dashboard/sw.js", { scope: "/dashboard" }).catch(function () {});  // optional: the pages work without it
  }

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function home() {
    var m = $("main[data-live]");
    var raw = (m && m.getAttribute("data-live")) || "/dashboard";
    try {
      var u = new URL(raw, location.origin);
      if (u.origin !== location.origin) return "/dashboard";
      if (u.pathname.indexOf("/dashboard") !== 0) return "/dashboard";
      return u.pathname + u.search + u.hash;
    } catch (_) {
      return "/dashboard";
    }
  }

  // -- “Live · updated just now” -----------------------------------------------------------------------------------
  function ago(t) {
    var s = Math.round((Date.now() - t) / 1000);
    if (s < 60) return "just now";
    var m = Math.round(s / 60);
    return m < 60 ? m + " min ago" : new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function showStatus() {
    var el = $("[data-live-status]");
    if (!el || !lastOk) return;
    el.hidden = false;
    el.textContent = (online ? "Live · updated " : "Offline · last updated ") + ago(lastOk);
    el.classList.toggle("off", !online);
  }

  // -- counts on every page ------------------------------------------------------------------------------------------
  function badge(holder, cls, n) {
    if (!holder) return;
    var b = $("." + cls, holder);
    if (!n) { if (b) b.remove(); return; }
    if (!b) { b = document.createElement("span"); b.className = cls; holder.appendChild(b); }
    b.textContent = String(n);
    if (cls === "count") b.setAttribute("aria-label", n + " waiting");
  }

  function setCount(n) {
    badge($('aside.side a[href="/dashboard/approvals"]'), "num", n);
    badge($('nav.tabs a[href="/dashboard/approvals"] .ico'), "count", n);
    document.title = (n ? "(" + n + ") " : "") + document.title.replace(/^\(\d+\) /, "");
  }

  // -- refreshing Overview and Approvals -----------------------------------------------------------------------------
  // Not while the owner is typing, has a discard waiting, ticked “still send”, or has just pointed at the page:
  // a request moving under the cursor could be sent by mistake.
  function busy(force) {
    var a = document.activeElement, main = $("main");
    if (pending) return true;
    if (a && main && main.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) && a.type !== "submit") return true;
    if ($("main [data-key]:not(.gone) input[name=override]:checked")) return true;
    return !force && Date.now() - lastPointer < HANDS_OFF;
  }
  ["pointermove", "pointerdown", "touchstart", "wheel"].forEach(function (t) {
    document.addEventListener(t, function (e) { if (e.target && e.target.closest && e.target.closest("main")) lastPointer = Date.now(); }, { passive: true });
  });

  function keys(root) { return $$("[data-key]", root).map(function (n) { return n.getAttribute("data-key"); }); }

  function focusMark() {                 // which control had focus, by its request and its role, to put focus back after a swap
    var a = document.activeElement, holder = a && a.closest && a.closest("main [data-key]");
    if (!holder) return null;
    return { key: holder.getAttribute("data-key"), sel: a.tagName === "SUMMARY" ? "summary" : a.tagName === "BUTTON" ? 'button[value="' + a.value + '"]' : null };
  }

  function swap(doc, settle) {
    var main = $("main"), next = $("main", doc);
    if (!main || !next || !main.hasAttribute("data-live")) return;
    var before = keys(main), mark = focusMark(), open = $$("details[open]", main).map(function (d) {
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
      if (mark && mark.sel && k === mark.key) { var f = $(mark.sel, n); if (f) f.focus(); }
    });
    putNotice();
    if (settle) {                        // after the owner's own action the next request moves up: no Send by the same tap
      var fresh = $$("button[value=approve]", next);
      fresh.forEach(function (b) { b.disabled = true; });
      setTimeout(function () { fresh.forEach(function (b) { if (!b.closest(".gone")) b.disabled = false; }); }, 1500);
    }
    if (window.InstallHint) $$("[data-install-hint]", next).forEach(function (n) { window.InstallHint.mount(n); });
  }

  function refresh(force) {
    if (!$("main[data-live]")) return Promise.resolve();
    if (busy(force)) { busyRetry = true; return Promise.resolve(); }
    busyRetry = false;
    return fetch(home(), { headers: { "x-live": "1" }, credentials: "same-origin", cache: "no-store" }).then(function (r) {
      if (r.redirected || r.status === 401 || r.status === 403) { location.assign(home()); return; }   // signed out: let the server show sign-in
      if (!r.ok) throw new Error(String(r.status));
      return r.text().then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        swap(doc, force);
        document.title = doc.title;
      });
    }).catch(function () { online = false; busyRetry = true; showStatus(); });
  }

  function poll() {
    if (document.visibilityState !== "visible") return;
    fetch("/dashboard/state.json", { headers: { "x-live": "1" }, credentials: "same-origin", cache: "no-store" }).then(function (r) {
      if (r.status === 401) { stop(); location.assign(home()); return; }
      if (!r.ok) throw new Error(String(r.status));
      return r.json().then(function (s) {
        lastOk = Date.now(); online = true;
        var changed = s.v !== version;
        version = s.v;
        showStatus();
        if (changed) setCount(s.waiting);
        if (changed || busyRetry || flushed) { var f = flushed; flushed = false; return refresh(f); }
      });
    }).catch(function () { online = false; showStatus(); });
  }

  function start() { if (!timer) { poll(); timer = setInterval(poll, POLL); } }
  function stop() { clearInterval(timer); timer = null; }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") start(); else { stop(); flushDiscard(); }
  });
  window.addEventListener("pagehide", flushDiscard);
  window.addEventListener("online", poll);
  setInterval(showStatus, 30000);

  // -- results of an action ------------------------------------------------------------------------------------------
  function putNotice() {
    var stack = $("main .stack");
    if (!notice || !stack || Date.now() - noticeAt > 60000) return;
    var old = $("main .stack > .banner");
    if (old && old !== notice) old.remove();
    stack.insertBefore(notice, stack.firstChild);
  }

  function say(text, bad) {
    notice = document.createElement("p"); noticeAt = Date.now();
    notice.className = "banner " + (bad ? "bad" : "ok");
    notice.setAttribute("role", bad ? "alert" : "status");
    var t = document.createElement("span"); t.textContent = text; notice.appendChild(t);
    var w = document.createElement("span"); w.className = "when num";
    w.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    notice.appendChild(w);
    putNotice();
  }

  // Resolves to the server's JSON answer, or to null when the server refused the form before acting on it (signed out,
  // expired form): then nothing was done, the owner is told so and the page is loaded again.
  function post(data, keepalive) {
    return fetch(ACT, { method: "POST", body: new URLSearchParams(data), credentials: "same-origin", keepalive: !!keepalive,
                        headers: { "accept": "application/json" } }).then(function (r) {
      if ((r.headers.get("content-type") || "").indexOf("application/json") >= 0) return r.json();
      if (keepalive) return null;
      if (r.redirected || r.status === 403) {                 // signed out or an expired form: refused before anything was done
        say("Nothing was done: this page has expired. Loading it again…", true);
        setTimeout(function () { location.assign(home()); }, 2500);
        return null;
      }
      throw new Error(String(r.status));                     // a proxy or server error: the outcome is unknown
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

  function leave(key) {                  // after acting on the request open on Approvals, show the list, not “no longer waiting”
    var m = $("main[data-live]");
    if (!m || !$('form.card[data-key="' + key + '"]', m)) return;
    m.setAttribute("data-live", "/dashboard/approvals");
    if (history.replaceState) history.replaceState(null, "", "/dashboard/approvals");
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
    if (keepalive) { flushed = true; sent.catch(function () {}); return; }   // the next poll shows what the server did with it
    sent.then(function (res) {
      if (res && !res.ok) { setGone(p.row, false); say(res.error, true); }
      if (res && res.ok) leave(p.row);
      if (res) refresh(true);
    }, function () { setGone(p.row, false); say("The server did not answer. The list below shows whether it is still waiting.", true); refresh(true); });
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
    var buttons = $$("button", form), label = btn.textContent;
    function again() { buttons.forEach(function (b) { b.disabled = false; }); btn.textContent = label; }
    buttons.forEach(function (b) { b.disabled = true; });
    btn.textContent = "Sending…";
    post(data).then(function (res) {
      if (!res) return;
      if (res.ok) { say(res.note, false); setGone(row, true); leave(row); } else { again(); say(res.error, true); }
      refresh(true);
    }, function () {
      again();
      say("Could not reach the server. It may or may not have been sent: check Activity before you try again.", true);
      refresh(true);
    });
  }, true);

  start();
})();
