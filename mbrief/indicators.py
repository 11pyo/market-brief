"""가격 기반 기술적 지표 — 순수 함수만 모아 둔다.

여기 있는 함수는 **네트워크도 파일도 건드리지 않는다**. 숫자 리스트를 받아 숫자를 돌려줄 뿐이다.
그래서 손으로 계산한 값과 1:1로 대조하는 단위 테스트(tests/test_indicators.py)가 가능하다.

왜 파이썬에서 계산하는가:
    섹터 '과열도'는 LLM에게 물어볼 값이 아니다. 모델은 가격 시계열을 보지 못하고,
    본 적 없는 수치를 그럴듯하게 지어낼 수 있다. 과열도는 여기서 결정하고,
    모델에는 '이미 계산된 사실'로만 전달한다 (mbrief/llm/prompts.py).

임계값(HEAT_THRESHOLDS)은 사이트·프롬프트가 함께 인용하는 단일 출처다.
숫자를 바꾸려면 여기만 고친다.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

# ===== 상수 =====
MA_WINDOW = 200          # 이격도 기준 이동평균 (일)
RSI_PERIOD = 14          # 일봉 RSI 기간

# 과열도 라벨 임계값 — (라벨, gapPercentile 하한). 위에서부터 '이상' 비교로 처음 맞는 것을 쓴다.
# schemas.HeatLabel 리터럴과 값이 일치해야 한다.
HEAT_THRESHOLDS: tuple[tuple[str, float], ...] = (
    ("hot", 85.0),
    ("warm", 65.0),
    ("neutral", 35.0),
    ("cool", 15.0),
)
HEAT_FLOOR_LABEL = "cold"        # 어떤 임계값에도 못 미칠 때
HEAT_UNKNOWN_LABEL = "neutral"   # 백분위를 계산할 수 없을 때 (호출부는 sampleDays=0으로 구분한다)

# 사람이 읽는 임계값 설명. 프롬프트와 사이트가 같은 문장을 인용하도록 여기서 만든다.
HEAT_RULE_TEXT = (
    "hot >= 85, warm >= 65, neutral >= 35, cool >= 15, cold < 15 "
    "(percentile of today's 200-day moving-average gap within that sector's own trailing gap distribution)"
)


# ===== 내부 헬퍼 =====
def _finite(values: Sequence[float] | None) -> list[float]:
    """유한한 실수만 순서를 유지한 채 남긴다.

    [SECURE] 외부(yfinance) 시계열의 NaN/inf/None/문자열을 계산 전에 제거 - 잘못된 입력으로 인한
    지표 오염 방지 (Category 1: Improper Input Validation).
    """
    out: list[float] = []
    for raw in values or ():
        try:
            number = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue  # [SECURE] 빈 except 없음 - 변환 불가 값은 명시적으로 건너뛴다 (Category 4)
        if math.isfinite(number):
            out.append(number)
    return out


def _positive_int(value: object, name: str) -> int:
    """[SECURE] 창 크기/기간을 양의 정수로 강제 - 음수 슬라이스·0 나눗셈 방지 (Category 1)."""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name}는 정수여야 합니다.") from e
    if number <= 0:
        raise ValueError(f"{name}는 1 이상이어야 합니다.")
    return number


# ===== 이동평균 =====
def sma(values: Sequence[float] | None, window: int) -> float | None:
    """마지막 `window`개의 단순 이동평균. 유효 관측치가 부족하면 None."""
    window = _positive_int(window, "window")
    series = _finite(values)
    if len(series) < window:
        return None
    return sum(series[-window:]) / window


def sma_series(values: Sequence[float] | None, window: int) -> list[float]:
    """창이 채워지는 시점부터의 SMA 목록. 길이는 max(0, len(유효값) - window + 1)."""
    window = _positive_int(window, "window")
    series = _finite(values)
    if len(series) < window:
        return []

    out: list[float] = []
    running = sum(series[:window])
    out.append(running / window)
    # [SECURE] 반복 횟수가 입력 길이로 고정된 유한 루프 - 무한 루프 방지 (Category 3)
    for i in range(window, len(series)):
        running += series[i] - series[i - window]
        out.append(running / window)
    return out


def ma_gap_series(closes: Sequence[float] | None, window: int = MA_WINDOW) -> list[float]:
    """이동평균이 존재하는 모든 날의 이격도 (close / MA - 1) * 100.

    반환 길이는 sma_series와 같다 (마지막 원소가 '오늘'의 이격도).
    """
    window = _positive_int(window, "window")
    series = _finite(closes)
    averages = sma_series(series, window)
    if not averages:
        return []

    aligned = series[window - 1:]
    gaps: list[float] = []
    for close, average in zip(aligned, averages):
        if average == 0:
            continue  # [SECURE] 0 나눗셈 회피 - 해당 날짜만 제외 (Category 5)
        gaps.append((close / average - 1.0) * 100.0)
    return gaps


# ===== RSI (Wilder 평활) =====
def rsi(closes: Sequence[float] | None, period: int = RSI_PERIOD) -> float | None:
    """Wilder 방식 RSI. 관측치가 period+1개 미만이면 None.

    1) 첫 `period`개 변화량의 단순 평균으로 avg_gain / avg_loss를 초기화한다.
    2) 이후 각 변화량마다 avg = (avg * (period - 1) + 현재) / period 로 평활한다.
    3) avg_loss == 0 이면 RSI = 100, avg_gain == 0 이면 RSI = 0.
    """
    period = _positive_int(period, "period")
    series = _finite(closes)
    if len(series) < period + 1:
        return None

    deltas = [series[i] - series[i - 1] for i in range(1, len(series))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # [SECURE] 반복 횟수가 입력 길이로 고정된 유한 루프 - 무한 루프 방지 (Category 3)
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0  # 변화가 전혀 없으면 중립
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ===== 백분위 =====
def percentile_rank(series: Sequence[float] | None, value: float | None) -> float | None:
    """분포 안에서 value의 백분위 (0~100). value 이하인 관측치의 비율이다.

    관측치가 없거나 value가 유한하지 않으면 None.
    """
    observations = _finite(series)
    if not observations:
        return None
    candidate = _finite([value])
    if not candidate:
        return None
    target = candidate[0]

    at_or_below = sum(1 for x in observations if x <= target)
    return (at_or_below / len(observations)) * 100.0


# ===== 과열도 라벨 =====
def heat_label(gap_percentile: float | None) -> str:
    """gapPercentile → 과열도 라벨. 백분위를 모르면 'neutral' (호출부가 sampleDays=0으로 구분).

    이 함수는 **오직 gapPercentile 하나만** 본다. RSI나 상대강도를 임의 가중치로 섞으면
    "왜 hot인가"를 설명할 수 없게 된다.
    """
    candidate = _finite([gap_percentile])
    if not candidate:
        return HEAT_UNKNOWN_LABEL
    value = candidate[0]
    for label, floor in HEAT_THRESHOLDS:
        if value >= floor:
            return label
    return HEAT_FLOOR_LABEL


def pct_return(closes: Sequence[float] | None, lookback: int) -> float | None:
    """`lookback` 거래일 전 대비 수익률 (%). 관측치가 부족하면 None."""
    lookback = _positive_int(lookback, "lookback")
    series = _finite(closes)
    if len(series) < lookback + 1:
        return None
    past = series[-(lookback + 1)]
    if past == 0:
        return None  # [SECURE] 0 나눗셈 회피 (Category 5)
    return (series[-1] / past - 1.0) * 100.0


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Improper Input Validation: 외부 시계열의 NaN/inf/None/비수치 값을 _finite로 제거한 뒤에만 계산.
#     window/period/lookback은 _positive_int로 양의 정수를 강제해 음수 슬라이스와 0 나눗셈을 막는다
#   - Integer Overflow/범위: 백분위는 0~100, 라벨은 고정 리터럴 집합에서만 나온다
#   - Infinite Loop or Recursion: 모든 반복이 입력 길이로 상한이 정해진 for 루프, 재귀 없음
#   - Null Pointer Dereference: None 입력을 빈 시퀀스로 취급하고 결과가 없으면 None을 반환
#   - Improper Exception Handling: 빈 except 없음, 변환 실패는 명시적으로 건너뛰고 계속한다
#   - Improper Resource Release: 파일/소켓/스레드를 사용하지 않는다 (순수 계산 모듈)
# Not Applied:
#   - [WARN] 이 모듈은 인가(authorization) 판단을 하지 않는다. 어떤 심볼의 시계열을 넣을지는
#     호출부(mbrief/sources/market_data.py)의 화이트리스트 책임이다.
#   - [WARN] 부동소수점 누적 오차: sma_series는 슬라이딩 합을 재사용해 O(n)으로 계산한다.
#     2년치(500여 개) 종가에서는 오차가 표시 자릿수 아래지만, 훨씬 긴 시계열에는 재합산이 필요하다.
# --------------------------------------------------
