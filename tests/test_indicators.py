"""mbrief/indicators.py 단위 테스트 — 손으로 계산한 값과 1:1 대조.

이 모듈은 순수 함수만 담고 있으므로 네트워크도 파일도 필요 없다.
숫자가 틀리면 섹터 과열도 전체가 조용히 틀리기 때문에, 산식 하나하나를 고정한다.
"""
from __future__ import annotations

import math

import pytest

from mbrief import indicators as ind


# ===== SMA =====
def test_sma_is_the_mean_of_the_trailing_window():
    # (3 + 4 + 5) / 3 = 4.0
    assert ind.sma([1, 2, 3, 4, 5], 3) == pytest.approx(4.0)
    # (1 + 2 + 3 + 4 + 5) / 5 = 3.0
    assert ind.sma([1, 2, 3, 4, 5], 5) == pytest.approx(3.0)


def test_sma_returns_none_when_the_window_is_not_filled():
    assert ind.sma([1, 2, 3], 5) is None
    assert ind.sma([], 1) is None
    assert ind.sma(None, 3) is None


def test_sma_series_walks_the_window_forward():
    # 창 3개: [1,2,3]=2.0, [2,3,4]=3.0, [3,4,5]=4.0
    assert ind.sma_series([1, 2, 3, 4, 5], 3) == pytest.approx([2.0, 3.0, 4.0])
    assert ind.sma_series([1, 2], 3) == []


def test_sma_series_length_is_len_minus_window_plus_one():
    values = list(range(1, 51))
    assert len(ind.sma_series(values, 10)) == len(values) - 10 + 1


def test_non_finite_and_non_numeric_values_are_dropped_before_the_math():
    # None/NaN/문자열을 걸러내면 [1,2,3,4,5]가 남는다 → 마지막 3개 평균 4.0
    dirty = [1, None, 2, float("nan"), 3, "x", 4, float("inf"), 5]
    assert ind.sma(dirty, 3) == pytest.approx(4.0)


@pytest.mark.parametrize("window", [0, -1, "abc", None])
def test_sma_rejects_a_non_positive_window(window):
    with pytest.raises(ValueError):
        ind.sma([1, 2, 3], window)


# ===== 이격도 =====
def test_ma_gap_series_matches_hand_computed_percentages():
    # 창 3: (3/2-1)*100=50, (4/3-1)*100=33.333..., (5/4-1)*100=25
    gaps = ind.ma_gap_series([1, 2, 3, 4, 5], window=3)
    assert gaps == pytest.approx([50.0, 100.0 / 3.0, 25.0])


def test_ma_gap_series_is_empty_when_the_ma_does_not_exist_yet():
    assert ind.ma_gap_series([1, 2, 3], window=200) == []


def test_ma_gap_series_is_zero_for_a_flat_series():
    assert ind.ma_gap_series([100.0] * 10, window=5) == pytest.approx([0.0] * 6)


def test_ma_gap_series_default_window_is_200():
    assert ind.MA_WINDOW == 200
    assert ind.ma_gap_series([100.0] * 199) == []
    assert len(ind.ma_gap_series([100.0] * 200)) == 1


# ===== RSI (Wilder) =====
def test_rsi_period_two_hand_computed():
    """closes=[10,11,10,11], period=2.

    변화량: +1, -1, +1
    초기: avg_gain=(1+0)/2=0.5, avg_loss=(0+1)/2=0.5
    평활: avg_gain=(0.5*1+1)/2=0.75, avg_loss=(0.5*1+0)/2=0.25
    RS=3 → RSI = 100 - 100/4 = 75
    """
    assert ind.rsi([10, 11, 10, 11], period=2) == pytest.approx(75.0)


def test_rsi_is_100_when_every_change_is_a_gain():
    assert ind.rsi(list(range(1, 20)), period=14) == pytest.approx(100.0)


def test_rsi_is_0_when_every_change_is_a_loss():
    assert ind.rsi(list(range(20, 1, -1)), period=14) == pytest.approx(0.0)


def test_rsi_is_50_for_a_perfectly_flat_series():
    # 상승도 하락도 없으면 RS를 정의할 수 없다 - 중립(50)으로 둔다.
    assert ind.rsi([100.0] * 20, period=14) == pytest.approx(50.0)


def test_rsi_needs_period_plus_one_observations():
    assert ind.rsi([1, 2, 3], period=14) is None
    assert ind.rsi(list(range(15)), period=14) is not None


# Wilder "New Concepts in Technical Trading Systems"의 예제 종가 (널리 인용되는 검증 데이터셋).
_WILDER_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
    45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03,
]


@pytest.mark.parametrize(
    ("length", "expected"),
    [(15, 70.46), (16, 66.25), (17, 66.48)],
)
def test_rsi_matches_the_wilder_reference_series(length, expected):
    assert ind.rsi(_WILDER_CLOSES[:length], period=14) == pytest.approx(expected, abs=0.01)


@pytest.mark.parametrize("period", [0, -3, "x"])
def test_rsi_rejects_a_non_positive_period(period):
    with pytest.raises(ValueError):
        ind.rsi([1, 2, 3, 4], period)


# ===== 백분위 =====
def test_percentile_rank_is_the_share_of_observations_at_or_below():
    series = [1, 2, 3, 4]
    assert ind.percentile_rank(series, 3) == pytest.approx(75.0)   # 1,2,3 → 3/4
    assert ind.percentile_rank(series, 4) == pytest.approx(100.0)  # 전부
    assert ind.percentile_rank(series, 1) == pytest.approx(25.0)   # 1개
    assert ind.percentile_rank(series, 0) == pytest.approx(0.0)    # 없음


def test_percentile_rank_counts_ties_as_at_or_below():
    assert ind.percentile_rank([5, 5, 5, 9], 5) == pytest.approx(75.0)


def test_percentile_rank_returns_none_without_usable_input():
    assert ind.percentile_rank([], 1) is None
    assert ind.percentile_rank(None, 1) is None
    assert ind.percentile_rank([1, 2, 3], None) is None
    assert ind.percentile_rank([1, 2, 3], float("nan")) is None


def test_percentile_rank_ignores_non_finite_observations():
    # NaN/None을 걸러내면 [1,2,3,4]가 남아 3의 백분위는 75가 된다.
    assert ind.percentile_rank([1, None, 2, float("nan"), 3, 4], 3) == pytest.approx(75.0)


# ===== 과열도 라벨 =====
@pytest.mark.parametrize(
    ("percentile", "label"),
    [
        (100.0, "hot"),
        (85.0, "hot"),      # 경계: 85 이상은 hot
        (84.99, "warm"),
        (65.0, "warm"),     # 경계: 65 이상은 warm
        (64.99, "neutral"),
        (35.0, "neutral"),  # 경계: 35 이상은 neutral
        (34.99, "cool"),
        (15.0, "cool"),     # 경계: 15 이상은 cool
        (14.99, "cold"),
        (0.0, "cold"),
    ],
)
def test_heat_label_thresholds_including_exact_boundaries(percentile, label):
    assert ind.heat_label(percentile) == label


def test_heat_label_is_neutral_when_the_percentile_is_unknown():
    # '모름'은 별도 라벨을 두지 않는다 - 호출부가 sampleDays=0으로 구분한다.
    assert ind.heat_label(None) == "neutral"
    assert ind.heat_label(float("nan")) == "neutral"


def test_heat_thresholds_are_a_single_source_of_truth():
    """사이트와 프롬프트가 인용하는 임계값이 상수와 어긋나면 안 된다."""
    assert ind.HEAT_THRESHOLDS == (
        ("hot", 85.0), ("warm", 65.0), ("neutral", 35.0), ("cool", 15.0),
    )
    assert ind.HEAT_FLOOR_LABEL == "cold"
    for label, floor in ind.HEAT_THRESHOLDS:
        assert f"{label} >= {floor:g}" in ind.HEAT_RULE_TEXT
    assert "cold < 15" in ind.HEAT_RULE_TEXT


def test_heat_thresholds_are_strictly_descending():
    floors = [floor for _, floor in ind.HEAT_THRESHOLDS]
    assert floors == sorted(floors, reverse=True)
    assert len(set(floors)) == len(floors)


def test_heat_labels_match_the_schema_literal():
    from mbrief.schemas import SectorHeat

    allowed = {"hot", "warm", "neutral", "cool", "cold"}
    labels = {label for label, _ in ind.HEAT_THRESHOLDS} | {ind.HEAT_FLOOR_LABEL}
    assert labels <= allowed
    for label in labels:
        assert SectorHeat(symbol="XLK", label=label).label == label  # pydantic 리터럴 검증


# ===== 기간 수익률 =====
def test_pct_return_is_a_percentage_change_over_the_lookback():
    assert ind.pct_return([100, 110], 1) == pytest.approx(10.0)
    assert ind.pct_return([100, 105, 90], 2) == pytest.approx(-10.0)


def test_pct_return_returns_none_without_enough_history():
    assert ind.pct_return([100], 1) is None
    assert ind.pct_return([], 1) is None


def test_pct_return_guards_division_by_zero():
    assert ind.pct_return([0.0, 10.0], 1) is None


def test_every_public_function_tolerates_none_input():
    """수집 실패로 None이 흘러들어도 예외 대신 결측을 돌려줘야 한다."""
    assert ind.sma(None, 3) is None
    assert ind.sma_series(None, 3) == []
    assert ind.ma_gap_series(None) == []
    assert ind.rsi(None) is None
    assert ind.percentile_rank(None, None) is None
    assert ind.heat_label(None) == "neutral"
    assert ind.pct_return(None, 1) is None


def test_results_are_always_finite_numbers():
    closes = [100.0 + math.sin(i / 5.0) * 3 for i in range(300)]
    for value in (
        ind.sma(closes, 200),
        ind.rsi(closes),
        ind.percentile_rank(ind.ma_gap_series(closes), 0.0),
        ind.pct_return(closes, 63),
    ):
        assert value is not None and math.isfinite(value)


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Improper Input Validation: None/NaN/inf/문자열 입력이 예외 대신 결측으로 흡수되는지,
#     window/period가 0 이하일 때 ValueError로 거부되는지 회귀로 고정
#   - Improper Input for Security Decision: 라벨 임계값(85/65/35/15)과 라벨 집합이
#     schemas.HeatLabel 리터럴과 일치하는지 검증 - 판정 규칙의 단일 출처를 강제한다
#   - Infinite Loop: 모든 테스트가 유한 길이 입력만 사용하며 네트워크·파일 입출력이 없다
# Not Applied:
#   - [WARN] 해당 없음 - 순수 계산 함수 테스트로 외부 입출력·자격증명이 없다
# --------------------------------------------------
