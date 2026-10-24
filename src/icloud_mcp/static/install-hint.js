/*
 * install-hint.js: an "Install as an app" tip for PWAs, without a framework.
 *
 * Core (always):
 *   InstallHint.init({ app, lang, storageKey, isStandalone, onChange })
 *   InstallHint.state()    -> "button" | "ios-safari" | "ios-safari-menu" | "ios-chrome" | "none"
 *   InstallHint.install()  -> Promise<"accepted" | "dismissed" | "unavailable">
 *   InstallHint.dismiss()  -> hides the tip for good (localStorage)
 *   InstallHint.texts      -> { de, en }, texts for your own rendering
 * Rendering (optional):
 *   InstallHint.mount(element)  renders a card with `ih-*` classes (styles in dashboard.css)
 *
 * Platforms:
 *   - Chromium (Android, desktop): a button through `beforeinstallprompt`.
 *   - iPhone/iPad Safari: no button is possible, so steps. From Safari 26, Share sits behind "⋯" in the default
 *     compact view; before that it is in the bar.
 *   - iPhone/iPad Chrome (CriOS): steps, Share next to the address bar.
 *   - Anything else, an installed app, or a hidden tip: "none".
 *
 * Works under a strict CSP: no eval, no inline handlers, no outside assets.
 */
(function () {
  "use strict";

  var SHARE_PATH = ["M8 11V1", "M4.5 4.5 8 1l3.5 3.5", "M5 7H3a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V8a1 1 0 0 0-1-1h-2"];

  var TEXTS = {
    de: {
      title: "{app} als App",
      bodyButton: "Öffnet sich ohne Browser-Leiste, direkt vom Startbildschirm.",
      bodyIos: "Öffnet sich ohne Browser-Leiste, direkt vom Home-Bildschirm.",
      button: "Installieren",
      dismiss: "Hinweis ausblenden",
      stepsSafari: ["Unten auf {share} tippen", "„Zum Home-Bildschirm“ wählen"],
      stepsSafariMenu: ["Unten auf ⋯ tippen, dann auf „Teilen“", "„Zum Home-Bildschirm“ wählen"],
      stepsChrome: ["Neben der Adresszeile auf {share} tippen", "„Zum Home-Bildschirm“ wählen"],
    },
    en: {
      title: "Get {app} as an app",
      bodyButton: "Opens without the browser bar, straight from your home screen.",
      bodyIos: "Opens without the browser bar, straight from your Home Screen.",
      button: "Install",
      dismiss: "Hide this tip",
      stepsSafari: ["Tap {share} at the bottom", "Choose “Add to Home Screen”"],
      stepsSafariMenu: ["Tap ⋯ at the bottom, then Share", "Choose “Add to Home Screen”"],
      stepsChrome: ["Tap {share} next to the address bar", "Choose “Add to Home Screen”"],
    },
  };

  var opts = { app: document.title, lang: "de", storageKey: "install-hint-dismissed", isStandalone: null, onChange: null };
  var deferred = null; // BeforeInstallPromptEvent
  var installed = false;
  var mounts = [];

  function standalone() {
    if (typeof opts.isStandalone === "function") return !!opts.isStandalone();
    if (typeof opts.isStandalone === "boolean") return opts.isStandalone;
    return (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || window.navigator.standalone === true;
  }

  function dismissed() {
    try {
      return window.localStorage.getItem(opts.storageKey) === "1";
    } catch (e) {
      return false;
    }
  }

  function ios() {
    var ua = navigator.userAgent;
    // iPadOS reports itself as a Mac; touch gives it away.
    return /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
  }

  function state() {
    if (installed || standalone() || dismissed()) return "none";
    if (deferred) return "button";
    if (!ios()) return "none";
    var ua = navigator.userAgent;
    if (/CriOS\//.test(ua)) return "ios-chrome";
    // Real Safari: no other browser, no in-app view (those do not report "Safari/").
    if (/FxiOS|EdgiOS|OPiOS|GSA\//.test(ua) || !/Safari\//.test(ua)) return "none";
    var v = /Version\/(\d+)/.exec(ua);
    return v && Number(v[1]) >= 26 ? "ios-safari-menu" : "ios-safari";
  }

  function changed() {
    var s = state();
    for (var i = 0; i < mounts.length; i++) render(mounts[i]);
    if (typeof opts.onChange === "function") opts.onChange(s);
  }

  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault(); // our own button instead of the mini infobar
    deferred = e;
    changed();
  });
  window.addEventListener("appinstalled", function () {
    installed = true;
    deferred = null;
    changed();
  });

  function install() {
    if (!deferred) return Promise.resolve("unavailable");
    var e = deferred;
    deferred = null;
    e.prompt();
    return e.userChoice.then(function (c) {
      changed();
      return c.outcome;
    });
  }

  function dismiss() {
    try {
      window.localStorage.setItem(opts.storageKey, "1");
    } catch (e) {
      /* private mode: hide only for this page */
      installed = true;
    }
    changed();
  }

  // --- Default rendering ------------------------------------------------------

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text) n.textContent = text;
    return n;
  }

  function shareIcon() {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("class", "ih-share");
    svg.setAttribute("viewBox", "0 0 16 18");
    svg.setAttribute("width", "14");
    svg.setAttribute("height", "16");
    svg.setAttribute("aria-label", opts.lang === "en" ? "Share" : "Teilen");
    svg.setAttribute("role", "img");
    for (var i = 0; i < SHARE_PATH.length; i++) {
      var p = document.createElementNS(ns, "path");
      p.setAttribute("d", SHARE_PATH[i]);
      svg.appendChild(p);
    }
    return svg;
  }

  // Replaces {share} with the icon; the rest stays text.
  function step(text) {
    var li = el("li");
    var parts = text.split("{share}");
    for (var i = 0; i < parts.length; i++) {
      if (i > 0) li.appendChild(shareIcon());
      li.appendChild(document.createTextNode(parts[i]));
    }
    return li;
  }

  function render(target) {
    var s = state();
    var t = TEXTS[opts.lang] || TEXTS.de;
    target.textContent = "";
    target.hidden = s === "none";
    if (s === "none") return;

    var card = el("div", "ih-card");
    card.appendChild(el("h2", "ih-title", t.title.replace("{app}", opts.app)));
    card.appendChild(el("p", "ih-body", s === "button" ? t.bodyButton : t.bodyIos));
    if (s === "button") {
      var b = el("button", "ih-install", t.button);
      b.type = "button";
      b.addEventListener("click", install);
      card.appendChild(b);
    } else {
      var steps = s === "ios-chrome" ? t.stepsChrome : s === "ios-safari-menu" ? t.stepsSafariMenu : t.stepsSafari;
      var ol = el("ol", "ih-steps");
      for (var i = 0; i < steps.length; i++) ol.appendChild(step(steps[i]));
      card.appendChild(ol);
    }
    var x = el("button", "ih-dismiss", "×");
    x.type = "button";
    x.setAttribute("aria-label", t.dismiss);
    x.addEventListener("click", dismiss);
    card.appendChild(x);
    target.appendChild(card);
  }

  function mount(target) {
    if (!target) return;
    mounts = mounts.filter(function (n) { return n.isConnected; });   // the page may have replaced earlier ones
    mounts.push(target);
    render(target);
  }

  function init(o) {
    o = o || {};
    for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) opts[k] = o[k];
    changed();
  }

  window.InstallHint = { init: init, state: state, install: install, dismiss: dismiss, mount: mount, texts: TEXTS };

  // Auto start: <div data-install-hint data-app="Name" data-lang="en"></div>
  function auto() {
    var nodes = document.querySelectorAll("[data-install-hint]");
    if (!nodes.length) return;
    var first = nodes[0];
    if (first.getAttribute("data-app")) opts.app = first.getAttribute("data-app");
    if (first.getAttribute("data-lang")) opts.lang = first.getAttribute("data-lang");
    for (var i = 0; i < nodes.length; i++) mount(nodes[i]);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", auto);
  else auto();
})();
