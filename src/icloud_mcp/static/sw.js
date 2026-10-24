// Service worker of the icloud-mcp dashboard. It lets browsers install the dashboard as an app and caches nothing:
// every page and every request goes to the server as before, so nothing from your mailbox is stored by the browser.
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });
