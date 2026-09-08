"""섹터 PER/PBR 일별 기록 테스트 — 파일은 tmp로 격리되고 네트워크는 쓰지 않는다.

data/valuation/sector_valuation.jsonl 은 이 프로젝트에서 **재생성이 불가능한 유일한 파일**이다
(과거 PER 은 소급 수집할 수 없다). 그래서 여기서 고정하는 계약은 셋이다:
  1. append 전용 — 어떤 실패에도 기존 기록을 덮어쓰지 않는다.
  2. (date, symbol) 중복 금지 — 같은 날 daily 를 두 번 돌려도 분포가 이중 계상되지 않는다.
  3. 손상된 줄 하나가 나머지 기록 전체를 못 쓰게 만들지 않는다.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

import pytest

from mbrief import paths, pipeline, store
from mbrief.sources import market_data

XLK = "XLK"
XLF = "XLF"


def row(date_str: str, symbol: str = XLK, pe: float | None = 30.0,
        pb: float | None = 1.25, dy: float | None = 0.0043) -> dict:
    return {"date": date_str, "symbol": symbol, "trailingPE": pe, "priceToBook": pb, "dividendYield": dy}


def dates(count: int, start: str = "2026-01-01") -> list[str]:
    first = date.fromisoformat(start)
    return [(first + timedelta(days=i)).isoformat() for i in range(count)]


def raw_lines() -> list[str]:
    return paths.VALUATION_FILE.read_text(encoding="utf-8").splitlines()


# ===== 기록 · 재적재 =====
def test_append_then_load_roundtrip():
    assert store.append_valuation([row("2026-09-08"), row("2026-09-08", XLF, pe=16.47)]) == 2

    loaded = store.load_valuation()
    assert len(loaded) == 2
    assert loaded[0] == {
        "date": "2026-09-08", "symbol": "XLK",
        "trailingPE": 30.0, "priceToBook": 1.25, "dividendYield": 0.0043,
    }


def test_file_is_one_json_object_per_line():
    store.append_valuation([row("2026-09-08"), row("2026-09-09")])
    lines = raw_lines()
    assert len(lines) == 2
    for line in lines:
        parsed = json.loads(line)
        assert set(parsed) == {"date", "symbol", "trailingPE", "priceToBook", "dividendYield"}


def test_load_valuation_without_a_file_returns_empty():
    assert not paths.VALUATION_FILE.exists()
    assert store.load_valuation() == []


def test_append_creates_the_directory():
    store.append_valuation([row("2026-09-08")])
    assert paths.VALUATION_FILE.is_file()
    assert paths.VALUATION_FILE.parent.name == "valuation"


# ===== 중복 제거 (같은 날 두 번 실행) =====
def test_rerunning_the_same_day_records_nothing_new():
    batch = [row("2026-09-08"), row("2026-09-08", XLF)]
    assert store.append_valuation(batch) == 2
    assert store.append_valuation(batch) == 0        # daily 재실행
    assert len(store.load_valuation()) == 2
    assert len(raw_lines()) == 2


def test_duplicates_inside_one_batch_are_collapsed():
    assert store.append_valuation([row("2026-09-08"), row("2026-09-08", pe=99.0)]) == 1
    assert store.load_valuation()[0]["trailingPE"] == 30.0   # 먼저 온 값이 남는다


def test_a_new_day_still_appends_after_a_duplicate_run():
    store.append_valuation([row("2026-09-08")])
    store.append_valuation([row("2026-09-08")])
    assert store.append_valuation([row("2026-09-09")]) == 1
    assert [r["date"] for r in store.load_valuation()] == ["2026-09-08", "2026-09-09"]


def test_append_never_rewrites_existing_lines():
    store.append_valuation([row("2026-09-08")])
    first = paths.VALUATION_FILE.read_text(encoding="utf-8")
    store.append_valuation([row("2026-09-09")])
    assert paths.VALUATION_FILE.read_text(encoding="utf-8").startswith(first)


# ===== 입력 검증 =====
@pytest.mark.parametrize("bad_date", ["", "2026-9-8", "20260908", "../../etc", None])
def test_rows_with_a_bad_date_are_not_recorded(bad_date):
    assert store.append_valuation([row(bad_date)]) == 0      # type: ignore[arg-type]
    assert store.load_valuation() == []


@pytest.mark.parametrize("bad_symbol", ["", "x" * 20, "XL/K", "../XLK", "XL K"])
def test_rows_with_a_bad_symbol_are_not_recorded(bad_symbol):
    assert store.append_valuation([row("2026-09-08", bad_symbol)]) == 0
    assert store.load_valuation() == []


def test_symbols_are_normalised_to_upper_case():
    store.append_valuation([row("2026-09-08", "xlk")])
    assert store.load_valuation()[0]["symbol"] == "XLK"


@pytest.mark.parametrize("bad_value", [None, "n/a", float("nan"), float("inf"), True])
def test_non_finite_numbers_are_stored_as_null(bad_value):
    store.append_valuation([row("2026-09-08", pe=bad_value)])   # type: ignore[arg-type]
    assert store.load_valuation()[0]["trailingPE"] is None


def test_non_dict_rows_are_ignored():
    assert store.append_valuation(["not a row", None, 42]) == 0   # type: ignore[list-item]
    assert store.append_valuation([]) == 0
    assert store.append_valuation(None) == 0                      # type: ignore[arg-type]


# ===== 손상 내성 =====
def test_a_corrupt_line_is_skipped_and_the_rest_survive(caplog):
    store.append_valuation([row("2026-09-08"), row("2026-09-09")])
    with paths.VALUATION_FILE.open("a", encoding="utf-8") as f:
        f.write("{ this is not json\n")
        f.write('{"date": "nope", "symbol": "XLK"}\n')   # JSON이지만 형식 위반
        f.write("\n")                                     # 빈 줄은 조용히 무시
    store.append_valuation([row("2026-09-10")])

    with caplog.at_level("WARNING"):
        loaded = store.load_valuation()
    assert [r["date"] for r in loaded] == ["2026-09-08", "2026-09-09", "2026-09-10"]
    assert any("손상" in message for message in caplog.messages)


def test_a_truncated_last_line_does_not_hide_earlier_records():
    store.append_valuation([row("2026-09-08")])
    with paths.VALUATION_FILE.open("a", encoding="utf-8") as f:
        f.write('{"date": "2026-09-09", "symbol": "XL')   # 전원 차단 시뮬레이션
    assert len(store.load_valuation()) == 1


def test_oversized_file_is_skipped(monkeypatch):
    store.append_valuation([row("2026-09-08")])
    monkeypatch.setattr(store, "_MAX_VALUATION_BYTES", 1)
    assert store.load_valuation() == []


# ===== 조회 필터 =====
def test_load_valuation_filters_by_symbol():
    store.append_valuation([row("2026-09-08"), row("2026-09-08", XLF), row("2026-09-09")])
    assert len(store.load_valuation(symbol=XLK)) == 2
    assert len(store.load_valuation(symbol="xlf")) == 1
    assert store.load_valuation(symbol="XLE") == []


def test_load_valuation_rejects_a_malformed_symbol_filter():
    store.append_valuation([row("2026-09-08")])
    assert store.load_valuation(symbol="../../etc/passwd") == []


def test_load_valuation_limits_to_the_most_recent_days():
    all_dates = dates(5)
    store.append_valuation([row(d) for d in all_dates] + [row(d, XLF) for d in all_dates])

    recent = store.load_valuation(limit_days=2)
    assert {r["date"] for r in recent} == set(all_dates[-2:])
    assert len(recent) == 4                      # 2일 x 2심볼
    assert len(store.load_valuation(limit_days=0)) == 0


def test_load_valuation_returns_rows_in_file_order():
    ordered = dates(4)
    store.append_valuation([row(d) for d in ordered])
    assert [r["date"] for r in store.load_valuation()] == ordered


# ===== build_sector_valuation =====
def test_percentile_stays_none_until_the_minimum_is_reached():
    need = market_data.MIN_VALUATION_DAYS
    history = [row(d, XLK, pe=float(i + 1)) for i, d in enumerate(dates(need - 1))]
    built = market_data.build_sector_valuation({XLK: {"trailingPE": 30.0}}, history)["Technology"]

    assert built.pePercentile is None
    assert built.recordedDays == need - 1        # 진척도는 그대로 보여 준다
    assert built.trailingPE == 30.0


def test_percentile_is_filled_once_the_minimum_is_reached():
    need = market_data.MIN_VALUATION_DAYS
    # PER 관측치 1..60. 현재 PER 30 이하인 관측치는 30개 → 백분위 50.0
    history = [row(d, XLK, pe=float(i + 1)) for i, d in enumerate(dates(need))]
    built = market_data.build_sector_valuation({XLK: {"trailingPE": 30.0}}, history)["Technology"]

    assert built.recordedDays == need
    assert built.pePercentile == pytest.approx(50.0)


def test_min_valuation_days_is_sixty():
    assert market_data.MIN_VALUATION_DAYS == 60


def test_percentile_needs_a_current_pe_to_rank():
    need = market_data.MIN_VALUATION_DAYS
    history = [row(d, XLK, pe=float(i + 1)) for i, d in enumerate(dates(need))]
    built = market_data.build_sector_valuation({XLK: {"trailingPE": None}}, history)["Technology"]
    assert built.pePercentile is None
    assert built.recordedDays == need


def test_rows_without_a_pe_count_as_recorded_days_but_not_as_samples():
    need = market_data.MIN_VALUATION_DAYS
    history = [row(d, XLK, pe=None) for d in dates(need)]
    built = market_data.build_sector_valuation({XLK: {"trailingPE": 30.0}}, history)["Technology"]
    assert built.recordedDays == need
    assert built.pePercentile is None      # PER 표본이 0개라 순위를 매길 수 없다


def test_build_sector_valuation_maps_symbols_to_display_names():
    built = market_data.build_sector_valuation(
        {XLK: {"trailingPE": 33.15, "priceToBook": 1.25, "dividendYield": 0.0043},
         XLF: {"trailingPE": 16.47}},
        [],
    )
    assert set(built) == {"Technology", "Financials"}
    assert built["Technology"].symbol == XLK
    assert built["Technology"].dividendYield == pytest.approx(0.0043)
    assert built["Technology"].recordedDays == 0


def test_build_sector_valuation_tolerates_missing_fields():
    built = market_data.build_sector_valuation({XLK: {}}, [])["Technology"]
    assert built.trailingPE is None and built.priceToBook is None and built.dividendYield is None
    assert built.pePercentile is None and built.recordedDays == 0


def test_build_sector_valuation_ignores_unknown_symbols():
    built = market_data.build_sector_valuation({"SPY": {"trailingPE": 25.0}}, [])
    assert built == {}


def test_build_sector_valuation_handles_empty_input():
    assert market_data.build_sector_valuation({}, []) == {}
    assert market_data.build_sector_valuation(None, None) == {}


def test_recorded_days_counts_distinct_dates_only():
    history = [row("2026-09-08"), row("2026-09-08"), row("2026-09-09")]
    built = market_data.build_sector_valuation({XLK: {"trailingPE": 30.0}}, history)["Technology"]
    assert built.recordedDays == 2


# ===== 수집 → 기록 행 변환 =====
def test_valuation_rows_shape_matches_the_store_contract():
    rows = market_data.valuation_rows(
        {XLK: {"trailingPE": 33.15, "priceToBook": 1.25, "dividendYield": 0.0043}}, "2026-09-08",
    )
    assert rows == [{"date": "2026-09-08", "symbol": XLK,
                     "trailingPE": 33.15, "priceToBook": 1.25, "dividendYield": 0.0043}]
    assert store.append_valuation(rows) == 1


def test_valuation_rows_skips_symbols_with_no_usable_values():
    rows = market_data.valuation_rows(
        {XLK: {"trailingPE": 33.15}, XLF: {"trailingPE": None, "priceToBook": None, "dividendYield": None}},
        "2026-09-08",
    )
    assert [r["symbol"] for r in rows] == [XLK]


def test_valuation_rows_handles_empty_input():
    assert market_data.valuation_rows({}, "2026-09-08") == []
    assert market_data.valuation_rows(None, "2026-09-08") == []


# ===== 수집기 (get_sector_fundamentals) =====
def test_get_sector_fundamentals_covers_every_sector(monkeypatch):
    monkeypatch.setattr(
        market_data, "_fetch_fundamentals_sync",
        lambda symbol: {"trailingPE": 20.0, "priceToBook": 2.0, "dividendYield": 0.01},
    )
    found = asyncio.run(market_data.get_sector_fundamentals())
    expected = set(market_data.SECTOR_SYMBOLS.values()) | set(market_data.THEME_SYMBOLS.values())
    assert set(found) == expected               # 대분류 + 테마 전부 수집한다


def test_get_sector_fundamentals_isolates_failures(monkeypatch):
    def fetch(symbol):
        if symbol == XLK:
            raise RuntimeError("yfinance info 실패")
        return {"trailingPE": 20.0, "priceToBook": None, "dividendYield": None}

    monkeypatch.setattr(market_data, "_fetch_fundamentals_sync", fetch)
    found = asyncio.run(market_data.get_sector_fundamentals())
    assert XLK not in found
    expected = set(market_data.SECTOR_SYMBOLS.values()) | set(market_data.THEME_SYMBOLS.values())
    assert len(found) == len(expected) - 1        # 실패한 하나만 빠지고 나머지는 살아남는다


# ===== pipeline 배선 =====
def test_record_sector_valuation_appends_and_fills_the_bundle(monkeypatch):
    monkeypatch.setattr(
        market_data, "_fetch_fundamentals_sync",
        lambda symbol: {"trailingPE": 20.0, "priceToBook": 2.0, "dividendYield": 0.01},
    )
    bundle = market_data.to_bundle({}, {}, None, {})

    total = len(market_data.ALL_HEAT_SYMBOLS)          # 대분류 + 테마
    written = asyncio.run(pipeline.record_sector_valuation(bundle))
    assert written == total
    assert set(bundle.sectorValuation) == set(market_data.ALL_HEAT_SYMBOLS)
    assert bundle.sectorValuation["Technology"].trailingPE == 20.0
    assert bundle.sectorValuation["Technology"].recordedDays == 1
    assert bundle.sectorValuation["Technology"].pePercentile is None   # 아직 하루치뿐
    # group 태깅: 대분류와 테마가 구분돼 들어간다
    assert bundle.sectorValuation["Technology"].group == "sector"
    assert bundle.sectorValuation["반도체"].group == "theme"

    # 같은 날 두 번째 실행은 한 줄도 추가하지 않는다.
    assert asyncio.run(pipeline.record_sector_valuation(bundle)) == 0
    assert len(store.load_valuation()) == total


def test_record_sector_valuation_survives_a_collection_failure(monkeypatch):
    async def boom():
        raise RuntimeError("수집 실패")

    monkeypatch.setattr(market_data, "get_sector_fundamentals", boom)
    bundle = market_data.to_bundle({}, {}, None, {})
    assert asyncio.run(pipeline.record_sector_valuation(bundle)) == 0
    assert bundle.sectorValuation == {}


def test_offline_refresh_does_not_touch_the_valuation_log():
    """픽스처 값이 실제 기록에 섞이면 분포가 영구히 오염된다."""
    asyncio.run(pipeline.refresh(offline=True))
    assert not paths.VALUATION_FILE.exists()


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Path Traversal: 날짜·심볼에 경로 구분자·상위 경로 표기가 들어간 행이 기록되지 않고,
#     조회 심볼 필터도 화이트리스트 밖 값을 거부하는지 회귀로 고정
#   - Improper Input Validation: NaN/inf/bool/문자열 수치가 null로 저장되는지 검증
#   - Race Condition (TOCTOU): (date, symbol) 중복 기록이 재실행에서 발생하지 않는지,
#     append가 기존 줄을 덮어쓰지 않는지 검증
#   - Missing Error Handling: 손상 줄·절단된 마지막 줄·수집 실패에서 나머지 기록과
#     파이프라인이 살아남는지 검증
#   - 자원 고갈 방지: 파일 크기 상한 초과 시 조회가 중단되는지 검증
#   - 테스트 격리: conftest가 paths.VALUATION_FILE을 tmp로 바꿔치기하므로 실제 data/를 건드리지 않는다
# Not Applied:
#   - [WARN] 해당 없음 - 파일 입출력은 tmp 격리, 네트워크 호출은 monkeypatch로 대체한다
# --------------------------------------------------
