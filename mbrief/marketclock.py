"""미국 장 시계 — "지금이 언제이고, 시장은 어떤 상태인가".

브리핑이 틀리는 가장 흔한 방식은 사실 오류가 아니라 **시점 오인**이다.
07:00 KST 개장 전에 맞춰 쓴 문장("어젯밤 ... 마감했다")을 장중이나 휴장일에 그대로 쓰면
독자는 이미 지나간 세션을 오늘 일로 읽는다. 실제로 2026-09-07(미국 노동절)에 생성한
브리핑이 직전 금요일(09-04) 종가를 "어젯밤 마감"으로 서술한 사례가 있었다.

그래서 프롬프트에 **현재 시각 + 시장 상태 + 마지막 실제 세션 날짜**를 명시적으로 넣는다.
휴장일 판정은 하드코딩한 휴일 달력이 아니라 **차트 데이터에 실제로 존재하는 세션 날짜**로
한다. 거래가 있었다면 캔들이 있고, 없었다면 없다. 달력은 틀릴 수 있어도 데이터는 틀리지 않는다.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable, Literal
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")

# 미국 정규장·시간외 경계 (ET 기준)
PRE_OPEN = dt.time(4, 0)
REGULAR_OPEN = dt.time(9, 30)
REGULAR_CLOSE = dt.time(16, 0)
AFTER_CLOSE = dt.time(20, 0)

# 평일에 이 시각(ET)이 지났는데도 당일 캔들이 하나도 없으면 휴장으로 본다.
# 정규장 개장 30분 후. 일중 캔들은 개장 직후부터 생기므로 이 정도면 충분하고,
# 노동절처럼 개장 시간대에 걸친 휴장일을 장중으로 오판하지 않는다.
_HOLIDAY_CONFIRM_TIME = dt.time(10, 0)

# 한국 정규장 (KRX). 장중 시세 갱신(tick) 창을 정하는 데 쓴다.
KRX_OPEN = dt.time(9, 0)
KRX_CLOSE = dt.time(15, 30)

State = Literal["pre_market", "open", "after_hours", "closed", "holiday", "weekend"]

_STATE_KO: dict[str, str] = {
    "pre_market": "개장 전 (프리마켓)",
    "open": "정규장 진행 중",
    "after_hours": "시간외 (애프터마켓)",
    "closed": "장 마감",
    "holiday": "휴장일 (당일 세션 없음)",
    "weekend": "주말 휴장",
}

_STATE_EN: dict[str, str] = {
    "pre_market": "PRE-MARKET (regular session has not opened yet)",
    "open": "REGULAR SESSION IN PROGRESS",
    "after_hours": "AFTER-HOURS (regular session already closed today)",
    "closed": "CLOSED (outside all trading hours)",
    "holiday": "CLOSED ALL DAY (US market holiday - no session today)",
    "weekend": "CLOSED (weekend)",
}


@dataclass(frozen=True)
class MarketClock:
    """생성 시점의 시장 시계 스냅샷."""
    now_kst: dt.datetime
    now_et: dt.datetime
    state: State
    last_session: dt.date | None       # 마지막으로 실제 거래가 있었던 날짜
    quotes_are_live: bool              # True면 시세가 장중 실시간, False면 last_session 종가

    @property
    def state_ko(self) -> str:
        return _STATE_KO.get(self.state, self.state)

    @property
    def state_en(self) -> str:
        return _STATE_EN.get(self.state, self.state)


def session_dates(candles: Iterable[dict]) -> list[dt.date]:
    """캔들 목록 → 실제 거래가 있었던 ET 기준 날짜 (오름차순, 중복 제거).

    [SECURE] 외부(yfinance) 데이터의 결측·형식 오류를 개별 무시하고 계속 진행 (Category 5).
    """
    days: set[dt.date] = set()
    for candle in candles or []:
        ts = candle.get("time") if isinstance(candle, dict) else None
        if not isinstance(ts, (int, float)):
            continue
        try:
            days.add(dt.datetime.fromtimestamp(int(ts), ET).date())
        except (OverflowError, OSError, ValueError):
            continue
    return sorted(days)


def _classify(now_et: dt.datetime, today_has_session: bool | None) -> State:
    """ET 현재 시각 + 당일 세션 존재 여부 → 시장 상태.

    today_has_session이 None이면 '아직 알 수 없음'이므로 시간대만으로 판정한다.
    """
    if now_et.weekday() >= 5:
        return "weekend"
    if today_has_session is False:
        return "holiday"

    t = now_et.time()
    if PRE_OPEN <= t < REGULAR_OPEN:
        return "pre_market"
    if REGULAR_OPEN <= t < REGULAR_CLOSE:
        return "open"
    if REGULAR_CLOSE <= t < AFTER_CLOSE:
        return "after_hours"
    return "closed"


def build_clock(candles: Iterable[dict] | None = None,
                now: dt.datetime | None = None) -> MarketClock:
    """차트 캔들과 현재 시각으로 시장 시계를 만든다.

    candles는 일중(1d) 또는 5일(5d) 차트를 그대로 넘기면 된다.
    """
    now_kst = (now or dt.datetime.now(KST)).astimezone(KST)
    now_et = now_kst.astimezone(ET)

    days = session_dates(candles or [])
    last_session = days[-1] if days else None
    today_et = now_et.date()

    if last_session == today_et:
        today_has_session: bool | None = True
    elif now_et.weekday() >= 5:
        today_has_session = False
    elif days and now_et.time() >= _HOLIDAY_CONFIRM_TIME:
        # 평일 개장 30분 후(10:00 ET)가 지났는데 당일 캔들이 없다 = 휴장
        today_has_session = False
    else:
        today_has_session = None

    state = _classify(now_et, today_has_session)
    quotes_are_live = state in ("open", "pre_market", "after_hours") and today_has_session is not False
    return MarketClock(
        now_kst=now_kst,
        now_et=now_et,
        state=state,
        last_session=last_session,
        quotes_are_live=quotes_are_live,
    )


def parse_window(text: str) -> tuple[dt.time, dt.time] | None:
    """"09:00-15:30" → (time(9,0), time(15,30)). 형식이 어긋나면 None.

    [SECURE] 설정 문자열을 신뢰하지 않고 엄격히 파싱한다 - Improper Input 방지 (Category 1).
    """
    parts = (text or "").strip().split("-")
    if len(parts) != 2:
        return None
    bounds: list[dt.time] = []
    for part in parts:
        hm = part.strip().split(":")
        if len(hm) != 2 or not all(x.isdigit() for x in hm):
            return None
        hour, minute = int(hm[0]), int(hm[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        bounds.append(dt.time(hour, minute))
    return bounds[0], bounds[1]


def in_intraday_window(now: dt.datetime | None = None,
                       window: tuple[dt.time, dt.time] | None = None) -> bool:
    """장중 갱신을 돌려야 하는 시각인지 (한국 평일 기준).

    공휴일은 판별하지 않는다. 휴장일에는 시세가 그대로라 상위 호출부가 '변경 없음'으로
    배포를 건너뛰므로, 여기서 달력을 들고 있을 이유가 없다.
    """
    now_kst = (now or dt.datetime.now(KST)).astimezone(KST)
    if now_kst.weekday() >= 5:
        return False
    start, end = window or (KRX_OPEN, KRX_CLOSE)
    return start <= now_kst.time() <= end


def format_for_llm(clock: MarketClock) -> str:
    """프롬프트 최상단에 넣을 시점 블록.

    모델이 시제를 고르는 근거가 되므로 뉴스·시세보다 먼저 제시한다.
    """
    lines = [
        "=== CURRENT TIME & US MARKET STATE (authoritative - overrides any time reference in the data) ===",
        f"Now: {clock.now_kst:%Y-%m-%d %H:%M} KST  /  {clock.now_et:%Y-%m-%d %H:%M} ET ({clock.now_et:%a})",
        f"US market: {clock.state_en}",
    ]
    if clock.last_session:
        lines.append(f"Last completed US session: {clock.last_session:%Y-%m-%d (%a)}")
    if clock.quotes_are_live:
        lines.append("The quotes below are LIVE intraday prices for the session in progress.")
    elif clock.last_session:
        lines.append(
            f"The quotes below are the CLOSING values of {clock.last_session:%Y-%m-%d}, not live prices. "
            "Do not describe them as today's move unless today is that date."
        )

    lines.append("")
    lines.append(_framing_instruction(clock))
    lines.append("")
    return "\n".join(lines)


def _framing_instruction(clock: MarketClock) -> str:
    """시장 상태별로 브리핑의 시제와 초점을 지시한다."""
    if clock.state == "open":
        return (
            "FRAMING: The US session is OPEN right now. Write about the session IN PROGRESS - what is "
            "moving it so far today and what to watch for the remaining hours. Do NOT write "
            "'closed at' or 'last night' about the current session."
        )
    if clock.state == "after_hours":
        return (
            "FRAMING: Today's US regular session has already CLOSED. Lead with how it closed and why, "
            "then cover after-hours moves and what they imply for the next session."
        )
    if clock.state in ("holiday", "weekend"):
        last = f"{clock.last_session:%Y-%m-%d}" if clock.last_session else "the most recent session"
        return (
            f"FRAMING: There is NO US session today ({clock.state_en}). The most recent completed session "
            f"is {last}. Do NOT write 'last night the market closed' as if a session just ended. "
            "Recap that last session, then focus on news and positioning ahead of the NEXT open."
        )
    # pre_market / closed
    return (
        "FRAMING: The US regular session has not opened yet. Recap the last completed session, then cover "
        "what happened overnight, then give the setup for today's open."
    )


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Improper Input for Security Decision: 휴장 판정을 외부 문자열이 아니라 캔들 타임스탬프로만 수행
#   - Null Pointer Dereference: 캔들 결측·형식 오류를 개별 무시하고 기본값(None)으로 안전 처리
#   - Uninitialized Variable: 모든 분기에서 today_has_session/state를 명시적으로 결정
#   - Infinite Loop: 반복은 유한한 캔들 목록 순회뿐
# Not Applied:
#   - [WARN] 휴장 판정은 데이터 기반 휴리스틱이다. refresh가 실패해 차트 캐시가 하루 낡았거나
#     제공자가 당일 캔들을 10:00 ET 이후까지 늦게 반영하면 정규 거래일을 휴장으로 오판할 수 있다.
#     그 경우 시제 서술만 보수적으로 바뀌며 수치 자체는 영향받지 않는다.
#   - [WARN] 반일 조기 폐장(예: 추수감사절 다음 날 13:00 ET 마감)은 구분하지 않는다.
# --------------------------------------------------
