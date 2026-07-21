const CACHE_NAME = "introshow-crm-v2";
const STATIC_ASSETS = [
    "/static/css/dashboard.css",
    "/static/img/logo.jpg",
    "/static/img/icon-192.png",
    "/static/img/icon-512.png",
    "/static/manifest.json",
];

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
    );
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
        )
    );
    self.clients.claim();
});

// Стратегия: сеть в приоритете, кэш — офлайн-запас (только GET и только статика/страницы)
self.addEventListener("fetch", (event) => {
    const req = event.request;
    if (req.method !== "GET") return;
    const url = new URL(req.url);
    if (url.pathname.startsWith("/api/")) return; // API всегда из сети

    event.respondWith(
        fetch(req)
            .then((res) => {
                if (res.ok && url.pathname.startsWith("/static/")) {
                    const clone = res.clone();
                    caches.open(CACHE_NAME).then((cache) => cache.put(req, clone));
                }
                return res;
            })
            .catch(() => caches.match(req))
    );
});

// Web Push уведомления
self.addEventListener("push", function (event) {
    if (event.data) {
        const text = event.data.text();
        event.waitUntil(
            self.registration.showNotification("Intro Show CRM", {
                body: text,
                icon: "/static/img/icon-192.png",
                badge: "/static/img/icon-192.png",
            })
        );
    }
});

self.addEventListener("notificationclick", function (event) {
    event.notification.close();
    event.waitUntil(
        clients.matchAll({ type: "window" }).then((windowClients) => {
            for (let client of windowClients) {
                if ("focus" in client) return client.focus();
            }
            if (clients.openWindow) return clients.openWindow("/");
        })
    );
});
