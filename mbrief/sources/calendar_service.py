"""경제 캘린더 — 고임팩트 발표의 **일정과 결과**.

구성:
  1. FOMC 일정    : Federal Reserve 공식 캘린더 HTML 파싱 (1차 출처, 키 불필요)
  2. 지표 결과    : BLS 공개 API v1 (키 불필요) — CPI/Core CPI/PPI/실업률/비농업고용 등
  3. 어닝 일정    : Finnhub /calendar/earnings
  4. 경제지표 일정: Finnhub /calendar/economic (유료 플랜 전용 — 무료 키는 403이며 빈 배열로 처리)

v1 services/calendar_service.py를 이식했다. 변경점:
  - Finnhub API 키를 쿼리스트링이 아니라 X-Finnhub-Token 헤더로 전송한다.

BLS 공개 API는 키 없이 하루 25회로 제한되므로 반드시 캐시를 경유한다.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from mbrief.sources import httpguard
from mbrief.sources.news_collector import finnhub_headers

logger = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"
BLS_API = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

# [SECURE] 허용 호스트를 위 URL 상수에서만 파생 - 리다이렉트 경유 SSRF 방지 (Category 1)
ALLOWED_HOSTS = httpguard.hosts_from_urls([FINNHUB_BASE, BLS_API, FOMC_CALENDAR_URL])
_GUARD_HOOKS = httpguard.async_event_hooks(ALLOWED_HOSTS)

# [SECURE] 고정 HTTPS URL만 사용 - SSRF 방지 (Category 1)
UA_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MarketBriefBot/1.0)"}

# [SECURE] 응답 크기 상한 - 파서 메모리 고갈 방지 (Category 1)
_MAX_HTML_BYTES = 3 * 1024 * 1024

# 관심 종목 실적 발표 우선 표시
MAJOR_SYMBOLS = {
    "NVDA", "AAPL", "MSFT", "META", "GOOGL", "AMZN", "TSLA", "AMD",
    "INTC", "AVGO", "TSM", "QCOM", "MU", "NFLX", "CRM", "JPM", "GS",
    "BAC", "V", "MA", "PYPL", "COIN", "PLTR",
}

# ===== BLS 시리즈 정의 =====
# kind:
#   index → 지수. 전월(전분기) 대비 % 변화가 헤드라인
#   rate  → 비율(%). 값 자체가 헤드라인, 변화는 %p
#   level → 수준(천명). 전월 대비 증감이 헤드라인 (예: 비농업 고용 = NFP)
#   pct   → 값 자체가 이미 % 변화율 (생산성, 단위노동비용)
BLS_SERIES: list[dict] = [
    {"id": "CES0000000001",  "kind": "level", "unit": "천명", "impact": "high",
     "label": "비농업 고용 (Nonfarm Payrolls)"},
    {"id": "LNS14000000",    "kind": "rate",  "unit": "%",    "impact": "high",
     "label": "실업률 (Unemployment Rate)"},
    {"id": "CUUR0000SA0",    "kind": "index", "unit": "%",    "impact": "high",
     "label": "소비자물가 CPI"},
    {"id": "CUUR0000SA0L1E", "kind": "index", "unit": "%",    "impact": "high",
     "label": "근원 CPI (Core CPI)"},
    {"id": "WPUFD4",         "kind": "index", "unit": "%",    "impact": "high",
     "label": "생산자물가 PPI"},
    {"id": "PRS85006092",    "kind": "pct",   "unit": "%",    "impact": "high",
     "label": "비농업 생산성 (Nonfarm Productivity)"},
    {"id": "PRS85006112",    "kind": "pct",   "unit": "%",    "impact": "medium",
     "label": "단위노동비용 (Unit Labor Costs)"},
    {"id": "CES0500000003",  "kind": "index", "unit": "%",    "impact": "medium",
     "label": "시간당 평균임금 (Avg Hourly Earnings)"},
    {"id": "LNS11300000",    "kind": "rate",  "unit": "%",    "impact": "medium",
     "label": "경제활동참가율 (Participation Rate)"},
]
_BLS_BY_ID = {s["id"]: s for s in BLS_SERIES}

MONTH_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

# ===== TTL 캐시 =====
# BLS는 무키 25회/일 제한이므로 6시간 캐시(=하루 4회)로 여유를 둔다.
_BLS_TTL = 6 * 3600
_FOMC_TTL = 24 * 3600
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = asyncio.Lock()


def _cache_get(key: str, ttl: int) -> object | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    stored_at, value = entry
    if time.monotonic() - stored_at > ttl:
        return None
    return value


def _cache_put(key: str, value: object) -> None:
    _cache[key] = (time.monotonic(), value)


async def _fetch(client: httpx.AsyncClient, url: str, params: dict) -> dict | list | None:
    """Finnhub GET. 키는 헤더로만 전달한다."""
    try:
        async with asyncio.timeout(10):
            # [SECURE] API 키를 헤더로 전달 - 쿼리스트링/로그 노출 방지 (Category 2)
            resp = await client.get(url, params=params, headers=finnhub_headers())
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            # 무료 플랜은 economic calendar 미지원. 정상 동작이므로 경고 수준을 낮춘다.
            logger.info("[Calendar] %s 접근 불가(플랜 제한) - 건너뜀", url.rsplit("/", 1)[-1])
        else:
            logger.warning("[Calendar] 요청 실패 %s: %s", url, e)
        return None
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as e:
        # [SECURE] 상세 오류는 로컬 로그로만 (Category 4)
        logger.warning("[Calendar] 요청 실패 %s: %s", url, e)
        return None


# ===== 1. FOMC 일정 =====
def _parse_fomc_html(html: str) -> list[dict]:
    """Fed 공식 캘린더 HTML → FOMC 회의 일정 목록."""
    meetings: list[dict] = []
    # 연도 패널 단위로 분리 (패널마다 'YYYY FOMC Meetings' 헤더가 있다)
    panels = re.split(r'(?=<div class="panel panel-default)', html)

    for panel in panels:
        year_match = re.search(r"(\d{4})\s+FOMC Meetings", panel)
        if not year_match:
            continue
        year = int(year_match.group(1))

        months = re.findall(r"fomc-meeting__month[^>]*>\s*(?:<strong>)?\s*([A-Za-z/]+)", panel)
        days = re.findall(r"fomc-meeting__date[^>]*>\s*([^<]+?)\s*<", panel)

        for month_raw, day_raw in zip(months, days):
            parsed = _parse_meeting_date(year, month_raw, day_raw)
            if parsed is not None:
                meetings.append(parsed)

    return meetings


def _parse_meeting_date(year: int, month_raw: str, day_raw: str) -> dict | None:
    """'April' + '28-29*' → 시작·종료일. 'Apr/May' + '30-1' 같은 월 경계도 처리."""
    # '*'는 경제전망요약(SEP) 발표 회의 → 시장 영향이 더 크다
    has_sep = "*" in day_raw
    cleaned = day_raw.replace("*", "").strip()
    # '22 (notation vote)' 같은 비정규 표기는 숫자만 취한다
    day_numbers = re.findall(r"\d+", cleaned)
    if not day_numbers:
        return None

    month_parts = [m for m in re.split(r"/", month_raw) if m]
    start_month = MONTH_NUM.get(_full_month(month_parts[0]))
    if start_month is None:
        return None
    end_month = MONTH_NUM.get(_full_month(month_parts[-1]), start_month)

    try:
        start_day = int(day_numbers[0])
        end_day = int(day_numbers[-1])
        start = date(year, start_month, start_day)
        # '30-1'처럼 월을 넘어가면 종료일은 다음 달
        end_year = year + 1 if end_month < start_month else year
        end = date(end_year, end_month, end_day)
        if end < start:  # 월 표기가 하나뿐인데 일자가 역전되면 다음 달로 간주
            end = date(end_year + (1 if end_month == 12 else 0), end_month % 12 + 1, end_day)
    except ValueError as e:
        logger.debug("[FOMC] 날짜 파싱 실패 %s %s %s: %s", year, month_raw, day_raw, e)
        return None

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "label": "FOMC 정례회의",
        "hasSEP": has_sep,       # 점도표·경제전망 동시 발표 여부
        "impact": "high",
        "note": "성명서 + 점도표(SEP) + 파월 기자회견" if has_sep else "성명서 + 파월 기자회견",
    }


def _full_month(token: str) -> str:
    token = token.strip().lower()
    for name in MONTH_NUM:
        if name.startswith(token[:3]):
            return name
    return token


async def get_fomc_schedule(past_days: int = 45, future_days: int = 270) -> list[dict]:
    """FOMC 일정 (직전 회의 + 향후 회의). 24시간 캐시."""
    async with _cache_lock:
        cached = _cache_get("fomc", _FOMC_TTL)
        if cached is not None:
            meetings = cached
        else:
            meetings = []
            try:
                async with httpx.AsyncClient(
                    timeout=20, follow_redirects=True, event_hooks=_GUARD_HOOKS,
                ) as client:
                    resp = await client.get(FOMC_CALENDAR_URL, headers=UA_HEADERS)
                    resp.raise_for_status()
                    if len(resp.content) > _MAX_HTML_BYTES:
                        logger.warning("[FOMC] 응답 크기 초과 - 건너뜀")
                    else:
                        meetings = _parse_fomc_html(resp.text)
                        logger.info("[FOMC] 일정 %d건 파싱", len(meetings))
            except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as e:
                logger.warning("[FOMC] 일정 수집 실패: %s", e)
            _cache_put("fomc", meetings)

    today = datetime.now(timezone.utc).date()
    lo, hi = today - timedelta(days=past_days), today + timedelta(days=future_days)

    result = []
    for m in meetings:  # type: ignore[union-attr]
        try:
            start = date.fromisoformat(m["start"])
            end = date.fromisoformat(m["end"])
        except (ValueError, KeyError, TypeError):
            logger.debug("[FOMC] 캐시 항목 형식 오류 - 건너뜀")
            continue
        if not (lo <= end and start <= hi):
            continue
        result.append({
            **m,
            "isPast": end < today,
            "isLive": start <= today <= end,
            "daysUntil": (start - today).days,
        })

    result.sort(key=lambda x: x["start"])
    return result


# ===== 2. 지표 결과 (BLS) =====
def _to_float(value: object) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _period_label(entry: dict) -> str:
    year = entry.get("year", "")
    name = entry.get("periodName", "")
    return f"{year} {name}".strip()


def _is_periodic(entry: dict) -> bool:
    """월(M01~M12)·분기(Q01~Q04) 관측치만 사용.

    BLS는 연간평균(M13/Q05)을 같은 시리즈에 섞어 내려주므로, 이를 걸러내지 않으면
    '전기 대비' 비교가 엉뚱한 값과 이뤄진다.
    """
    period = str(entry.get("period", ""))
    return period not in ("M13", "Q05") and bool(period)


def _build_indicator(meta: dict, points: list[dict]) -> dict | None:
    """BLS 관측치 → 화면·프롬프트용 지표 결과."""
    points = [p for p in points if _is_periodic(p)]
    if not points:
        return None

    latest = points[0]
    value = _to_float(latest.get("value"))
    if value is None:
        return None
    prev = _to_float(points[1].get("value")) if len(points) > 1 else None

    kind = meta["kind"]
    display: float | None
    display_unit = meta["unit"]
    # is_delta: display가 '변화량'이면 True(부호 표시), '수준값'이면 False
    is_delta = kind in ("level", "index")

    if kind in ("pct", "rate"):
        display = value
    elif kind == "level":
        display = round(value - prev, 1) if prev is not None else None
    elif kind == "index":
        display = round((value - prev) / prev * 100, 2) if prev else None
    else:
        display, display_unit = None, ""

    # 방향은 항상 '원 지표 값의 전기 대비 변화'를 기준으로 판정한다.
    if prev is None or value == prev:
        direction = "flat"
    else:
        direction = "up" if value > prev else "down"

    change_note = f"이전 {prev:g} → {value:g}" if prev is not None else ""

    return {
        "series": meta["id"],
        "label": meta["label"],
        "impact": meta["impact"],
        "period": _period_label(latest),
        "kind": kind,
        "value": value,
        "prevValue": prev,
        "display": display,
        "displayUnit": display_unit,
        "isDelta": is_delta,
        "changeNote": change_note,
        "direction": direction,
    }


async def get_indicator_results() -> list[dict]:
    """최근 발표된 미국 고임팩트 지표 결과. 6시간 캐시 (BLS 무키 25회/일 제한)."""
    async with _cache_lock:
        cached = _cache_get("bls", _BLS_TTL)
        if cached is not None:
            return cached  # type: ignore[return-value]

        now_year = datetime.now(timezone.utc).year
        results: list[dict] = []
        try:
            async with httpx.AsyncClient(timeout=25, event_hooks=_GUARD_HOOKS) as client:
                async with asyncio.timeout(25):
                    resp = await client.post(
                        BLS_API,
                        json={
                            "seriesid": [s["id"] for s in BLS_SERIES],
                            "startyear": str(now_year - 1),
                            "endyear": str(now_year),
                        },
                        headers={"Content-Type": "application/json"},
                    )
                    resp.raise_for_status()
                    payload = resp.json()

            if payload.get("status") != "REQUEST_SUCCEEDED":
                logger.warning("[BLS] 요청 거절: %s", str(payload.get("message"))[:200])
            else:
                for series in payload.get("Results", {}).get("series", []):
                    meta = _BLS_BY_ID.get(series.get("seriesID", ""))
                    if meta is None:
                        continue
                    indicator = _build_indicator(meta, series.get("data") or [])
                    if indicator is not None:
                        results.append(indicator)
                logger.info("[BLS] 지표 결과 %d건 수집", len(results))
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError, TypeError, KeyError) as e:
            # [SECURE] 상세 오류는 로컬 로그로만 (Category 4)
            logger.warning("[BLS] 지표 수집 실패: %s", e)

        impact_order = {"high": 0, "medium": 1, "low": 2}
        results.sort(key=lambda x: impact_order.get(x["impact"], 2))
        _cache_put("bls", results)
        return results


# ===== 3. Finnhub 경제지표 일정 (유료 플랜 전용) =====
async def get_economic_events(days: int = 5) -> list[dict]:
    """당일부터 N일간의 고영향 경제지표 일정. 무료 플랜은 403 → 빈 목록."""
    now = datetime.now(timezone.utc)
    params = {
        "from": now.strftime("%Y-%m-%d"),
        "to": (now + timedelta(days=days)).strftime("%Y-%m-%d"),
    }
    async with httpx.AsyncClient(timeout=15, event_hooks=_GUARD_HOOKS) as client:
        data = await _fetch(client, f"{FINNHUB_BASE}/calendar/economic", params)

    if not isinstance(data, dict) or "economicCalendar" not in data:
        return []

    events = []
    for ev in data["economicCalendar"]:
        impact = (ev.get("impact") or "").lower()
        country = (ev.get("country") or "").upper()
        if country not in ("US", "EU", "CN", "JP", "KR"):
            continue
        if impact not in ("high", "medium") and country != "US":
            continue
        events.append({
            "event": ev.get("event", ""),
            "time": ev.get("time", ""),
            "country": country,
            "impact": impact,
            "estimate": ev.get("estimate"),
            "prev": ev.get("prev"),
            "actual": ev.get("actual"),
            "unit": ev.get("unit", ""),
        })

    impact_order = {"high": 0, "medium": 1, "low": 2}
    events.sort(key=lambda x: (x.get("time", ""), impact_order.get(x.get("impact", "low"), 2)))
    return events[:30]


# ===== 4. 어닝 일정 =====
async def get_earnings_events(days: int = 45) -> list[dict]:
    """당일부터 N일간의 주요 기업 실적 발표 일정."""
    now = datetime.now(timezone.utc)
    params = {
        "from": now.strftime("%Y-%m-%d"),
        "to": (now + timedelta(days=days)).strftime("%Y-%m-%d"),
    }
    async with httpx.AsyncClient(timeout=15, event_hooks=_GUARD_HOOKS) as client:
        data = await _fetch(client, f"{FINNHUB_BASE}/calendar/earnings", params)

    if not isinstance(data, dict) or "earningsCalendar" not in data:
        return []

    events = []
    for ev in data["earningsCalendar"]:
        symbol = (ev.get("symbol") or "").upper()
        events.append({
            "symbol": symbol,
            "company": ev.get("company", symbol),
            "date": ev.get("date", ""),
            "hour": ev.get("hour", ""),      # bmo=장전, amc=장후
            "epsEstimate": ev.get("epsEstimate"),
            "revenueEstimate": ev.get("revenueEstimate"),
            "isMajor": symbol in MAJOR_SYMBOLS,
        })

    # 관심 종목 실적만 노출한다. 전체를 담으면 하루 수백 개의 초소형주가 쏟아져
    # 캘린더가 알파벳순 티커 나열로 변해 정보 가치가 사라진다.
    major = [e for e in events if e["isMajor"]]
    major.sort(key=lambda x: x["date"])
    return major[:20]


# ===== 통합 =====
# 주요 종목 실적은 분기마다 몰려 있어 7일 창으로는 대부분 빈 목록이 된다.
# 45일이면 다음 분기 실적 시즌의 핵심 종목(NVDA/AVGO/MU 등)이 항상 잡힌다.
async def get_calendar(days_eco: int = 5, days_earn: int = 45) -> dict:
    """4개 소스를 동시 수집. 개별 실패는 빈 목록으로 격리한다."""
    eco, earn, fomc, indicators = await asyncio.gather(
        get_economic_events(days_eco),
        get_earnings_events(days_earn),
        get_fomc_schedule(),
        get_indicator_results(),
        return_exceptions=True,
    )

    def _safe(value: object, label: str) -> list:
        if isinstance(value, BaseException):
            # [SECURE] 개별 소스 실패 격리 + 로깅 - 빈 except 없음 (Category 4)
            logger.warning("[Calendar] %s 수집 실패: %s", label, value)
            return []
        return value if isinstance(value, list) else []

    return {
        "economic": _safe(eco, "economic"),
        "earnings": _safe(earn, "earnings"),
        "fomc": _safe(fomc, "fomc"),
        "indicators": _safe(indicators, "indicators"),
    }


def format_for_llm(calendar: dict) -> str:
    """브리핑 프롬프트용 텍스트. 발표 결과와 예정 일정을 T1 공식 출처로 제시한다."""
    calendar = calendar or {}
    fomc = calendar.get("fomc") or []
    indicators = calendar.get("indicators") or []
    earnings = [e for e in (calendar.get("earnings") or []) if e.get("isMajor")]

    if not (fomc or indicators or earnings):
        return ""

    text = "🏛️ TIER 1 — OFFICIAL ECONOMIC CALENDAR (schedule & released results)\n"
    text += (
        "*** AUTHORITATIVE: FOMC dates come from the Federal Reserve's official calendar; "
        "indicator results come directly from the U.S. Bureau of Labor Statistics. "
        "Use these exact figures. Do NOT invent or alter them. ***\n\n"
    )

    if fomc:
        text += "[FOMC MEETINGS]\n"
        for m in fomc[:5]:
            when = f"{m.get('start')} ~ {m.get('end')}"
            if m.get("isLive"):
                status = "IN PROGRESS TODAY"
            elif m.get("isPast"):
                status = "most recent (concluded)"
            else:
                status = f"upcoming in {m.get('daysUntil')} days"
            sep = " + SEP/dot plot" if m.get("hasSEP") else ""
            text += f"  - {when} [{status}]{sep}\n"
        text += "\n"

    if indicators:
        text += "[LATEST RELEASED U.S. INDICATORS — source: BLS]\n"
        for ind in indicators:
            display = ind.get("display")
            if display is None:
                head_str = "n/a"
            elif ind.get("isDelta"):
                # 변화량이므로 부호를 붙인다 (예: 비농업 고용 -23천명)
                head_str = f"{display:+g}{ind.get('displayUnit', '')} vs prior period"
            else:
                head_str = f"{display:g}{ind.get('displayUnit', '')}"
            prev = ind.get("prevValue")
            prev_str = f", prior {prev:g}" if prev is not None else ""
            text += f"  - {ind.get('label')}: {head_str} | period {ind.get('period')}{prev_str}\n"
        text += "\n"

    if earnings:
        text += "[MAJOR EARNINGS AHEAD]\n"
        hour_label = {"bmo": "before open", "amc": "after close", "dmh": "during market"}
        for e in earnings[:10]:
            when = hour_label.get(e.get("hour", ""), "")
            text += f"  - {e.get('date')} {e.get('symbol')}{(' (' + when + ')') if when else ''}\n"
        text += "\n"

    return text + "---\n\n"


# --------------------------------------------------
# Security Checklist
# Applied:
#   - SSRF: 모든 외부 요청 URL을 소스 상수로 고정, 사용자 입력 URL 미사용.
#     추가로 httpguard 응답 훅이 리다이렉트 종착지의 스킴(https)·호스트(ALLOWED_HOSTS)를 검증한다
#   - Unencrypted Sensitive Data: Finnhub 키를 X-Finnhub-Token 헤더로만 전송 (쿼리스트링 노출 제거)
#   - 자원 고갈 방지: HTML 응답 3MB 상한, 요청 타임아웃(10~25s), 결과 개수 상한
#   - Missing Error Handling: 4개 소스를 독립 격리, 하나가 실패해도 나머지는 반환
#   - Improper Exception Handling: 빈 except 없음, 모든 실패 경로에서 로깅
#   - Error Message Information Exposure: 상세 오류는 로컬 로그에만 기록
#   - Improper Resource Release: httpx.AsyncClient를 async context manager로 사용
#   - Null Pointer Dereference: 파싱 값에 _to_float/기본값 적용 후 None 검사
#   - API Rate Limit 준수: BLS 무키 25회/일 제한에 맞춰 6시간 TTL 캐시 (하루 4회)
# Not Applied:
#   - [WARN] FOMC 일정은 Fed 페이지의 HTML 구조에 의존한다. 구조 변경 시 빈 목록이 되며
#     로그 경고만 남는다. 정기 점검 필요.
#   - [WARN] Improper Certificate Validation: httpx 기본 TLS 검증에 의존 (핀닝 없음).
#   - [WARN] 캐시가 프로세스 메모리에 있어 CLI를 여러 번 실행하면 매번 재요청한다.
#     daily 파이프라인은 1회 실행이므로 현재 사용 패턴에서는 문제되지 않는다.
# --------------------------------------------------
