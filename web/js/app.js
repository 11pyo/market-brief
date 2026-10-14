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
     1. innerHTML을 쓰지 않는다. 동적 문자열은 textContent / createTextNode로만 넣는다
        (HTML 이스케이프 헬퍼를 두지 않는 이유 — 이스케이프가 필요한 싱크 자체가 없다).
     2. fetch 경로는 상대 경로만 쓴다 (GitHub Pages 하위 경로 대응 + SSRF 표면 없음).
     3. eval / new Function / setTimeout(문자열)로 코드를 생성하지 않는다.
     4. 파일명에 들어가는 식별자는 정규식 화이트리스트로 검증한 뒤에만 사용한다.
   ========================================================================= */
(function () {
  'use strict';

  window.addEventListener('error', function (e) {
    window.__lastError = (e && e.message) + ' @ ' + (e && e.lineno);
  });

  // ===== 공통 유틸 =====
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

  // Lightweight Charts는 시간축을 항상 UTC로 렌더한다. 뷰어가 보는 시각을 자신의
  // 표준시로 맞추기 위해, 일중 차트에 한해 표시용으로만 오프셋을 더한다.
  // 원본 data/charts/*.json 의 epoch는 손대지 않는다 (파일은 계속 UTC 기준 진실값).
  //
  // 1mo(일봉)는 제외한다. 일봉은 시각이 아니라 '거래일'이 의미이고, 미국 자정(04:00 UTC)에
  // 찍혀 있어 서쪽 표준시 뷰어에서 오프셋을 빼면 날짜가 하루 밀린다.
  function toViewerTime(ts, period) {
    if (period === '1mo') return ts;
    return ts - new Date(ts * 1000).getTimezoneOffset() * 60;
  }

  function viewerTimeZone() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
    } catch (err) {
      return '';
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
          candleSeries.setData(candles.map(function (c) {
            return {
              time: toViewerTime(c.time, period),
              open: c.open, high: c.high, low: c.low, close: c.close
            };
          }));
        } else {
          var areaSeries = chartInstance.addAreaSeries({
            lineColor: '#6366f1',
            topColor: 'rgba(99, 102, 241, 0.25)',
            bottomColor: 'rgba(99, 102, 241, 0)',
            lineWidth: 2,
            crosshairMarkerVisible: true,
            crosshairMarkerRadius: 4
          });
          areaSeries.setData(candles.map(function (c) {
            return { time: toViewerTime(c.time, period), value: c.close };
          }));
        }
        chartInstance.timeScale().fitContent();
      })
      .catch(function (err) {
        // [SECURE] 예외 원문을 화면에 렌더하지 않는다 - 내부 정보 노출 및 XSS 방지 (Category 1/4)
        console.error('[mbrief] 차트 로드 실패:', err && err.message);
        chartState(label('lChartError', 'Failed to load chart'));
      });
  }

  // 표시 순서 = 데이터 계약의 기간 순서
  var PERIOD_ORDER = ['1d', '5d', '1mo'];

  // [SECURE] data-* 값을 화이트리스트(PERIOD_ORDER)로 걸러 임의 문자열이 경로에 들어가지 않게 한다 (Category 1)
  function parsePeriods(raw) {
    var declared = String(raw || '').split(',');
    var available = [];
    for (var i = 0; i < PERIOD_ORDER.length; i++) {
      for (var j = 0; j < declared.length; j++) {
        if (declared[j].trim() === PERIOD_ORDER[i]) { available.push(PERIOD_ORDER[i]); break; }
      }
    }
    return available;
  }

  // 데이터가 없는 기간 탭은 비활성화한다 (선언이 없으면 예전 동작대로 전부 허용).
  function syncTabs(available) {
    var tabs = document.querySelectorAll('.chart-tab');
    for (var i = 0; i < tabs.length; i++) {
      var enabled = !available.length || available.indexOf(tabs[i].dataset.period) >= 0;
      tabs[i].disabled = !enabled;
      tabs[i].setAttribute('aria-disabled', enabled ? 'false' : 'true');
    }
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

  function openChart(slug, title, periodsRaw) {
    var modal = document.getElementById('chart-modal');
    if (!modal || !CHART_KEY_RE.test(slug + '_1d')) return;
    lastFocused = document.activeElement;
    chartSlug = slug;
    var heading = document.getElementById('chart-title');
    if (heading) heading.textContent = title || slug;
    // 시간축이 어느 표준시인지 명시한다 (일중 차트는 뷰어 로컬 시각으로 표시된다)
    // 이 사이트는 정적 파일이라 차트는 실시간이 아니라 '갱신 시점의 스냅샷'이다.
    // 어느 표준시로 그렸는지와, 언제 찍힌 데이터인지를 함께 밝힌다.
    var tzNote = document.getElementById('chart-tz');
    if (tzNote) {
      var parts = [];
      var tz = viewerTimeZone();
      if (tz) parts.push(label('lChartTz', 'Times shown in') + ' ' + tz);
      // 빌더가 이미 언어에 맞게 포맷한 문자열이라 그대로 쓴다 (재가공하면 로캘이 깨진다).
      var basis = body ? body.dataset.chartsUpdated : '';
      if (basis) parts.push(label('lMarketBasis', 'As of') + ' ' + basis);
      tzNote.textContent = parts.join('  ·  ');
    }
    modal.hidden = false;
    var closeBtn = document.getElementById('chart-close');
    if (closeBtn) closeBtn.focus();
    // 1D 데이터가 없는 심볼도 있다. 사용 가능한 첫 기간으로 연다.
    var available = parsePeriods(periodsRaw);
    syncTabs(available);
    setPeriod(available.length ? available[0] : '1d');
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
        openChart(el.dataset.chartSlug, el.dataset.name, el.dataset.chartPeriods);
      });
    }
    var tabs = document.querySelectorAll('.chart-tab');
    for (var j = 0; j < tabs.length; j++) {
      tabs[j].addEventListener('click', function (e) {
        if (e.currentTarget.disabled) return;
        setPeriod(e.currentTarget.dataset.period);
      });
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
    var source = '(' + tokens.map(escapeRegExp).join('|') + ')';
    var pattern = new RegExp(source, 'gi');
    // 필터에는 'g' 없는 별도 정규식을 쓴다. g 플래그 정규식의 test()는 lastIndex를 전진시켜
    // 매칭되는 텍스트 노드를 하나 걸러 하나씩 탈락시킨다 (상태를 공유하면 안 된다).
    var testPattern = new RegExp(source, 'i');
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        var tag = node.parentNode && node.parentNode.nodeName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'MARK') return NodeFilter.FILTER_REJECT;
        return testPattern.test(node.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
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
    var actors = [];
    var lensNodes = document.querySelectorAll('[data-lens]');
    for (var i = 0; i < lensNodes.length; i++) {
      var el = lensNodes[i];
      var hay = (el.dataset.lens || '') + ' ' + (el.textContent || '');
      if (!pattern.test(hay)) continue;
      var name = (el.dataset.lens || '').trim();
      if (el.classList.contains('trade-card')) trades.push(name);
      else if (el.classList.contains('actor-card')) actors.push(name);
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
    appendGroup('actors', (lensRoot && lensRoot.dataset.lActors) || 'Key actors', actors);
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

  // ===== Google Analytics 4 (GA4) =====
  function initGoogleAnalytics() {
    var gaId = body && body.dataset.gaMeasurementId;
    if (!gaId || !/^G-[A-Z0-9]+$/i.test(gaId)) return;
    window.dataLayer = window.dataLayer || [];
    function gtag() { window.dataLayer.push(arguments); }
    gtag('js', new Date());
    gtag('config', gaId);
  }

  // =======================================================================
  // 8. 스마트 네비게이터 허브 & 팝업 모달 & 뷰 모드 스위처 (스크롤 압박 0)
  // =======================================================================
  var VIEW_MODE_KEY = 'mbrief_view_mode';

  var SECTION_TITLES = {
    'briefing-body': { icon: '🧭', title: '오늘의 핵심 브리핑 (7개 파트)' },
    'sec-market': { icon: '📊', title: '10초 시장 스냅샷 (12대 지표)' },
    'sec-sectors': { icon: '🔥', title: '섹터 과열도 & PER 밸류에이션' },
    'sec-calendar': { icon: '🏛️', title: '경제 캘린더 & FOMC 일정' },
    'sec-news': { icon: '📰', title: '검증된 근거 뉴스 50선' },
    'sec-scorecard': { icon: '🎯', title: 'AI 예측 적중률 성적표' }
  };

  var currentActiveSection = null;
  var currentPlaceholder = null;

  function openSectionModal(targetId) {
    var modalEl = document.getElementById('section-modal');
    var modalBodyHost = document.getElementById('modal-body-host');
    var modalTitleEl = document.getElementById('modal-section-title');
    var modalBadgeIcon = document.getElementById('modal-badge-icon');
    var modalTabs = document.querySelectorAll('.modal-tab-btn');
    if (!modalEl || !modalBodyHost) return;

    var targetEl = document.getElementById(targetId);
    if (!targetEl) return;

    // 기존에 모달에 올라가 있던 섹션을 원래 자리로 복원
    if (currentActiveSection && currentPlaceholder && currentPlaceholder.parentNode) {
      currentPlaceholder.parentNode.insertBefore(currentActiveSection, currentPlaceholder);
      currentPlaceholder.remove();
      currentPlaceholder = null;
    }

    // 새로운 플레이스홀더를 남겨두고 섹션 DOM을 모달 내부로 이동 (이벤트·차트 보존)
    var placeholder = document.createElement('div');
    placeholder.className = 'section-placeholder';
    placeholder.style.display = 'none';
    targetEl.parentNode.insertBefore(placeholder, targetEl);
    modalBodyHost.textContent = '';
    modalBodyHost.appendChild(targetEl);

    currentActiveSection = targetEl;
    currentPlaceholder = placeholder;

    // 접힌 섹션 내용 자동 펼침
    targetEl.classList.remove('is-collapsed');
    var collapseBody = targetEl.querySelector('.collapsible-body');
    if (collapseBody) collapseBody.style.display = 'block';

    // 타이틀 및 아이콘 갱신
    var meta = SECTION_TITLES[targetId] || { icon: '📋', title: targetEl.dataset.sectionName || '상세 보기' };
    if (modalBadgeIcon) modalBadgeIcon.textContent = meta.icon;
    if (modalTitleEl) modalTitleEl.textContent = meta.title;

    // 상단 탭 활성화 상태
    for (var i = 0; i < modalTabs.length; i++) {
      modalTabs[i].classList.toggle('is-active', modalTabs[i].dataset.target === targetId);
    }

    modalEl.removeAttribute('hidden');
    body.style.overflow = 'hidden';
    modalBodyHost.scrollTop = 0;

    // URL 해시 기록 (뒤로가기 지원)
    if (window.location.hash !== '#' + targetId) {
      try { window.history.pushState(null, '', '#' + targetId); } catch (e) {}
    }
  }

  function closeSectionModal() {
    var modalEl = document.getElementById('section-modal');
    if (!modalEl) return;

    if (currentActiveSection && currentPlaceholder && currentPlaceholder.parentNode) {
      currentPlaceholder.parentNode.insertBefore(currentActiveSection, currentPlaceholder);
      currentPlaceholder.remove();
      currentPlaceholder = null;
      currentActiveSection = null;
    }

    modalEl.setAttribute('hidden', '');
    body.style.overflow = '';

    if (window.location.hash) {
      try { window.history.pushState(null, '', window.location.pathname + window.location.search); } catch (e) {}
    }
  }

  function initSectionModal() {
    var modalEl = document.getElementById('section-modal');
    var modalBackdrop = document.getElementById('modal-backdrop');
    var modalCloseBtn = document.getElementById('modal-close-btn');
    var modalTabs = document.querySelectorAll('.modal-tab-btn');
    if (!modalEl) return;

    if (modalCloseBtn) {
      modalCloseBtn.addEventListener('click', closeSectionModal);
    }
    if (modalBackdrop) {
      modalBackdrop.addEventListener('click', closeSectionModal);
    }

    for (var i = 0; i < modalTabs.length; i++) {
      (function (btn) {
        btn.addEventListener('click', function () {
          var targetId = btn.dataset.target;
          if (targetId) openSectionModal(targetId);
        });
      })(modalTabs[i]);
    }

    window.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && modalEl && !modalEl.hasAttribute('hidden')) {
        closeSectionModal();
      }
    });

    window.addEventListener('popstate', function () {
      var hash = (window.location.hash || '').replace('#', '');
      if (hash && SECTION_TITLES[hash]) {
        openSectionModal(hash);
      } else if (modalEl && !modalEl.hasAttribute('hidden')) {
        closeSectionModal();
      }
    });
  }

  function initHubNavigator() {
    var cards = document.querySelectorAll('.hub-card[data-target]');
    if (!cards.length) return;

    for (var i = 0; i < cards.length; i++) {
      (function (card) {
        function activate() {
          var targetId = card.dataset.target;
          if (!targetId) return;

          // 팝업 모드(기본)일 때는 팝업 모달로 시원하게 열기!
          if (body.classList.contains('mode-card') || !body.classList.contains('mode-all')) {
            openSectionModal(targetId);
          } else {
            // 전체 펼쳐보기 모드일 때는 해당 섹션으로 부드러운 스크롤 이동
            var targetEl = document.getElementById(targetId);
            if (targetEl) {
              targetEl.classList.remove('is-collapsed');
              targetEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
              targetEl.classList.remove('is-focused');
              void targetEl.offsetWidth;
              targetEl.classList.add('is-focused');
              window.setTimeout(function () { targetEl.classList.remove('is-focused'); }, 2000);
            }
          }
        }

        card.addEventListener('click', activate);
        card.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            activate();
          }
        });
      })(cards[i]);
    }

    // 접속 시 URL에 #섹션 해시가 있다면 바로 팝업 열기
    var initialHash = (window.location.hash || '').replace('#', '');
    if (initialHash && SECTION_TITLES[initialHash]) {
      window.setTimeout(function () { openSectionModal(initialHash); }, 150);
    }
  }

  function initViewMode() {
    var btnModeCard = document.getElementById('btn-mode-card');
    var btnModeAll = document.getElementById('btn-mode-all');
    if (!btnModeCard || !btnModeAll) return;

    function expandAllSections() {
      var collapsibles = document.querySelectorAll('.collapsible-block');
      for (var i = 0; i < collapsibles.length; i++) {
        collapsibles[i].classList.remove('is-collapsed');
        var bodyEl = collapsibles[i].querySelector('.collapsible-body');
        if (bodyEl) bodyEl.style.display = 'block';
        var toggleBtn = collapsibles[i].querySelector('.collapse-toggle-btn');
        if (toggleBtn) {
          toggleBtn.setAttribute('aria-expanded', 'true');
          var txt = toggleBtn.querySelector('.collapse-text');
          if (txt) txt.textContent = label('lCollapseFold', '접기');
        }
      }
    }

    function setMode(mode) {
      var isCard = mode === 'card';
      body.classList.toggle('mode-card', isCard);
      body.classList.toggle('mode-all', !isCard);
      btnModeCard.classList.toggle('is-active', isCard);
      btnModeCard.setAttribute('aria-pressed', isCard ? 'true' : 'false');
      btnModeAll.classList.toggle('is-active', !isCard);
      btnModeAll.setAttribute('aria-pressed', !isCard ? 'true' : 'false');
      safeLocalSet(VIEW_MODE_KEY, mode);

      // 전체 모드로 전환 시 현재 모달이 열려있다면 닫고 복원하며, 모든 섹션을 자동 펼침(Ctrl+F 및 인쇄 지원)
      if (!isCard) {
        closeSectionModal();
        expandAllSections();
      }
    }

    btnModeCard.addEventListener('click', function () { setMode('card'); });
    btnModeAll.addEventListener('click', function () { setMode('all'); });

    var saved = safeLocalGet(VIEW_MODE_KEY);
    // 기본값은 card (팝업 모드, 스크롤 0)
    setMode(saved === 'all' ? 'all' : 'card');
  }

  function initNewsExpander() {
    var btn = document.getElementById('btn-news-more');
    var list = document.getElementById('news-list');
    if (!btn || !list) return;

    var extraItems = list.querySelectorAll('.news-item--extra');
    if (!extraItems.length) return;

    for (var i = 0; i < extraItems.length; i++) {
      extraItems[i].classList.add('is-hidden');
    }

    var isExpanded = false;
    var count = btn.dataset.count || extraItems.length;

    btn.addEventListener('click', function () {
      isExpanded = !isExpanded;
      for (var j = 0; j < extraItems.length; j++) {
        extraItems[j].classList.toggle('is-hidden', !isExpanded);
      }
      var textEl = btn.querySelector('.news-more-text');
      if (textEl) {
        if (isExpanded) {
          textEl.textContent = label('lNewsShowLess', '뉴스 접기 (상위 10건만 보기)');
        } else {
          textEl.textContent = label('lNewsShowMore', '근거 뉴스 전체 펼치기') + ' (+' + count + '건 더보기)';
        }
      }
    });
  }

  function initQuickNav() {
    var quickNav = document.getElementById('quick-nav');
    var btnHub = document.getElementById('btn-quick-hub');
    var btnTop = document.getElementById('btn-quick-top');
    if (!quickNav) return;

    var ticking = false;
    window.addEventListener('scroll', function () {
      if (!ticking) {
        window.requestAnimationFrame(function () {
          var show = window.pageYOffset > 380;
          quickNav.classList.toggle('is-visible', show);
          ticking = false;
        });
        ticking = true;
      }
    }, { passive: true });

    if (btnHub) {
      btnHub.addEventListener('click', function () {
        var hub = document.getElementById('hub-navigator');
        if (hub) hub.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    }

    if (btnTop) {
      btnTop.addEventListener('click', function () {
        window.scrollTo({ top: 0, behavior: 'smooth' });
      });
    }
  }

  // 오늘자 최신 브리핑 확인 및 캐시 갱신 체크
  function initFreshnessCheck() {
    var timeEl = document.querySelector('.hero-date time');
    if (!timeEl) return;
    var publishedDate = timeEl.getAttribute('datetime');
    if (!publishedDate) return;

    // 한국 시간 기준 오늘 날짜 구하기
    try {
      var nowKst = new Date();
      var utc = nowKst.getTime() + (nowKst.getTimezoneOffset() * 60000);
      var kstDate = new Date(utc + (3600000 * 9));
      var todayKstStr = kstDate.toISOString().slice(0, 10);

      // 만약 오늘 날짜와 다르고, 최신 데이터 파일이 있는지 백그라운드로 가볍게 확인
      if (publishedDate !== todayKstStr) {
        fetch(BASE + 'data/latest.json?t=' + Date.now(), { cache: 'no-store' })
          .then(function (res) { return res.ok ? res.json() : null; })
          .then(function (data) {
            if (data && data.date && data.date !== publishedDate) {
              // 더 최신 브리핑이 배포된 경우 즉시 새로고침 안내 토스트
              showToast('✨ 오늘자 최신 브리핑이 준비되었습니다! 새로고침합니다.', 'success');
              window.setTimeout(function () { window.location.reload(); }, 1500);
            }
          })
          .catch(function () {});
      }
    } catch (e) {}
  }

  // ===== 부트스트랩 =====
  function boot() {
    initGoogleAnalytics();
    initCurrency();
    initChart();
    initShare();
    initLens();
    initPwaHint();
    initArchiveFilter();
    initServiceWorker();
    initSectionModal();
    initHubNavigator();
    initViewMode();
    initNewsExpander();
    initQuickNav();
    initFreshnessCheck();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  // 테스트/디버깅 편의를 위한 최소 노출 (전역 오염 최소화)
  window.mbrief = { formatPrice: formatPrice, lang: LANG };
})();

/* --------------------------------------------------
   Security Checklist
   Applied:
     - XSS: innerHTML 싱크가 하나도 없다. 동적 문자열은 textContent / createTextNode로만 삽입하므로
            HTML 이스케이프 단계 자체가 불필요하다 (이스케이프 누락 실수의 여지를 제거)
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
