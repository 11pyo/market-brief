/* =========================================================================
   서비스워커 — 정적 사이트 v2

   캐시 이름에 빌드 ID를 포함시켜 배포마다 새 캐시를 쓰고 옛 캐시를 지운다.
   `f6ed0d0401`는 빌더(mbrief/site/builder.py)가 빌드 시점에 치환한다.

   전략
     - HTML(navigate)  : 네트워크 우선 → 실패 시 캐시 → 그래도 없으면 시작 페이지
     - data/*.json     : 네트워크 우선(5초 타임아웃) → 캐시
     - css/js/icons    : stale-while-revalidate (즉시 캐시 응답 + 백그라운드 갱신)
     - 그 외/교차 출처  : 개입하지 않음

   경로 규칙: 모든 URL은 서비스워커 스크립트 위치(사이트 루트) 기준 상대 경로다.
   GitHub 프로젝트 페이지(/<repo>/) 하위 배포에서도 그대로 동작한다.
   ========================================================================= */
'use strict';

var BUILD_ID = 'f6ed0d0401';
var CACHE_NAME = 'mbrief-' + BUILD_ID;
var DATA_TIMEOUT_MS = 5000;

// 상대 경로 — SW 스크립트 URL 기준으로 해석된다
var PRECACHE = [
  './',
  './index.html',
  './css/style.css',
  './js/app.js',
  './manifest.json',
  './icons/icon-192.png',
];

// 스코프 루트(오프라인 폴백 대상)
var SCOPE_ROOT = new URL('./', self.location.href).href;

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      // 개별 실패가 설치 전체를 막지 않게 각각 처리한다
      return Promise.all(PRECACHE.map(function (url) {
        return cache.add(url).catch(function (err) {
          console.warn('[sw] precache 건너뜀:', url, err && err.name);
          return null;
        });
      }));
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (key) {
        // [SECURE] 이 사이트가 만든 캐시만 삭제 - 동일 오리진의 타 앱 캐시 보호 (Category 2)
        if (key !== CACHE_NAME && key.indexOf('mbrief-') === 0) return caches.delete(key);
        return null;
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

function putInCache(request, response) {
  if (!response || response.status !== 200 || response.type !== 'basic') return response;
  var copy = response.clone();
  caches.open(CACHE_NAME).then(function (cache) {
    return cache.put(request, copy);
  }).catch(function (err) {
    console.warn('[sw] 캐시 저장 실패:', err && err.name);
  });
  return response;
}

function networkFirst(request, timeoutMs) {
  var network = fetch(request).then(function (response) { return putInCache(request, response); });
  if (!timeoutMs) {
    return network.catch(function () { return caches.match(request); });
  }
  var timeout = new Promise(function (_, reject) {
    self.setTimeout(function () { reject(new Error('timeout')); }, timeoutMs);
  });
  return Promise.race([network, timeout]).catch(function () {
    return caches.match(request).then(function (cached) {
      // 캐시도 없으면 네트워크 응답을 끝까지 기다린다 (타임아웃은 캐시 우선 전환용일 뿐)
      return cached || network;
    });
  });
}

function staleWhileRevalidate(request) {
  return caches.match(request).then(function (cached) {
    var network = fetch(request)
      .then(function (response) { return putInCache(request, response); })
      .catch(function (err) {
        console.warn('[sw] 재검증 실패:', err && err.name);
        return cached;
      });
    return cached || network;
  });
}

self.addEventListener('fetch', function (event) {
  var request = event.request;
  if (request.method !== 'GET') return;

  var url;
  try { url = new URL(request.url); }
  catch (err) { console.warn('[sw] URL 파싱 실패:', err && err.name); return; }

  // [SECURE] 교차 출처 요청은 가로채지 않는다 - 외부 응답 캐시 오염 방지 (Category 1)
  if (url.origin !== self.location.origin) return;
  // 스코프 밖(같은 오리진의 다른 앱)도 건드리지 않는다
  if (url.href.indexOf(SCOPE_ROOT) !== 0) return;

  if (request.mode === 'navigate') {
    event.respondWith(
      networkFirst(request, 0).then(function (response) {
        return response || caches.match(SCOPE_ROOT + 'index.html') || caches.match('./');
      })
    );
    return;
  }

  var path = url.pathname;
  if (path.indexOf('/data/') >= 0 && path.slice(-5) === '.json') {
    event.respondWith(networkFirst(request, DATA_TIMEOUT_MS));
    return;
  }

  if (/\.(?:css|js|png|svg|webp|woff2?)$/.test(path)) {
    event.respondWith(staleWhileRevalidate(request));
  }
});

/* --------------------------------------------------
   Security Checklist
   Applied:
     - Improper Authorization: 교차 출처 및 스코프 밖 요청은 가로채지 않는다
     - Incorrect Permission on Critical Resource: 'mbrief-' 접두사 캐시만 삭제해 타 앱 캐시를 보호
     - Improper Exception Handling: 모든 catch가 경고 로깅 또는 폴백을 수행한다 (빈 catch 없음)
     - Unencrypted Sensitive Data: 개인 데이터를 캐시하지 않는다. 공개 정적 산출물만 대상
     - Race Condition: 캐시 이름에 빌드 ID를 포함해 배포 간 캐시 혼선을 제거
   Not Applied:
     - [WARN] POST/PUT 등 비-GET 요청은 처리하지 않는다 (정적 사이트라 필요 없음).
     - [WARN] 캐시 용량 상한이 없다. 아카이브가 매우 커지면 오래된 항목 정리 로직을 추가할 것.
   -------------------------------------------------- */
