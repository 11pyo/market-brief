"""시세 데이터 — yfinance 지수/원자재/FX + 섹터 ETF + CNN Fear & Greed + 캔들 차트.

v1 services/market_data.py를 이식했다. 변경점:
  - SYMBOL_KEYS(표시명 → 슬러그) 추가. 차트 파일명·사이트 데이터 계약의 키가 된다.
  - get_all_charts(): 12심볼 × 3기간을 제한된 스레드풀로 동시 수집 (개별 실패 격리)
  - build_market_bundle(): 스냅샷·섹터·F&G·캘린더를 동시 수집해 MarketBundle로 반환
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import yfinance as yf

from mbrief import indicators
from mbrief.schemas import (
    FearGreed,
    MarketBundle,
    Quote,
    SectorHeat,
    SectorQuote,
    SectorValuation,
)
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
    "Utilities":           "XLU",
}

# 대분류 안을 쪼갠 산업·테마 ETF.
# GICS 11개 대분류는 "Technology" 하나에 반도체·소프트웨어·클라우드가 뒤섞여 있어
# 무엇이 달아올랐는지 구분되지 않는다. 아래는 그 안을 갈라 보기 위한 목록이다.
#
# 선정 기준: 2년치 일봉 확보(백분위 계산에 필요) + 테마 중복 최소화.
# 주의: 테마는 대분류의 '부분집합'이라 서로 겹친다 (SMH 는 XLK 안에 있다). 순위를 나란히
#       놓고 합산하거나 비중으로 읽으면 안 되며, 화면·프롬프트에 이 사실을 함께 표기한다.
THEME_SYMBOLS: dict[str, str] = {
    "반도체":            "SMH",
    "소프트웨어":         "IGV",
    "클라우드":           "SKYY",
    "데이터센터 인프라":    "DTCR",
    "사이버보안":         "CIBR",
    "인터넷 플랫폼":       "FDN",
    "AI":               "AIQ",
    "바이오텍":           "XBI",
    "방산·항공우주":       "ITA",
    "지역은행":           "KRE",
    "주택건설":           "ITB",
    "금광":              "GDX",
    "우라늄·원자력":       "URA",
}

# 과열도·밸류에이션 계산 대상 전체 (대분류 + 테마). 값은 (심볼, group).
ALL_HEAT_SYMBOLS: dict[str, tuple[str, str]] = {
    **{name: (sym, "sector") for name, sym in SECTOR_SYMBOLS.items()},
    **{name: (sym, "theme") for name, sym in THEME_SYMBOLS.items()},
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


# ===== 섹터 기술적 과열도 (가격만으로 계산) =====
# 이 블록의 수치는 전부 파이썬이 계산한다. LLM에게 "이 섹터 과열됐나?"를 묻지 않는다 —
# 모델은 가격 시계열을 보지 못하므로 본 적 없는 백분위를 지어낼 수 있다.
# 판정 규칙과 임계값은 mbrief/indicators.py 한 곳에만 있다.

SECTOR_BENCHMARK = "^GSPC"        # 상대강도(rel3m) 기준 지수
HISTORY_PERIOD = "2y"             # 200일 이동평균 + 1년 이상의 이격도 분포를 확보하는 최소 기간
# [SECURE] 기간 문자열 화이트리스트 - 임의 값이 외부 API 인자로 전달되는 것 방지 (Category 1)
HISTORY_PERIODS: tuple[str, ...] = ("1y", "2y", "5y", "10y", "max")
REL_LOOKBACK_DAYS = 63            # 약 3개월 (거래일 기준)
MIN_GAP_SAMPLE_DAYS = 60          # 이격도 백분위를 신뢰하기 위한 최소 관측일 수
MIN_VALUATION_DAYS = 60           # PER 백분위를 채우기 위한 최소 기록일 수 (Feature 2)

# [SECURE] 동시 수집 워커·전체 시간 상한 - 스레드/소켓 고갈과 무한 대기 방지 (Category 3)
_HISTORY_MAX_WORKERS = 6
_HISTORY_TOTAL_TIMEOUT = 300
_FUNDAMENTALS_MAX_WORKERS = 4
_FUNDAMENTALS_TOTAL_TIMEOUT = 180


def _finite_float(value: object) -> float | None:
    """숫자로 바꿀 수 있는 유한값만 남긴다. 그 외(None/문자열/NaN/inf/bool)는 None.

    [SECURE] 외부 API의 결측·비정상 값을 계산 전에 제거 - 잘못된 입력 검증 (Category 1)
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None  # [SECURE] 빈 except 없음 - 변환 실패는 결측으로 확정 (Category 4)
    return number if math.isfinite(number) else None


def _fetch_history_sync(symbol: str, period: str) -> list[float]:
    """일봉 종가 목록 (동기, executor에서 실행). 빈 결과는 빈 리스트."""
    hist = yf.Ticker(symbol).history(period=period, interval="1d")
    if hist.empty or "Close" not in hist:
        return []
    closes: list[float] = []
    for raw in hist["Close"].tolist():
        number = _finite_float(raw)
        if number is not None:
            closes.append(number)
    return closes


async def get_sector_history(period: str = HISTORY_PERIOD) -> dict[str, list[float]]:
    """11개 섹터 ETF + ^GSPC의 일봉 종가를 동시 수집한다.

    Returns: {"XLK": [종가...], ..., "^GSPC": [...]} — 빈 결과는 키에 포함하지 않는다.
    get_all_charts와 같은 방식으로 개별 실패를 격리하고, 전체 타임아웃이 걸려도
    **완료된 심볼은 반드시 수확한다** (하나 때문에 전부 버리면 과열도가 통째로 빈다).
    """
    if period not in HISTORY_PERIODS:
        logger.warning("[MarketData] 허용되지 않은 기간 %r - %s로 대체", str(period)[:10], HISTORY_PERIOD)
        period = HISTORY_PERIOD

    # dict.fromkeys로 순서를 유지하며 중복 제거 (섹터에 ^GSPC가 들어와도 두 번 받지 않는다)
    symbols = list(dict.fromkeys(
        [*SECTOR_SYMBOLS.values(), *THEME_SYMBOLS.values(), SECTOR_BENCHMARK]
    ))
    loop = asyncio.get_running_loop()

    results: dict[str, list[float]] = {}
    # [SECURE] context manager로 스레드풀 확실히 종료 - 리소스 누수 방지 (Category 5)
    with ThreadPoolExecutor(max_workers=_HISTORY_MAX_WORKERS, thread_name_prefix="history") as pool:
        tasks = [loop.run_in_executor(pool, _fetch_history_sync, symbol, period) for symbol in symbols]
        done, pending = await asyncio.wait(tasks, timeout=_HISTORY_TOTAL_TIMEOUT)
        if pending:
            logger.warning(
                "[MarketData] 섹터 시계열 타임아웃(%ss) - 완료 %d개는 사용, 미완 %d개 취소",
                _HISTORY_TOTAL_TIMEOUT, len(done), len(pending),
            )
            for task in pending:
                task.cancel()

    for symbol, task in zip(symbols, tasks):
        if task not in done or task.cancelled():
            continue
        outcome = task.exception()
        if outcome is not None:
            # [SECURE] 개별 실패 격리 후 로깅 - 빈 except 없음 (Category 4)
            logger.warning("[MarketData] 시계열 실패 %s: %s", symbol, outcome)
            continue
        closes = task.result()
        if closes:
            results[symbol] = closes

    logger.info("[MarketData] 섹터 시계열 %d/%d개 수집 (period=%s)", len(results), len(symbols), period)
    return results


def build_sector_heat(history: dict[str, list[float]] | None) -> dict[str, SectorHeat]:
    """일봉 종가 → 섹터 과열도. 네트워크를 쓰지 않는 순수 변환이다.

    - gapPct        : (마지막 종가 / SMA200 - 1) * 100
    - gapPercentile : 그 이격도가 자기 자신의 과거 이격도 분포에서 차지하는 백분위
    - rsi14         : 일봉 RSI(14), Wilder 평활
    - rel3m         : 섹터 63거래일 수익률 - ^GSPC 63거래일 수익률 (%p)
    - label         : gapPercentile 하나로만 결정 (indicators.heat_label)

    관측치가 MIN_GAP_SAMPLE_DAYS 미만이면 gapPercentile을 채우지 않고 sampleDays=0으로 둔다.
    이때 label은 "neutral"이지만 그것은 '중립'이 아니라 '모름'이며, sampleDays=0이 그 구분자다.
    """
    history = history or {}
    benchmark = history.get(SECTOR_BENCHMARK) or []
    benchmark_return = indicators.pct_return(benchmark, REL_LOOKBACK_DAYS)
    if benchmark_return is None and benchmark:
        logger.warning("[MarketData] 기준 지수(%s) 3개월 수익률 계산 불가 - rel3m 생략", SECTOR_BENCHMARK)

    heat: dict[str, SectorHeat] = {}
    for name, (symbol, group) in ALL_HEAT_SYMBOLS.items():
        closes = history.get(symbol) or []
        if not closes:
            logger.warning("[MarketData] 과열도 건너뜀 %s (%s): 종가 없음", name, symbol)
            continue

        gaps = indicators.ma_gap_series(closes, indicators.MA_WINDOW)
        gap_pct = gaps[-1] if gaps else None

        gap_percentile: float | None = None
        sample_days = 0
        if len(gaps) >= MIN_GAP_SAMPLE_DAYS:
            gap_percentile = indicators.percentile_rank(gaps, gap_pct)
            if gap_percentile is not None:
                sample_days = len(gaps)

        sector_return = indicators.pct_return(closes, REL_LOOKBACK_DAYS)
        rel3m = (
            sector_return - benchmark_return
            if sector_return is not None and benchmark_return is not None
            else None
        )

        try:
            heat[name] = SectorHeat(
                symbol=symbol,
                name=name,
                group=group,
                gapPct=None if gap_pct is None else round(gap_pct, 2),
                gapPercentile=None if gap_percentile is None else round(gap_percentile, 1),
                rsi14=_round_or_none(indicators.rsi(closes, indicators.RSI_PERIOD), 1),
                rel3m=None if rel3m is None else round(rel3m, 2),
                label=indicators.heat_label(gap_percentile),  # type: ignore[arg-type]
                sampleDays=sample_days,
            )
        except (TypeError, ValueError) as e:
            # [SECURE] 개별 섹터 변환 실패를 격리 - 전체 과열도 손실 방지 (Category 4)
            logger.warning("[MarketData] 과열도 변환 실패 %s (%s): %s", name, symbol, e)

    unknown = sum(1 for item in heat.values() if item.sampleDays == 0)
    logger.info("[MarketData] 섹터 과열도 %d개 산출 (표본 부족 %d개)", len(heat), unknown)
    return heat


def _round_or_none(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


# ===== 섹터 밸류에이션 (PER/PBR 일별 기록) =====
def _fetch_fundamentals_sync(symbol: str) -> dict:
    """yf.Ticker(symbol).info에서 PER/PBR/배당수익률만 뽑는다 (동기, executor에서 실행).

    ETF에 따라 값이 자주 비어 있다. 결측은 None으로 두고 예외로 만들지 않는다.
    dividendYield는 ETF info의 `yield` 필드다 (0.0043 = 0.43%).
    """
    info = yf.Ticker(symbol).info
    if not isinstance(info, dict):
        return {"trailingPE": None, "priceToBook": None, "dividendYield": None}
    return {
        "trailingPE": _finite_float(info.get("trailingPE")),
        "priceToBook": _finite_float(info.get("priceToBook")),
        "dividendYield": _finite_float(info.get("yield")),
    }


async def get_sector_fundamentals() -> dict[str, dict]:
    """11개 섹터 ETF의 PER/PBR/배당수익률을 동시 수집한다.

    Returns: {"XLK": {"trailingPE": 33.15, "priceToBook": 1.25, "dividendYield": 0.0043}, ...}
    개별 심볼 실패는 격리하고, 결측 필드 수를 집계해 한 줄로 로깅한다. 절대 전체를 실패시키지 않는다.
    """
    symbols = list(dict.fromkeys([*SECTOR_SYMBOLS.values(), *THEME_SYMBOLS.values()]))
    loop = asyncio.get_running_loop()

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=_FUNDAMENTALS_MAX_WORKERS, thread_name_prefix="funda") as pool:
        tasks = [loop.run_in_executor(pool, _fetch_fundamentals_sync, symbol) for symbol in symbols]
        done, pending = await asyncio.wait(tasks, timeout=_FUNDAMENTALS_TOTAL_TIMEOUT)
        if pending:
            logger.warning(
                "[MarketData] 밸류에이션 타임아웃(%ss) - 완료 %d개는 사용, 미완 %d개 취소",
                _FUNDAMENTALS_TOTAL_TIMEOUT, len(done), len(pending),
            )
            for task in pending:
                task.cancel()

    missing = 0
    for symbol, task in zip(symbols, tasks):
        if task not in done or task.cancelled():
            continue
        outcome = task.exception()
        if outcome is not None:
            logger.warning("[MarketData] 밸류에이션 실패 %s: %s", symbol, outcome)
            continue
        data = task.result()
        results[symbol] = data
        missing += sum(1 for key in ("trailingPE", "priceToBook", "dividendYield") if data.get(key) is None)

    logger.info("[MarketData] 섹터 밸류에이션 %d/%d개 수집 (결측 필드 %d개)",
                len(results), len(symbols), missing)
    return results


def valuation_rows(fundamentals: dict[str, dict] | None, date_str: str) -> list[dict]:
    """수집 결과 → store.append_valuation 입력 형식. 값이 하나도 없는 심볼은 기록하지 않는다."""
    rows: list[dict] = []
    for symbol, data in (fundamentals or {}).items():
        data = data or {}
        values = {key: _finite_float(data.get(key))
                  for key in ("trailingPE", "priceToBook", "dividendYield")}
        if all(v is None for v in values.values()):
            continue  # 빈 관측치는 분포를 흐릴 뿐이라 기록하지 않는다
        rows.append({"date": date_str, "symbol": symbol, **values})
    return rows


def build_sector_valuation(
    fundamentals: dict[str, dict] | None,
    history_rows: list[dict] | None,
) -> dict[str, SectorValuation]:
    """현재 PER/PBR + 누적 기록 → 섹터 밸류에이션. 네트워크를 쓰지 않는 순수 변환이다.

    pePercentile은 그 심볼의 PER 관측치가 MIN_VALUATION_DAYS개 이상 쌓인 뒤에만 채운다.
    그 전에는 None이고 recordedDays가 진척도를 보여 준다 — '저평가/고평가'를 말할 근거가
    아직 없다는 사실 자체를 데이터로 표현한다.
    """
    fundamentals = fundamentals or {}

    # 심볼별 PER 관측치 / 기록일 수를 한 번만 훑어 모은다.
    pe_history: dict[str, list[float]] = {}
    recorded_dates: dict[str, set[str]] = {}
    for row in history_rows or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        date_str = str(row.get("date") or "")
        if date_str:
            recorded_dates.setdefault(symbol, set()).add(date_str)
        value = _finite_float(row.get("trailingPE"))
        if value is not None:
            pe_history.setdefault(symbol, []).append(value)

    valuation: dict[str, SectorValuation] = {}
    for name, (symbol, group) in ALL_HEAT_SYMBOLS.items():
        data = fundamentals.get(symbol)
        recorded_days = len(recorded_dates.get(symbol, ()))
        if data is None and recorded_days == 0:
            continue  # 오늘 값도 없고 기록도 없다면 보여 줄 것이 없다
        data = data or {}

        trailing_pe = _finite_float(data.get("trailingPE"))
        samples = pe_history.get(symbol, [])
        pe_percentile: float | None = None
        if trailing_pe is not None and len(samples) >= MIN_VALUATION_DAYS:
            pe_percentile = indicators.percentile_rank(samples, trailing_pe)

        try:
            valuation[name] = SectorValuation(
                symbol=symbol,
                name=name,
                group=group,
                trailingPE=_round_or_none(trailing_pe, 2),
                priceToBook=_round_or_none(_finite_float(data.get("priceToBook")), 2),
                dividendYield=_round_or_none(_finite_float(data.get("dividendYield")), 6),
                pePercentile=_round_or_none(pe_percentile, 1),
                recordedDays=recorded_days,
            )
        except (TypeError, ValueError) as e:
            logger.warning("[MarketData] 밸류에이션 변환 실패 %s (%s): %s", name, symbol, e)

    ready = sum(1 for item in valuation.values() if item.pePercentile is not None)
    logger.info("[MarketData] 섹터 밸류에이션 %d개 구성 (백분위 확보 %d개, 기준 %d일)",
                len(valuation), ready, MIN_VALUATION_DAYS)
    return valuation


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
    sector_heat: dict[str, SectorHeat] | None = None,
    sector_valuation: dict[str, SectorValuation] | None = None,
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
        sectorHeat=dict(sector_heat or {}),              # [SECURE] 호출자 dict를 복사해 보관 (Category 6)
        sectorValuation=dict(sector_valuation or {}),    # [SECURE] 호출자 dict를 복사해 보관 (Category 6)
    )


async def build_market_bundle() -> MarketBundle:
    """스냅샷 + 섹터 + 공포탐욕 + 캘린더 + 섹터 시계열을 동시 수집해 MarketBundle 반환.

    sectorHeat는 여기서 채운다 (가격만으로 계산하므로 추가 API 키가 필요 없다).
    chartKeys와 sectorValuation은 파일 상태를 다루는 호출자(pipeline.refresh)가 채운다.
    개별 소스 실패는 해당 필드를 비우고 나머지는 유지한다.
    """
    from mbrief.sources import calendar_service  # 지연 임포트 — 모듈 간 순환 방지

    snapshot, sectors, fear_greed, calendar, history = await asyncio.gather(
        get_snapshot(),
        get_sector_snapshot(),
        get_cnn_fear_greed(),
        calendar_service.get_calendar(),
        get_sector_history(),
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
    history = _ok(history, "섹터 시계열", {})

    return to_bundle(
        snapshot, sectors, fear_greed, calendar,  # type: ignore[arg-type]
        sector_heat=build_sector_heat(history),   # type: ignore[arg-type]
    )


# --------------------------------------------------
# Security Checklist
# Applied:
#   - SSRF: 외부 요청 URL과 심볼을 모두 소스 상수(화이트리스트)로 고정, 사용자 입력 URL 미사용.
#     CNN 호출은 httpguard 응답 훅으로 리다이렉트 종착지의 스킴(https)·호스트까지 검증한다
#   - Path Traversal: 차트 키는 SYMBOL_KEYS 슬러그 + 고정 기간 문자열로만 생성 (store가 재검증)
#   - Improper Input Validation: 시계열 기간(period)은 HISTORY_PERIODS 화이트리스트로 검증한 뒤에만
#     외부 API에 전달하고, 외부에서 온 수치는 _finite_float로 NaN/inf/비수치를 걸러 계산에 넣는다
#   - Infinite Loop: 모든 외부 호출에 asyncio.timeout 상한, 차트/시계열 전체 수집 300초·밸류에이션 180초 상한
#     (상한 초과 시 asyncio.wait으로 완료분은 수확하고 미완 작업만 취소 - 부분 결과 보존)
#   - Improper Resource Release: httpx.AsyncClient / ThreadPoolExecutor를 context manager로 사용
#   - Missing Error Handling: 심볼·섹터·차트·시계열·밸류에이션별 예외를 개별 격리하여 전체 수집 중단 방지
#   - Improper Exception Handling: 빈 except 없음, 모든 실패 경로에서 로깅
#   - Error Message Information Exposure: 상세 오류는 로컬 로그에만 기록
#   - Null Pointer Dereference: 결측 필드에 기본값 적용 후 None 검사
#   - Private Array Returned: to_bundle이 sector_heat/sector_valuation dict를 복사해 보관하여
#     호출자가 나중에 원본을 변경해도 번들 내용이 바뀌지 않는다 (Category 6)
#   - 자원 고갈 방지: 차트 동시 실행 워커 6개, 시계열 6개, 밸류에이션 4개로 제한
# Not Applied:
#   - [WARN] Improper Certificate Validation: httpx/yfinance 기본 TLS 검증에 의존 (핀닝 없음).
#   - [WARN] Use of Vulnerable API: CNN Fear&Greed는 비공식 엔드포인트로 사전 통지 없이 변경될 수 있다.
#     실패 시 None을 반환해 파이프라인은 계속 진행하지만 정기 점검이 필요하다.
#   - [WARN] yf.Ticker(...).info는 스크래핑 성격이라 필드 구성이 예고 없이 바뀔 수 있다.
#     결측은 None으로 흡수하지만, trailingPE가 장기간 전부 비면 밸류에이션 기록이 멈추므로
#     data/valuation/sector_valuation.jsonl 의 증가 여부를 주기적으로 확인할 것.
# --------------------------------------------------
