/* =========================================================================
   증시흐름 v2 — 정적 사이트 클라이언트 스크립트 (바닐라 ES2020, 빌드 스텝 없음)

   페이지 본문은 전부 빌드 시점에 서버(파이썬 Jinja2)가 렌더한다.
   이 파일이 하는 일은 "정적 HTML로는 불가능한 것"뿐이다.
     - 차트 모달 (Lightweight Charts, data/charts/*.json 지연 로드)
     - KRW/USD 표기 전환 (localStorage 기억)
     - 공유 버튼 (Web Share API → 클립보드 폴백)
     - 관심종목 렌즈 (본문 하이라이트, 로컬 전용)
     - PWA 안내 바 닫기 기억
     - 아카이브 언어 필터
     - 서비스워커 등록 및 갱신 토스트

   보안 규칙 (전 함수 공통)
     1. innerHTML에 들어가는 모든 동적 문자열은 escapeHtml()을 거친다.
     2. fetch 경로는 상대 경로만 쓴다 (GitHub Pages 하위 경로 대응 + SSRF 표면 없음).
     3. eval / new Function / innerHTML로 스크립트 삽입을 하지 않는다.
     4. 파일명에 들어가는 식별자는 정규식 화이트리스트로 검증한 뒤에만 사용한다.
   ========================================================================= */
(function () {
  'use strict';

  // ===== 공통 유틸 =====
  var HTML_ENTITIES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  // [SECURE] HTML 출력 인코딩 - XSS 방지 (Category 1)
  function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"']/g, function (ch) { return HTML_ENTITIES[ch]; });
  }

  // [SECURE] 정규식 메타문자 이스케이프 - 사용자 입력이 패턴으로 해석되는 것을 방지 (Category 1)
  function escapeRegExp(value) {
    return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  var body = document.body;
  // 상대 경로 기준점: index.html은 "./", archive/*.html은 "../"
  var BASE = (body && body.dataset.base) || './';
  var LANG = (body && body.dataset.lang) || 'ko';

  function label(key, fallback) {
    var value = body ? body.dataset[key] : '';
    return value || fallback || '';
  }

  function safeLocalGet(key) {
    // [SECURE] 프라이빗 모드 등에서 localStorage 접근이 예외를 던진다. 조용히 삼키지 않고 null 반환 (Category 4)
    try { return window.localStorage.getItem(key); }
    catch (err) { console.warn('[mbrief] localStorage 읽기 불가:', err && err.name); return null; }
  }

  function safeLocalSet(key, value) {
    try { window.localStorage.setItem(key, value); return true; }
    catch (err) { console.warn('[mbrief] localStorage 쓰기 불가:', err && err.name); return false; }
  }

  function showToast(message, kind) {
    var host = document.getElementById('toast-container');
    if (!host) return;
    var el = document.createElement('div');
    el.className = 'toast' + (kind ? ' toast--' + kind : '');
    // [SECURE] textContent 사용 - 마크업 해석 자체를 차단 (Category 1)
    el.textContent = String(message || '');
    host.appendChild(el);
    window.setTimeout(function () { el.remove(); }, 4000);
  }

  // ===== 숫자 포맷 (파이썬 builder._fmt_price 와 동일 규칙) =====
  function formatPrice(value, name) {
    if (value === null || value === undefined || !isFinite(value)) return 'N/A';
    var lower = String(name || '').toLowerCase();
    if (lower.indexOf('yield') >= 0 || lower.indexOf('vix') >= 0 ||
        lower.indexOf('dxy') >= 0 || lower.indexOf('eur/usd') >= 0) {
      return value.toFixed(2);
    }
    if (value >= 1000000) return Math.round(value).toLocaleString('en-US');
    if (value >= 100) return value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return value.toFixed(2);
  }

  function formatSigned(value) {
    if (value === null || value === undefined || !isFinite(value)) return '';
    var sign = value >= 0 ? '+' : '-';
    return sign + Math.abs(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  // =======================================================================
  // 1. 통화 전환 (USD 기본 = 서버 렌더 값, KRW는 USD/KRW 시세로 클라이언트 환산)
  // =======================================================================
  var CURRENCY_KEY = 'mbrief_currency';

  function applyCurrency(currency) {
    var grid = document.getElementById('market-cards');
    if (!grid) return;
    var rate = parseFloat(grid.dataset.krwRate);
    var toKrw = currency === 'KRW' && isFinite(rate) && rate > 0;

    var cards = grid.querySelectorAll('[data-usd]');
    for (var i = 0; i < cards.length; i++) {
      var card = cards[i];
      var usd = parseFloat(card.dataset.usd);
      var usdChange = parseFloat(card.dataset.usdChange);
      if (!isFinite(usd)) continue;
      var name = card.dataset.name || '';
      var price = toKrw ? usd * rate : usd;
      var change = isFinite(usdChange) ? (toKrw ? usdChange * rate : usdChange) : null;

      var priceEl = card.querySelector('[data-role="price"]');
      var changeEl = card.querySelector('[data-role="change"]');
      if (priceEl) priceEl.textContent = formatPrice(price, name) + (toKrw ? ' ₩' : '');
      if (changeEl) {
        var pctEl = changeEl.querySelector('.card-pct');
        var pctText = pctEl ? pctEl.textContent : '';
        // [SECURE] 재조립도 textContent 기반 - innerHTML 미사용 (Category 1)
        changeEl.textContent = formatSigned(change) + ' ';
        if (pctEl) { changeEl.appendChild(pctEl); pctEl.textContent = pctText; }
      }
    }
  }

  function initCurrency() {
    var buttons = document.querySelectorAll('.cur-btn');
    if (!buttons.length) return;
    var saved = safeLocalGet(CURRENCY_KEY);
    var current = saved === 'KRW' ? 'KRW' : 'USD';

    function select(currency) {
      current = currency === 'KRW' ? 'KRW' : 'USD';
      for (var i = 0; i < buttons.length; i++) {
        var active = buttons[i].dataset.currency === current;
        buttons[i].classList.toggle('is-active', active);
        buttons[i].setAttribute('aria-pressed', active ? 'true' : 'false');
      }
      safeLocalSet(CURRENCY_KEY, current);
      applyCurrency(current);
    }

    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener('click', function (e) { select(e.currentTarget.dataset.currency); });
    }
    select(current);
  }

  // =======================================================================
  // 2. 차트 모달
  // =======================================================================
  // [SECURE] 차트 키 화이트리스트 - Path Traversal 및 임의 경로 fetch 방지 (Category 1)
  var CHART_KEY_RE = /^[a-z0-9]+_(1d|5d|1mo)$/;
  var chartInstance = null;
  var chartSlug = null;
  var lastFocused = null;

  function chartState(text) {
    var container = document.getElementById('chart-container');
    if (!container) return;
    container.textContent = '';
    var el = document.createElement('div');
    el.className = 'chart-state';
    el.textContent = text;
    container.appendChild(el);
  }

  function destroyChart() {
    if (chartInstance) {
      try { chartInstance.remove(); }
      catch (err) { console.warn('[mbrief] 차트 해제 실패:', err && err.name); }
      chartInstance = null;
    }
  }

  function loadChart(slug, period) {
    var key = slug + '_' + period;
    if (!CHART_KEY_RE.test(key)) { chartState(label('lChartError', 'Invalid chart')); return; }

    chartState(label('lChartLoading', 'Loading...'));
    destroyChart();

    // [SECURE] 상대 경로 고정 - 외부 호스트 요청 불가 (Category 1: SSRF/Open Redirect 표면 제거)
    fetch(BASE + 'data/charts/' + key + '.json', { credentials: 'omit' })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (candles) {
        var container = document.getElementById('chart-container');
        if (!container) return;
        if (!Array.isArray(candles) || candles.length === 0) {
          chartState(label('lChartEmpty', 'No data'));
          return;
        }
        if (typeof window.LightweightCharts === 'undefined') {
          chartState(label('lChartError', 'Chart library unavailable'));
          return;
        }
        container.textContent = '';
        chartInstance = window.LightweightCharts.createChart(container, {
          autoSize: true,
          height: 320,
          layout: { background: { color: '#111827' }, textColor: '#94a3b8', fontSize: 12 },
          grid: {
            vertLines: { color: 'rgba(99, 102, 241, 0.08)' },
            horzLines: { color: 'rgba(99, 102, 241, 0.08)' }
          },
          crosshair: { mode: 1 },
          rightPriceScale: { borderColor: 'rgba(99, 102, 241, 0.2)' },
          timeScale: {
            borderColor: 'rgba(99, 102, 241, 0.2)',
            timeVisible: period !== '1mo',
            secondsVisible: false
          }
        });

        if (period === '1d') {
          var candleSeries = chartInstance.addCandlestickSeries({
            upColor: '#10b981', downColor: '#ef4444',
            borderUpColor: '#10b981', borderDownColor: '#ef4444',
            wickUpColor: '#10b981', wickDownColor: '#ef4444'
          });
          candleSeries.setData(candles);
        } else {
          var areaSeries = chartInstance.addAreaSeries({
            lineColor: '#6366f1',
            topColor: 'rgba(99, 102, 241, 0.25)',
            bottomColor: 'rgba(99, 102, 241, 0)',
            lineWidth: 2,
            crosshairMarkerVisible: true,
            crosshairMarkerRadius: 4
          });
          areaSeries.setData(candles.map(function (c) { return { time: c.time, value: c.close }; }));
        }
        chartInstance.timeScale().fitContent();
      })
      .catch(function (err) {
        // [SECURE] 예외 원문을 화면에 렌더하지 않는다 - 내부 정보 노출 및 XSS 방지 (Category 1/4)
        console.error('[mbrief] 차트 로드 실패:', err && err.message);
        chartState(label('lChartError', 'Failed to load chart'));
      });
  }

  function setPeriod(period) {
    var tabs = document.querySelectorAll('.chart-tab');
    for (var i = 0; i < tabs.length; i++) {
      var active = tabs[i].dataset.period === period;
      tabs[i].classList.toggle('is-active', active);
      tabs[i].setAttribute('aria-selected', active ? 'true' : 'false');
    }
    if (chartSlug) loadChart(chartSlug, period);
  }

  function openChart(slug, title) {
    var modal = document.getElementById('chart-modal');
    if (!modal || !CHART_KEY_RE.test(slug + '_1d')) return;
    lastFocused = document.activeElement;
    chartSlug = slug;
    var heading = document.getElementById('chart-title');
    if (heading) heading.textContent = title || slug;
    modal.hidden = false;
    var closeBtn = document.getElementById('chart-close');
    if (closeBtn) closeBtn.focus();
    setPeriod('1d');
  }

  function closeChart() {
    var modal = document.getElementById('chart-modal');
    if (!modal) return;
    modal.hidden = true;
    destroyChart();
    chartSlug = null;
    if (lastFocused && typeof lastFocused.focus === 'function') lastFocused.focus();
  }

  function initChart() {
    var cards = document.querySelectorAll('[data-chart-slug]');
    for (var i = 0; i < cards.length; i++) {
      cards[i].addEventListener('click', function (e) {
        var el = e.currentTarget;
        openChart(el.dataset.chartSlug, el.dataset.name);
      });
    }
    var tabs = document.querySelectorAll('.chart-tab');
    for (var j = 0; j < tabs.length; j++) {
      tabs[j].addEventListener('click', function (e) { setPeriod(e.currentTarget.dataset.period); });
    }
    var closeBtn = document.getElementById('chart-close');
    if (closeBtn) closeBtn.addEventListener('click', closeChart);
    var modal = document.getElementById('chart-modal');
    if (modal) modal.addEventListener('click', function (e) { if (e.target === modal) closeChart(); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && modal && !modal.hidden) closeChart();
    });
  }

  // =======================================================================
  // 3. 공유
  // =======================================================================
  function initShare() {
    var btn = document.getElementById('share-btn');
    if (!btn) return;
    btn.addEventListener('click', function () {
      var url = window.location.href;
      var title = btn.dataset.shareTitle || document.title;
      if (navigator.share) {
        navigator.share({ title: title, url: url }).catch(function (err) {
          if (err && err.name !== 'AbortError') console.warn('[mbrief] 공유 취소/실패:', err.name);
        });
        return;
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(function () {
          showToast(label('lShareCopied', 'Copied'), 'success');
        }).catch(function (err) {
          console.warn('[mbrief] 클립보드 복사 실패:', err && err.name);
          window.prompt(title, url);
        });
        return;
      }
      window.prompt(title, url);
    });
  }

  // =======================================================================
  // 4. 관심종목 렌즈 (완전 클라이언트 사이드, 서버 전송 없음)
  // =======================================================================
  var WATCH_KEY = 'mbrief_watchlist';
  var MAX_TOKENS = 12;
  var activeMarks = [];

  function clearMarks() {
    for (var i = 0; i < activeMarks.length; i++) {
      var mark = activeMarks[i];
      var parent = mark.parentNode;
      if (!parent) continue;
      parent.replaceChild(document.createTextNode(mark.textContent), mark);
      parent.normalize();
    }
    activeMarks = [];
  }

  function parseTokens(raw) {
    var parts = String(raw || '').split(',');
    var tokens = [];
    for (var i = 0; i < parts.length && tokens.length < MAX_TOKENS; i++) {
      var token = parts[i].trim();
      if (token.length >= 2 && token.length <= 32) tokens.push(token);
    }
    return tokens;
  }

  function highlight(root, tokens) {
    if (!root || !tokens.length) return 0;
    var pattern = new RegExp('(' + tokens.map(escapeRegExp).join('|') + ')', 'gi');
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        var tag = node.parentNode && node.parentNode.nodeName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'MARK') return NodeFilter.FILTER_REJECT;
        return pattern.test(node.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }
    });
    var targets = [];
    var node;
    while ((node = walker.nextNode())) targets.push(node);

    var count = 0;
    for (var i = 0; i < targets.length; i++) {
      var text = targets[i].nodeValue;
      var frag = document.createDocumentFragment();
      var cursor = 0;
      pattern.lastIndex = 0;
      var match;
      while ((match = pattern.exec(text)) !== null) {
        if (match.index > cursor) frag.appendChild(document.createTextNode(text.slice(cursor, match.index)));
        var mark = document.createElement('mark');
        // [SECURE] 매치 문자열은 textContent로만 삽입 - XSS 방지 (Category 1)
        mark.textContent = match[0];
        frag.appendChild(mark);
        activeMarks.push(mark);
        cursor = match.index + match[0].length;
        count += 1;
        if (match[0].length === 0) pattern.lastIndex += 1;
      }
      if (cursor < text.length) frag.appendChild(document.createTextNode(text.slice(cursor)));
      if (targets[i].parentNode) targets[i].parentNode.replaceChild(frag, targets[i]);
    }
    return count;
  }

  function renderLensResult(tokens, count) {
    var out = document.getElementById('lens-result');
    if (!out) return;
    out.textContent = '';
    if (!tokens.length) return;

    if (count === 0) {
      out.textContent = label('lWatchNone', 'No matches');
      return;
    }

    var pattern = new RegExp(tokens.map(escapeRegExp).join('|'), 'i');
    var trades = [];
    var sectors = [];
    var lensNodes = document.querySelectorAll('[data-lens]');
    for (var i = 0; i < lensNodes.length; i++) {
      var el = lensNodes[i];
      var hay = (el.dataset.lens || '') + ' ' + (el.textContent || '');
      if (!pattern.test(hay)) continue;
      var name = (el.dataset.lens || '').trim();
      if (el.classList.contains('trade-card')) trades.push(name);
      else sectors.push(name);
    }

    var summary = document.createElement('p');
    summary.textContent = label('lWatchCount', '{n} matches').replace('{n}', String(count));
    out.appendChild(summary);

    function appendGroup(titleKey, fallbackTitle, items) {
      if (!items.length) return;
      var title = document.createElement('p');
      title.className = 'lens-group-title';
      title.textContent = fallbackTitle;
      out.appendChild(title);
      var list = document.createElement('ul');
      for (var k = 0; k < items.length; k++) {
        var li = document.createElement('li');
        li.textContent = items[k];
        list.appendChild(li);
      }
      out.appendChild(list);
    }

    var lensRoot = document.getElementById('lens');
    appendGroup('trades', (lensRoot && lensRoot.dataset.lTrades) || 'Trade ideas', trades);
    appendGroup('sectors', (lensRoot && lensRoot.dataset.lSectors) || 'Sectors', sectors);
  }

  function applyLens(raw) {
    var tokens = parseTokens(raw);
    clearMarks();
    var count = highlight(document.getElementById('briefing-body'), tokens);
    renderLensResult(tokens, count);
  }

  function initLens() {
    var form = document.getElementById('lens-form');
    var input = document.getElementById('lens-input');
    var clearBtn = document.getElementById('lens-clear');
    if (!form || !input) return;

    var saved = safeLocalGet(WATCH_KEY);
    if (saved) {
      input.value = saved;
      var details = document.getElementById('lens');
      if (details) details.open = true;
      applyLens(saved);
    }

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var value = input.value.slice(0, 200);
      safeLocalSet(WATCH_KEY, value);
      applyLens(value);
    });

    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        input.value = '';
        safeLocalSet(WATCH_KEY, '');
        clearMarks();
        var out = document.getElementById('lens-result');
        if (out) out.textContent = '';
      });
    }
  }

  // =======================================================================
  // 5. PWA 안내 바
  // =======================================================================
  var PWA_KEY = 'mbrief_pwa_hint_dismissed';

  function initPwaHint() {
    var bar = document.getElementById('pwa-hint');
    if (!bar) return;
    var standalone = window.matchMedia && window.matchMedia('(display-mode: standalone)').matches;
    if (standalone || safeLocalGet(PWA_KEY) === '1') return;
    bar.hidden = false;
    var close = document.getElementById('pwa-hint-close');
    if (close) {
      close.addEventListener('click', function () {
        bar.hidden = true;
        safeLocalSet(PWA_KEY, '1');
      });
    }
  }

  // =======================================================================
  // 6. 아카이브 언어 필터
  // =======================================================================
  function initArchiveFilter() {
    var buttons = document.querySelectorAll('.arc-filter-btn');
    if (!buttons.length) return;
    var items = document.querySelectorAll('.arc-item');

    function apply(filter) {
      for (var i = 0; i < items.length; i++) {
        items[i].hidden = !(filter === 'all' || items[i].dataset.lang === filter);
      }
      var months = document.querySelectorAll('.arc-month');
      for (var m = 0; m < months.length; m++) {
        months[m].hidden = months[m].querySelectorAll('.arc-item:not([hidden])').length === 0;
      }
    }

    for (var b = 0; b < buttons.length; b++) {
      buttons[b].addEventListener('click', function (e) {
        var btn = e.currentTarget;
        for (var i = 0; i < buttons.length; i++) buttons[i].classList.toggle('is-active', buttons[i] === btn);
        apply(btn.dataset.filter || 'all');
      });
    }
  }

  // =======================================================================
  // 7. 서비스워커
  // =======================================================================
  function initServiceWorker() {
    if (!('serviceWorker' in navigator)) return;
    // [SECURE] 상대 경로 등록 - 하위 경로 배포(/repo/)에서도 스코프가 사이트 루트로 고정된다 (Category 1)
    navigator.serviceWorker.register(BASE + 'sw.js').then(function (reg) {
      reg.addEventListener('updatefound', function () {
        var incoming = reg.installing;
        if (!incoming) return;
        incoming.addEventListener('statechange', function () {
          if (incoming.state === 'installed' && navigator.serviceWorker.controller) {
            showToast(label('lSwUpdated', 'Update available. Reload.'), 'success');
          }
        });
      });
    }).catch(function (err) {
      console.warn('[mbrief] 서비스워커 등록 실패:', err && err.name);
    });
  }

  // ===== 부트스트랩 =====
  function boot() {
    initCurrency();
    initChart();
    initShare();
    initLens();
    initPwaHint();
    initArchiveFilter();
    initServiceWorker();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  // 테스트/디버깅 편의를 위한 최소 노출 (전역 오염 최소화)
  window.mbrief = { escapeHtml: escapeHtml, formatPrice: formatPrice, lang: LANG };
})();

/* --------------------------------------------------
   Security Checklist
   Applied:
     - XSS: 동적 문자열은 textContent 또는 escapeHtml()만 사용. innerHTML로 사용자 데이터 미삽입
     - Code Injection: eval / new Function / setTimeout(문자열) 미사용
     - Path Traversal: 차트 키를 /^[a-z0-9]+_(1d|5d|1mo)$/ 로 검증한 뒤에만 경로에 사용
     - SSRF / Open Redirect: fetch는 BASE(상대 경로) 하위로만 요청. 외부 URL을 코드에서 조립하지 않음
     - Error Message Information Exposure: 예외 원문은 console에만, 화면에는 일반 메시지
     - Improper Exception Handling: 빈 catch 없음. 모든 catch가 로깅 또는 폴백 동작을 수행
     - Infinite Loop: 관심종목 토큰 12개 상한, 정규식 zero-length 매치 시 lastIndex 강제 전진
     - Cookie Information Exposure: fetch에 credentials:'omit' 지정, 쿠키 미사용
   Not Applied:
     - [WARN] Lightweight Charts를 CDN에서 로드하며 SRI(무결성 해시)를 적용하지 않았다.
              고신뢰 배포가 필요하면 버전 고정 + integrity 속성 또는 자체 호스팅으로 전환할 것.
     - [WARN] 차트 JSON은 스키마 검증 없이 라이브러리에 전달한다. 같은 오리진의 빌드 산출물만
              대상이므로 신뢰 가능하지만, 외부 데이터 소스를 붙일 경우 필드 검증이 필요하다.
   -------------------------------------------------- */
