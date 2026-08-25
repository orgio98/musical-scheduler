// 커튼콜 웹앱용 서비스 워커
// 목표 1: 코드를 수정해도 브라우저가 옛날 버전을 계속 보여주는 문제를 없앤다.
// 목표 2: 새 버전이 감지되면 곧바로 활성화해 화면이 자동으로 최신 코드로 갱신되게 한다.

self.addEventListener("install", () => {
  self.skipWaiting();          // 설치되자마자 대기 없이 활성화 준비
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    // 예전 캐시가 남아 있다면 전부 정리
    const keys = await caches.keys();
    await Promise.all(keys.map((k) => caches.delete(k)));
    await self.clients.claim();  // 열려 있는 탭의 제어권을 즉시 가져온다
  })());
});

// 페이지가 "새 버전이니 바로 넘어가라"고 보내는 신호를 처리
self.addEventListener("message", (e) => {
  if (e.data && e.data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});

// 아무것도 캐시하지 않고 항상 네트워크에서 새로 받아온다
self.addEventListener("fetch", (event) => {
  event.respondWith(
    fetch(event.request, { cache: "no-store" }).catch(() =>
      caches.match(event.request)
    )
  );
});
