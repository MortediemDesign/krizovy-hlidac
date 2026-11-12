// Service worker: aplikace se otevře i bez signálu a ukáže poslední známý stav.
// Cesty jsou relativní, takže funguje na vlastní doméně i na GitHub Pages (/repo/).
const CACHE = "hlidac-v2";
const SHELL = [
  "./", "index.html", "manifest.webmanifest", "icon.svg", "icon-180.png", "icon-192.png",
  "fonts/barlow-latin-400-normal.woff2", "fonts/barlow-latin-ext-400-normal.woff2",
  "fonts/barlow-latin-600-normal.woff2", "fonts/barlow-latin-ext-600-normal.woff2",
  "fonts/barlow-condensed-latin-700-normal.woff2", "fonts/barlow-condensed-latin-ext-700-normal.woff2",
];
const scoped = p => new URL(p, self.registration.scope).href;

self.addEventListener("install", e => {
  // po jednom – když jeden soubor selže, ostatní se uloží
  e.waitUntil(caches.open(CACHE).then(c => Promise.all(SHELL.map(u => c.add(u).catch(() => {})))));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;

  // stav a stránka: nejdřív síť, při výpadku poslední uložená verze
  const name = url.pathname.split("/").pop();
  let key = null;
  if (name === "status.json" || name === "status.enc.json") key = scoped(name);
  else if (e.request.mode === "navigate") key = scoped("index.html");
  if (key) {
    e.respondWith(
      fetch(e.request).then(r => {
        if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(key, copy)); }
        return r;
      }).catch(() => caches.match(key).then(hit => hit || Response.error()))
    );
    return;
  }

  // písma, ikony: z mezipaměti
  e.respondWith(caches.match(e.request, {ignoreSearch: true}).then(hit => hit || fetch(e.request)));
});
