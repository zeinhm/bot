/* ZENITH service worker — installable PWA shell + web push.
   Network-first for pages (live data stays fresh), cache-first for static assets.
   Cache version: bump CACHE_VERSION to invalidate everything on the next visit. */
const CACHE_VERSION = 'v2';
const STATIC_CACHE = 'zenith-static-' + CACHE_VERSION;
const PAGE_CACHE = 'zenith-pages-' + CACHE_VERSION;

// Minimal precache: app icons + manifest. CSS/JS are query-versioned, so they're
// runtime-cached instead (avoids precaching a stale exact URL).
const PRECACHE = [
  '/static/manifest.webmanifest',
  '/static/icons/icon-192.png?v=2',
  '/static/icons/icon-512.png?v=2',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((c) => c.addAll(PRECACHE)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => k !== STATIC_CACHE && k !== PAGE_CACHE).map((k) => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

function isStatic(url) {
  return url.pathname.startsWith('/static/') || url.pathname.startsWith('/landing/');
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Never intercept live/data/auth endpoints — let them hit the network directly.
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/ws') ||
    url.pathname.startsWith('/auth/') ||
    url.pathname.startsWith('/bot/') ||
    url.pathname.startsWith('/push/') ||
    url.pathname === '/sw.js'
  ) return;

  // Static assets: cache-first, fill cache on miss.
  if (isStatic(url)) {
    event.respondWith(
      caches.match(req).then((hit) =>
        hit || fetch(req).then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(STATIC_CACHE).then((c) => c.put(req, copy));
          }
          return res;
        })
      )
    );
    return;
  }

  // Page navigations: network-first so live data is fresh; fall back to cache offline.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(PAGE_CACHE).then((c) => c.put(req, copy));
        }
        return res;
      }).catch(() =>
        caches.match(req).then((hit) => hit || caches.match('/dashboard'))
      )
    );
  }
});

// --- Web Push (backend wired in Phase 5) ---
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { body: event.data && event.data.text() }; }
  const title = data.title || 'ZENITH';
  const options = {
    body: data.body || '',
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/icon-192.png',
    tag: data.tag || undefined,
    data: { url: data.url || '/dashboard' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/dashboard';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if (w.url.includes(target) && 'focus' in w) return w.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(target);
    })
  );
});
