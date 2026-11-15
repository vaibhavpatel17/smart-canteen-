/* Smart Canteen service worker. It exists so the phone can install the app (Add to Home Screen) and so the screen still opens
   when the signal drops. It never touches /api: orders and payments always go to the real server, or fail loudly. */
const CACHE = 'canteen-shell-v1';

self.addEventListener('install', e => { self.skipWaiting(); });

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/') || url.pathname === '/sw.js' || url.pathname === '/healthz') return;
  // pages and files: the network first (so a new version shows up straight away), the last good copy when offline
  e.respondWith(fetch(req).then(res => {
    if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
    return res;
  }).catch(() => caches.match(req).then(hit => hit || new Response('You are offline. Check your connection and open the app again.', { status: 503, headers: { 'Content-Type': 'text/plain; charset=utf-8' } }))));
});
