// Service worker — makes the app installable and loads the shell offline.
// Strategy: network-FIRST for the app shell/HTML so reps always get the latest
// version when online (a stale cache-first SW is why old builds got "stuck" on phones).
// Static assets (icons/manifest) are cache-first for speed. API calls never cached.
const CACHE = 'ix-sales-v5';
const SHELL = ['/', '/knock', '/qualify', '/manifest.webmanifest', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) return; // never cache API / writes

  // App code (HTML / navigations): network-first → always fresh online, cached fallback offline.
  const isShell = e.request.mode === 'navigate' || url.pathname === '/' || url.pathname.endsWith('.html');
  if (isShell) {
    e.respondWith(
      fetch(e.request).then(res => {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy)).catch(() => {});
        return res;
      }).catch(() => caches.match(e.request).then(hit => hit || caches.match('/index.html')))
    );
    return;
  }

  // Static assets (icons, manifest): cache-first for speed.
  e.respondWith(
    caches.match(e.request).then(hit => hit || fetch(e.request).then(res => {
      const copy = res.clone();
      caches.open(CACHE).then(c => c.put(e.request, copy)).catch(() => {});
      return res;
    }))
  );
});
