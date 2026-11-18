const CACHE = 'jc-tj-v9';
const ASSETS = ['./','./index.html','./manifest.webmanifest','./icon-192.png','./icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;

  // 赛程/历史数据必须拿最新；页面用 ?t=时间戳 防缓存，
  // 所以离线缓存统一按「去掉 query 的 URL」作 key，断网时才能命中。
  const url = e.request.url;
  const isData = url.indexOf('data.enc') !== -1 || url.indexOf('history.enc') !== -1;
  if (isData) {
    const key = new Request(url.split('?')[0]);
    e.respondWith(fetch(e.request).then(r => {
      const copy = r.clone();
      caches.open(CACHE).then(c => c.put(key, copy));
      return r;
    }).catch(() => caches.match(key)));
    return;
  }

  if (e.request.mode === 'navigate') {
    e.respondWith(fetch(e.request, {cache: 'no-store'}).then(r => {
      const copy = r.clone();
      caches.open(CACHE).then(c => c.put('./index.html', copy));
      return r;
    }).catch(() => caches.match('./index.html')));
    return;
  }
  e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request).then(r => {
    const copy = r.clone();
    caches.open(CACHE).then(c => c.put(e.request, copy));
    return r;
  })));
});
