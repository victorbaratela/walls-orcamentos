const ESTATICO = 'walls-estatico-v1';
const PAGINAS = 'walls-paginas';
const BASICOS = [
  '/static/offline.html',
  '/static/app.js',
  '/static/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png'
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(ESTATICO).then(c => c.addAll(BASICOS)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(nomes => Promise.all(
      nomes.filter(n => n !== ESTATICO && n !== PAGINAS).map(n => caches.delete(n))
    ))
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (['/login', '/sair', '/sw.js'].includes(url.pathname)) return;

  // Arquivos fixos (ícones, scripts): usa a cópia guardada primeiro
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(caches.match(req).then(r => r || fetch(req)));
    return;
  }

  // Telas do sistema: internet primeiro, cópia guardada como reserva
  e.respondWith(
    fetch(req).then(resp => {
      if (resp.ok && !resp.redirected) {
        const copia = resp.clone();
        caches.open(PAGINAS).then(c => c.put(req, copia));
      }
      return resp;
    }).catch(() =>
      caches.match(req).then(r => r || caches.match('/static/offline.html'))
    )
  );
});