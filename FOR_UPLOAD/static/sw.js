self.addEventListener("push", function(event) {
    if (event.data) {
        const text = event.data.text();
        event.waitUntil(
            self.registration.showNotification("Обновление статуса заказа", {
                body: text,
                icon: "/static/icon.png", // add an icon if you want
            })
        );
    }
});

self.addEventListener("notificationclick", function(event) {
    event.notification.close();
    // Focus or open a new window when clicked
    event.waitUntil(
        clients.matchAll({ type: "window" }).then(windowClients => {
            for (let client of windowClients) {
                if (client.url === "/" && "focus" in client) {
                    return client.focus();
                }
            }
            if (clients.openWindow) {
                return clients.openWindow("/");
            }
        })
    );
});
