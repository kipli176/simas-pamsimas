/* Service Worker SIMAS — cache aset & shell aplikasi untuk dukungan offline petugas */
const CACHE = 'simas-v1';

const ASSETS = [
  '/',
  '/static/vendor/bootstrap.min.css',
  '/static/vendor/bootstrap.bundle.min.js',
  '/static/vendor/bootstrap-icons.css',
  '/static/vendor/fonts/bootstrap-icons.woff2',
  '/static/vendor/fonts/bootstrap-icons.woff',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/js/pwa.js'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  // JANGAN cache halaman admin/aksi/foto (data sensitif)
  if (url.pathname.startsWith('/admin') ||
      url.pathname.startsWith('/api/') ||
      url.pathname.startsWith('/pelanggan') ||
      url.pathname.startsWith('/tagihan') ||
      url.pathname.startsWith('/tarif') ||
      url.pathname.startsWith('/laporan') ||
      url.pathname.startsWith('/pengaturan') ||
      url.pathname.startsWith('/uploads/') ||
      url.pathname.startsWith('/petugas/masuk') ||
      url.pathname.startsWith('/petugas/keluar')) {
    return;
  }

  // Aset statis: cache-first
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(req).then((cached) => {
        if (cached) return cached;
        return fetch(req).then((res) => {
          const clone = res.clone();
          caches.open(CACHE).then((cache) => cache.put(req, clone));
          return res;
        });
      })
    );
    return;
  }

  // Halaman (login & dashboard petugas): network-first, fallback ke cache saat offline
  event.respondWith(
    fetch(req)
      .then((res) => {
        const clone = res.clone();
        caches.open(CACHE).then((cache) => cache.put(req, clone));
        return res;
      })
      .catch(() => caches.match(req).then((cached) => cached || caches.match('/')))
  );
});
