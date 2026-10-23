// Passkey sign-in and registration for the icloud-mcp dashboard. Served from the server itself (CSP script-src 'self').
// Buttons carry data-passkey="signin" or "register"; everything else on the pages works without this file.
(function () {
  "use strict";
  var b64 = {
    toBuf: function (s) {
      s = s.replace(/-/g, "+").replace(/_/g, "/");
      while (s.length % 4) s += "=";
      var bin = atob(s), out = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
      return out.buffer;
    },
    fromBuf: function (buf) {
      var bytes = new Uint8Array(buf), bin = "";
      for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
      return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    }
  };

  function requestOptions(o) {
    if (PublicKeyCredential.parseRequestOptionsFromJSON) return PublicKeyCredential.parseRequestOptionsFromJSON(o);
    o.challenge = b64.toBuf(o.challenge);
    (o.allowCredentials || []).forEach(function (c) { c.id = b64.toBuf(c.id); });
    return o;
  }

  function creationOptions(o) {
    if (PublicKeyCredential.parseCreationOptionsFromJSON) return PublicKeyCredential.parseCreationOptionsFromJSON(o);
    o.challenge = b64.toBuf(o.challenge);
    o.user.id = b64.toBuf(o.user.id);
    (o.excludeCredentials || []).forEach(function (c) { c.id = b64.toBuf(c.id); });
    return o;
  }

  function credentialJSON(c) {
    if (typeof c.toJSON === "function") return c.toJSON();
    var r = c.response, out = { id: c.id, rawId: b64.fromBuf(c.rawId), type: c.type, clientExtensionResults: c.getClientExtensionResults(),
      authenticatorAttachment: c.authenticatorAttachment || null, response: { clientDataJSON: b64.fromBuf(r.clientDataJSON) } };
    if (r.attestationObject) {
      out.response.attestationObject = b64.fromBuf(r.attestationObject);
      if (r.getTransports) out.response.transports = r.getTransports();
    } else {
      out.response.authenticatorData = b64.fromBuf(r.authenticatorData);
      out.response.signature = b64.fromBuf(r.signature);
      if (r.userHandle) out.response.userHandle = b64.fromBuf(r.userHandle);
    }
    return out;
  }

  function post(url, body, csrf) {
    var headers = { "Content-Type": "application/json" };
    if (csrf) headers["X-CSRF"] = csrf;
    return fetch(url, { method: "POST", headers: headers, credentials: "same-origin", body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { j._status = r.status; return j; }); });
  }

  function showError(btn, text) {
    var box = btn.parentNode.querySelector("[data-passkey-error]") || document.querySelector("[data-passkey-error]");
    if (box) { box.textContent = text; box.hidden = false; }
  }

  function run(btn) {
    var purpose = btn.getAttribute("data-passkey"), csrf = btn.getAttribute("data-csrf") || "";
    var next = btn.getAttribute("data-next") || "/dashboard";
    var nameField = btn.getAttribute("data-name-from"), name = nameField ? (document.getElementById(nameField) || {}).value || "" : "";
    btn.disabled = true;
    post("/dashboard/passkey/options", { purpose: purpose }, csrf).then(function (opts) {
      if (opts.error === "confirm" && opts.confirm) { window.location = opts.confirm; return null; }
      if (opts.error) throw new Error(opts.error);
      var call = purpose === "register"
        ? navigator.credentials.create({ publicKey: creationOptions(opts.publicKey) })
        : navigator.credentials.get({ publicKey: requestOptions(opts.publicKey) });
      return call.then(function (cred) {
        return post("/dashboard/passkey/verify", { purpose: purpose, ticket: opts.ticket, credential: credentialJSON(cred), next: next, name: name }, csrf);
      });
    }).then(function (res) {
      if (!res) return;
      if (res.error) throw new Error(res.error);
      window.location = res.next || "/dashboard";
    }).catch(function (e) {
      btn.disabled = false;
      var msg = e && e.name === "NotAllowedError" ? "The passkey prompt was closed or timed out. Try again." : (e && e.message) || "That did not work.";
      showError(btn, msg);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var buttons = document.querySelectorAll("[data-passkey]");
    if (!window.PublicKeyCredential) {
      for (var i = 0; i < buttons.length; i++) {
        buttons[i].disabled = true;
        showError(buttons[i], "This browser cannot use passkeys here. Use the owner password.");
      }
      return;
    }
    for (var j = 0; j < buttons.length; j++) {
      buttons[j].addEventListener("click", function (ev) { run(ev.currentTarget); });
    }
  });
})();
