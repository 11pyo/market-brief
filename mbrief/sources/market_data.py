"""시세 데이터 — yfinance 지수/원자재/FX + 섹터 ETF + CNN Fear & Greed + 캔들 차트.

v1 services/market_data.py를 이식했다. 변경점:
  - SYMBOL_KEYS(표시명 → 슬러그) 추가. 차트 파일명·사이트 데이터 계약의 키가 된다.
  - get_all_charts(): 12심볼 × 3기간을 제한된 스레드풀로 동시 수집 (개별 실패 격리)
  - build_market_bundle(): 스냅샷·섹터·F&G·캘린더를 동시 수집해 MarketBundle로 반환
"""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import yfinance as yf

from mbrief.schemas import FearGreed, MarketBundle, Quote, SectorQuote
from mbrief.sources import httpguard
from mbrief.timeutil import now_iso

logger = logging.getLogger(__name__)


# ===== CNN FEAR & GREED INDEX =====
# [SECURE] 비공식 CNN 데이터비즈 엔드포인트를 소스 상수로 고정 - SSRF 방지 (Category 1)
_CNN_FG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

# [SECURE] 허용 호스트를 URL 상수에서만 파생 - 리다이렉트 경유 SSRF 방지 (Category 1)
ALLOWED_HOSTS = httpguard.hosts_from_urls([_CNN_FG_URL])
_GUARD_HOOKS = httpguard.async_event_hooks(ALLOWED_HOSTS)

_CNN_FG_CACHE: tuple[float, dict] | None = None
_CNN_FG_CACHE_TTL = 300  # 5분

_CNN_FG_RATING_KO: dict[str, str] = {
    "Extreme Fear": "극도의 공포",
    "Fear": "공포",
    "Neutral": "중립",
    "Greed": "탐욕",
    "Extreme Greed": "극도의 탐욕",
}
_CNN_FG_COLORS: dict[str, str] = {
    "Extreme Fear": "#ef4444",
    "Fear":         "#f97316",
    "Neutral":      "#eab308",
    "Greed":        "#84cc16",
    "Extreme Greed": "#22c55e",
}


async def get_cnn_fear_greed() -> dict | None:
    """CNN Fear & Greed Index 조회. 5분 캐시. 실패 시 None."""
    global _CNN_FG_CACHE
    now = time.time()
    if _CNN_FG_CACHE:
        cached_at, cached_data = _CNN_FG_CACHE
        if now - cached_at < _CNN_FG_CACHE_TTL:
            logger.debug("[FearGreed] 캐시 히트")
            return cached_data

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://edition.cnn.com/markets/fear-and-greed",
            "Origin": "https://edition.cnn.com",
            "Accept": "application/json, text/plain, */*",
        }
        # [SECURE] 모든 외부 호출에 타임아웃 지정 - 무한 대기 방지 (Category 3)
        # [SECURE] 응답 훅으로 리다이렉트 종착지 호스트까지 검증 - SSRF 방지 (Category 1)
        async with httpx.AsyncClient(
            timeout=10, headers=headers, follow_redirects=True, event_hooks=_GUARD_HOOKS,
        ) as client:
            resp = await client.get(_CNN_FG_URL)
            resp.raise_for_status()
            data = resp.json()

        fg = data.get("fear_and_greed", {}) if isinstance(data, dict) else {}
        score_raw = fg.get("score")
        rating = fg.get("rating", "")

        # fear_and_greed 필드가 비면 historical 최신 항목으로 폴백
        if score_raw is None and isinstance(data, dict):
            historical = data.get("fear_and_greed_historical", {}).get("data", [])
            if historical:
                latest = historical[-1]
                score_raw = latest.get("y")
                rating = latest.get("rating", "")
                logger.warning("[FearGreed] historical 최신 항목 사용: score=%s", score_raw)

        if score_raw is None:
            logger.warning("[FearGreed] score 없음 - 건너뜀")
            return None

        score = round(float(score_raw), 1)
        result = {
            "score": score,
            "rating": rating,
            "rating_ko": _CNN_FG_RATING_KO.get(rating, rating),
            "color": _CNN_FG_COLORS.get(rating, "#eab308"),
            "previous_close": round(float(fg.get("previous_close") or 0), 1),
            "previous_1week": round(float(fg.get("previous_1_week") or 0), 1),
            "previous_1month": round(float(fg.get("previous_1_month") or 0), 1),
            "timestamp": str(fg.get("timestamp", "")),
        }
        _CNN_FG_CACHE = (now, result)
        logger.info("[FearGreed] 조회 완료: %s (%s)", score, rating)
        return result

    except (httpx.HTTPError, ValueError, TypeError, KeyError) as e:
        # [SECURE] 상세 오류는 로컬 로그에만 기록 - 내부 정보 노출 방지 (Category 4)
        logger.warning("[FearGreed] CNN 조회 실패: %s", e)
        return None


# 표시명 → Yahoo Finance 심볼
SYMBOLS: dict[str, str] = {
    "S&P 500":            "^GSPC",
    "NASDAQ":             "^IXIC",
    "Dow Jones":          "^DJI",
    "US 10Y Yield":       "^TNX",
    "Dollar Index (DXY)": "DX-Y.NYB",
    "Crude Oil (WTI)":    "CL=F",
    "VIX":                "^VIX",
    "Gold":               "GC=F",
    "EUR/USD":            "EURUSD=X",
    "BTC/USD":            "BTC-USD",
    "KOSPI":              "^KS11",
    "USD/KRW":            "KRW=X",
}

# 표시명 → 슬러그. 차트 파일명(`<slug>_<period>.json`)과 사이트 데이터 계약의 키.
SYMBOL_KEYS: dict[str, str] = {
    "S&P 500":            "sp500",
    "NASDAQ":             "nasdaq",
    "Dow Jones":          "dow",
    "US 10Y Yield":       "us10y",
    "Dollar Index (DXY)": "dxy",
    "Crude Oil (WTI)":    "wti",
    "VIX":                "vix",
    "Gold":               "gold",
    "EUR/USD":            "eurusd",
    "BTC/USD":            "btc",
    "KOSPI":              "kospi",
    "USD/KRW":            "usdkrw",
}

_snapshot_cache: tuple[float, dict] | None = None
SNAPSHOT_CACHE_TTL = 30  # 30초 — 시장 개장 중 적정, yfinance 호출 절감


def _fetch_snapshot_sync() -> dict[str, dict]:
    """동기 yfinance 호출 (executor에서 실행)."""
    tickers = yf.Tickers(" ".join(SYMBOLS.values()))
    results: dict[str, dict] = {}

    for name, symbol in SYMBOLS.items():
        try:
            info = tickers.tickers[symbol].fast_info
            price = info.last_price
            prev_close = info.previous_close
            if price is None or prev_close is None:
                raise ValueError("가격 없음")
            change = price - prev_close
            change_pct = (change / prev_close) * 100 if prev_close else 0.0
            results[name] = {
                "price": round(price, 4),
                "change": round(change, 4),
                "changePercent": round(change_pct, 4),
            }
        except (KeyError, ValueError, TypeError, ZeroDivisionError, AttributeError, OSError) as e:
            # [SECURE] 개별 심볼 실패를 격리 - 전체 수집 중단 방지 (Category 4)
            logger.warning("[MarketData] %s (%s) 조회 실패: %s", name, symbol, e)
            results[name] = {"price": None, "change": None, "changePercent": None}

    success = sum(1 for v in results.values() if v["price"] is not None)
    logger.info("[MarketData] %d/%d개 지표 조회 완료", success, len(results))
    return results


async def get_snapshot() -> dict[str, dict]:
    """지수·원자재·FX 스냅샷. 30초 캐시. yfinance는 동기이므로 executor에서 실행."""
    global _snapshot_cache
    now = time.time()
    if _snapshot_cache:
        cached_at, cached_data = _snapshot_cache
        if now - cached_at < SNAPSHOT_CACHE_TTL:
            logger.debug("[MarketData] 스냅샷 캐시 히트")
            return cached_data
    loop = asyncio.get_running_loop()
    # [SECURE] 상한 타임아웃 - 외부 라이브러리 무한 대기 방지 (Category 3)
    async with asyncio.timeout(60):
        result = await loop.run_in_executor(None, _fetch_snapshot_sync)
    _snapshot_cache = (now, result)
    return result


# ===== 섹터 ETF =====
SECTOR_SYMBOLS: dict[str, str] = {
    "Technology":          "XLK",
    "Financials":          "XLF",
    "Energy":              "XLE",
    "Healthcare":          "XLV",
    "Industrials":         "XLI",
    "Cons. Discretionary": "XLY",
    "Cons. Staples":       "XLP",
    "Communication":       "XLC",
    "Materials":           "XLB",
    "Real Estate":         "XLRE",
}

_sector_cache: tuple[float, dict] | None = None
SECTOR_CACHE_TTL = 60  # 1분


def _fetch_sector_sync() -> dict[str, dict]:
    tickers = yf.Tickers(" ".join(SECTOR_SYMBOLS.values()))
    results: dict[str, dict] = {}
    for name, symbol in SECTOR_SYMBOLS.items():
        try:
            info = tickers.tickers[symbol].fast_info
            price = info.last_price
            prev_close = info.previous_close
            if price is None or prev_close is None:
                raise ValueError("가격 없음")
            change_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0.0
            results[name] = {
                "symbol": symbol,
                "price": round(price, 2),
                "changePercent": round(change_pct, 2),
            }
        except (KeyError, ValueError, TypeError, ZeroDivisionError, AttributeError, OSError) as e:
            logger.warning("[MarketData] 섹터 %s (%s) 조회 실패: %s", name, symbol, e)
    return results


async def get_sector_snapshot() -> dict[str, dict]:
    """섹터 ETF 스냅샷. 60초 캐시."""
    global _sector_cache
    now = time.time()
    if _sector_cache:
        cached_at, cached_data = _sector_cache
        if now - cached_at < SECTOR_CACHE_TTL:
            return cached_data
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(60):
        result = await loop.run_in_executor(None, _fetch_sector_sync)
    _sector_cache = (now, result)
    return result


# ===== 캔들 차트 =====
_chart_cache: dict[str, tuple[float, list]] = {}
CHART_CACHE_TTL = 60  # 1분

PERIODS: tuple[str, ...] = ("1d", "5d", "1mo")
PERIOD_CONFIG: dict[str, dict[str, str]] = {
    "1d":  {"period": "1d",  "interval": "5m"},
    "5d":  {"period": "5d",  "interval": "30m"},
    "1mo": {"period": "1mo", "interval": "1d"},
}

# [SECURE] 차트 동시 수집 워커 상한 - 스레드/소켓 고갈(DoS) 방지 (Category 3)
_CHART_MAX_WORKERS = 6
_CHART_TOTAL_TIMEOUT = 300


def _fetch_chart_sync(symbol: str, period: str) -> list[dict]:
    config = PERIOD_CONFIG.get(period, PERIOD_CONFIG["1d"])
    hist = yf.Ticker(symbol).history(period=config["period"], interval=config["interval"])
    if hist.empty:
        return []
    candles: list[dict] = []
    seen: set[int] = set()
    for dt, row in hist.iterrows():
        ts = int(dt.timestamp())
        if ts in seen:
            continue
        seen.add(ts)
        candles.append({
            "time":  ts,
            "open":  round(float(row["Open"]),  4),
            "high":  round(float(row["High"]),  4),
            "low":   round(float(row["Low"]),   4),
            "close": round(float(row["Close"]), 4),
        })
    return candles


async def get_chart(name: str, period: str = "1d") -> list[dict]:
    """표시명 + 기간 → 캔들 목록. 60초 캐시. 알 수 없는 심볼이면 빈 목록."""
    symbol = SYMBOLS.get(name)
    if symbol is None:
        # [SECURE] 화이트리스트에 없는 심볼은 조회하지 않음 - 임의 외부 요청 방지 (Category 1)
        logger.warning("[MarketData] 알 수 없는 심볼: %r", str(name)[:40])
        return []
    if period not in PERIOD_CONFIG:
        period = "1d"

    cache_key = f"{symbol}:{period}"
    now = time.time()
    cached = _chart_cache.get(cache_key)
    if cached is not None and now - cached[0] < CHART_CACHE_TTL:
        return cached[1]

    loop = asyncio.get_running_loop()
    async with asyncio.timeout(45):
        candles = await loop.run_in_executor(None, _fetch_chart_sync, symbol, period)
    _chart_cache[cache_key] = (now, candles)
    return candles


async def get_all_charts() -> dict[str, list[dict]]:
    """12심볼 × 3기간을 제한된 스레드풀로 동시 수집.

    Returns: {"<slug>_<period>": [Candle, ...]} — 빈 결과는 키에 포함하지 않는다.
    개별 심볼 실패는 로깅만 하고 전체 수집을 중단시키지 않는다.
    """
    loop = asyncio.get_running_loop()
    jobs: list[tuple[str, str, str]] = [
        (f"{SYMBOL_KEYS[name]}_{period}", symbol, period)
        for name, symbol in SYMBOLS.items()
        for period in PERIODS
    ]

    results: dict[str, list[dict]] = {}
    # [SECURE] context manager로 스레드풀 확실히 종료 - 리소스 누수 방지 (Category 5)
    with ThreadPoolExecutor(max_workers=_CHART_MAX_WORKERS, thread_name_prefix="chart") as pool:
        tasks = [loop.run_in_executor(pool, _fetch_chart_sync, symbol, period) for _, symbol, period in jobs]
        # [SECURE] 전체 상한을 두되(무한 대기 방지, Category 3) 이미 끝난 결과는 반드시 수확한다.
        # gather를 timeout으로 감싸면 완료된 차트까지 전부 버려져 사이트가 통째로 비게 된다.
        done, pending = await asyncio.wait(tasks, timeout=_CHART_TOTAL_TIMEOUT)
        if pending:
            logger.warning(
                "[MarketData] 차트 수집 타임아웃(%ss) - 완료 %d개는 사용, 미완 %d개 취소",
                _CHART_TOTAL_TIMEOUT, len(done), len(pending),
            )
            for task in pending:
                task.cancel()

    for (key, symbol, period), task in zip(jobs, tasks):
        if task not in done or task.cancelled():
            continue
        outcome = task.exception()
        if outcome is not None:
            # [SECURE] 개별 실패 격리 후 로깅 - 빈 except 없음 (Category 4)
            logger.warning("[MarketData] 차트 실패 %s (%s/%s): %s", key, symbol, period, outcome)
            continue
        candles = task.result()
        if candles:
            results[key] = candles

    logger.info("[MarketData] 차트 %d/%d개 수집", len(results), len(jobs))
    return results


def format_for_llm(snapshot: dict, fear_greed: dict | None = None) -> str:
    """프롬프트용 시세 텍스트."""
    text = "=== CURRENT MARKET DATA ===\n"
    for name, data in (snapshot or {}).items():
        price = (data or {}).get("price")
        if price is not None:
            change = data.get("change") or 0
            direction = "▲" if change >= 0 else "▼"
            pct_val = data.get("changePercent")
            pct = f" ({pct_val:+.2f}%)" if pct_val is not None else ""
            text += f"{name}: {price:,.2f} {direction} {abs(change):.2f}{pct}\n"
        else:
            text += f"{name}: N/A\n"

    if fear_greed:
        text += (
            f"\n=== CNN FEAR & GREED INDEX (Source: CNN Markets) ===\n"
            f"Score: {fear_greed.get('score')} / 100 — {fear_greed.get('rating')} "
            f"({fear_greed.get('rating_ko')})\n"
            f"Previous Close: {fear_greed.get('previous_close')} | "
            f"1 Week Ago: {fear_greed.get('previous_1week')} | "
            f"1 Month Ago: {fear_greed.get('previous_1month')}\n"
        )
    return text


def to_bundle(
    snapshot: dict,
    sectors: dict,
    fear_greed: dict | None,
    calendar: dict | None,
    chart_keys: list[str] | None = None,
) -> MarketBundle:
    """원시 dict → MarketBundle (pydantic 검증 경유)."""
    quotes: dict[str, Quote] = {}
    for name, data in (snapshot or {}).items():
        try:
            quotes[name] = Quote(**(data or {}))
        except (TypeError, ValueError) as e:
            logger.warning("[MarketData] 시세 변환 실패 %s: %s", name, e)

    sector_quotes: dict[str, SectorQuote] = {}
    for name, data in (sectors or {}).items():
        try:
            sector_quotes[name] = SectorQuote(**(data or {}))
        except (TypeError, ValueError) as e:
            logger.warning("[MarketData] 섹터 변환 실패 %s: %s", name, e)

    fg: FearGreed | None = None
    if fear_greed:
        try:
            fg = FearGreed(**fear_greed)
        except (TypeError, ValueError) as e:
            logger.warning("[MarketData] 공포탐욕 변환 실패: %s", e)

    return MarketBundle(
        updatedAt=now_iso(),
        quotes=quotes,
        sectors=sector_quotes,
        fearGreed=fg,
        calendar=calendar or {},
        chartKeys=sorted(chart_keys or []),
    )


async def build_market_bundle() -> MarketBundle:
    """스냅샷 + 섹터 + 공포탐욕 + 캘린더를 동시 수집해 MarketBundle 반환.

    chartKeys는 차트를 실제로 저장하는 호출자(pipeline.refresh)가 채운다.
    개별 소스 실패는 해당 필드를 비우고 나머지는 유지한다.
    """
    from mbrief.sources import calendar_service  # 지연 임포트 — 모듈 간 순환 방지

    snapshot, sectors, fear_greed, calendar = await asyncio.gather(
        get_snapshot(),
        get_sector_snapshot(),
        get_cnn_fear_greed(),
        calendar_service.get_calendar(),
        return_exceptions=True,
    )

    def _ok(value: object, label: str, default: object) -> object:
        if isinstance(value, BaseException):
            logger.warning("[MarketData] %s 수집 실패: %s", label, value)
            return default
        return value

    snapshot = _ok(snapshot, "스냅샷", {})
    sectors = _ok(sectors, "섹터", {})
    fear_greed = _ok(fear_greed, "공포탐욕", None)
    calendar = _ok(calendar, "캘린더", {})

    return to_bundle(snapshot, sectors, fear_greed, calendar)  # type: ignore[arg-type]


# --------------------------------------------------
# Security Checklist
# Applied:
#   - SSRF: 외부 요청 URL과 심볼을 모두 소스 상수(화이트리스트)로 고정, 사용자 입력 URL 미사용.
#     CNN 호출은 httpguard 응답 훅으로 리다이렉트 종착지의 스킴(https)·호스트까지 검증한다
#   - Path Traversal: 차트 키는 SYMBOL_KEYS 슬러그 + 고정 기간 문자열로만 생성 (store가 재검증)
#   - Infinite Loop: 모든 외부 호출에 asyncio.timeout 상한, 차트 전체 수집 300초 상한
#     (상한 초과 시 asyncio.wait으로 완료분은 수확하고 미완 작업만 취소 - 부분 결과 보존)
#   - Improper Resource Release: httpx.AsyncClient / ThreadPoolExecutor를 context manager로 사용
#   - Missing Error Handling: 심볼·섹터·차트별 예외를 개별 격리하여 전체 수집 중단 방지
#   - Improper Exception Handling: 빈 except 없음, 모든 실패 경로에서 로깅
#   - Error Message Information Exposure: 상세 오류는 로컬 로그에만 기록
#   - Null Pointer Dereference: 결측 필드에 기본값 적용 후 None 검사
#   - 자원 고갈 방지: 차트 동시 실행 워커 6개로 제한
# Not Applied:
#   - [WARN] Improper Certificate Validation: httpx/yfinance 기본 TLS 검증에 의존 (핀닝 없음).
#   - [WARN] Use of Vulnerable API: CNN Fear&Greed는 비공식 엔드포인트로 사전 통지 없이 변경될 수 있다.
#     실패 시 None을 반환해 파이프라인은 계속 진행하지만 정기 점검이 필요하다.
# --------------------------------------------------
