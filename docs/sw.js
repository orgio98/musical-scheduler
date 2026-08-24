// 커튼콜 웹앱용 서비스 워커
// 목표: 코드를 수정할 때마다 브라우저가 옛날 버전을 캐시로 계속 보여주는 문제를 없앤다.
// 방법: 아무것도 캐시하지 않고, 모든 요청을 항상 네트워크에서 새로 받아온다.

self.addEventListener("install", (e) => {
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    // 예전에 다른 서비스 워커가 남겨둔 캐시가 있다면 전부 정리
    const keys = await caches.keys();
    await Promise.all(keys.map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  event.respondWith(
    fetch(event.request, { cache: "no-store" }).catch(() =>
      caches.match(event.request)
    )
  );
});
