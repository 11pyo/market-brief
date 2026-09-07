"""저장소 테스트 — 왕복, ID 검증(경로 탈출 차단), 원자적 쓰기, v1 마이그레이션."""
from __future__ import annotations

import json
import re

import pytest

from mbrief import paths, store
from mbrief.schemas import BriefingRecord, MarketBundle, Portfolio, Scorecard, StructuredBriefing


def _record(fixture_json, **overrides) -> BriefingRecord:
    data = fixture_json("briefing_sample.json")
    data.update(overrides)
    return BriefingRecord.model_validate(data)


# ===== ID =====
def test_new_record_id_matches_regex():
    record_id = store.new_record_id("2026-09-07", "ko")
    assert store.RECORD_ID_RE.match(record_id)
    assert record_id.startswith("2026-09-07-ko-")


def test_new_record_id_is_unique():
    ids = {store.new_record_id("2026-09-07", "ko") for _ in range(50)}
    assert len(ids) == 50  # secrets 기반이므로 충돌이 없어야 한다


@pytest.mark.parametrize("date_str,lang", [
    ("2026-9-7", "ko"), ("", "ko"), ("../../etc", "ko"), ("2026/09/07", "ko"),
    ("2026-09-07", "korean"), ("2026-09-07", ""), ("2026-09-07", "k/o"),
])
def test_new_record_id_rejects_bad_input(date_str, lang):
    with pytest.raises(ValueError):
        store.new_record_id(date_str, lang)


def test_new_record_id_normalizes_lang_case():
    assert store.new_record_id("2026-09-07", " KO ").startswith("2026-09-07-ko-")


@pytest.mark.parametrize("bad_id", [
    "../../../etc/passwd",
    "..\\..\\windows\\system32\\config",
    "2026-09-07-ko-a1b2c3/../../evil",
    "2026-09-07-ko-ZZZZZZ",
    "2026-09-07-ko-a1b2c",
    "",
    "a" * 100,
])
def test_record_path_rejects_traversal(bad_id):
    with pytest.raises(ValueError):
        store._record_path(bad_id)


def test_load_record_returns_none_for_traversal_id():
    assert store.load_record("../../secret") is None


@pytest.mark.parametrize("bad_key", ["../evil", "sp500_1y", "SP500_1d", "sp500", "sp500_1d/../x"])
def test_chart_path_rejects_bad_keys(bad_key):
    with pytest.raises(ValueError):
        store._chart_path(bad_key)


def test_chart_path_accepts_valid_keys():
    for key in ("sp500_1d", "usdkrw_5d", "btc_1mo"):
        assert store._chart_path(key).name == f"{key}.json"


# ===== 브리핑 왕복 =====
def test_save_and_load_record_roundtrip(fixture_json):
    record = _record(fixture_json)
    path = store.save_record(record)
    assert path == paths.BRIEFINGS_DIR / f"{record.id}.json"
    assert path.is_file()

    loaded = store.load_record(record.id)
    assert loaded is not None
    assert loaded.id == record.id
    assert loaded.headline == record.headline
    assert loaded.structured is not None
    assert loaded.structured.direction.up == record.structured.direction.up
    assert loaded.market is not None
    assert "S&P 500" in loaded.market.quotes


def test_load_record_missing_returns_none():
    assert store.load_record("2026-01-01-ko-abcdef") is None


def test_list_records_newest_first(fixture_json):
    old = _record(fixture_json, id="2026-09-05-ko-aaaaaa", date="2026-09-05",
                  generatedAt="2026-09-05T07:00:00+09:00")
    new = _record(fixture_json, id="2026-09-07-ko-bbbbbb", date="2026-09-07",
                  generatedAt="2026-09-07T07:00:00+09:00")
    store.save_record(old)
    store.save_record(new)

    records = store.list_records()
    assert [r.id for r in records] == [new.id, old.id]
    assert len(store.list_records(limit=1)) == 1


def test_list_records_sorts_by_parsed_datetime_with_mixed_offsets(fixture_json):
    """오프셋이 섞이면 문자열 비교로는 순서가 뒤집힌다. 반드시 파싱된 시각으로 정렬한다."""
    # "2026-09-07T00:00:00-05:00" = 09-07 05:00 UTC  (실제로 더 나중)
    # "2026-09-07T07:00:00+09:00" = 09-06 22:00 UTC
    # 문자열 비교로는 "07:00:00+09:00" > "00:00:00-05:00" 이라 순서가 반대가 된다.
    later = _record(fixture_json, id="2026-09-07-ko-aaaaaa", date="2026-09-07",
                    generatedAt="2026-09-07T00:00:00-05:00")
    earlier = _record(fixture_json, id="2026-09-07-ko-bbbbbb", date="2026-09-07",
                      generatedAt="2026-09-07T07:00:00+09:00")
    store.save_record(later)
    store.save_record(earlier)

    assert [r.id for r in store.list_records()] == [later.id, earlier.id]
    assert store.latest_record("ko").id == later.id


def test_list_records_puts_unparsable_timestamps_last(fixture_json):
    """손상된 generatedAt이 최신 자리를 차지하지 못하게 한다."""
    good = _record(fixture_json, id="2026-09-07-ko-cccccc", date="2026-09-07",
                   generatedAt="2026-09-07T07:00:00+09:00")
    broken = _record(fixture_json, id="2026-09-07-ko-dddddd", date="2026-09-07",
                     generatedAt="쓰레기값")
    store.save_record(good)
    store.save_record(broken)

    assert [r.id for r in store.list_records()] == [good.id, broken.id]


def test_list_records_treats_naive_timestamp_as_local_zone(fixture_json):
    naive = _record(fixture_json, id="2026-09-07-ko-eeeeee", date="2026-09-07",
                    generatedAt="2026-09-07T09:00:00")          # KST 09:00 로 간주
    aware = _record(fixture_json, id="2026-09-07-ko-ffffff", date="2026-09-07",
                    generatedAt="2026-09-07T07:00:00+09:00")    # KST 07:00
    store.save_record(naive)
    store.save_record(aware)

    assert [r.id for r in store.list_records()] == [naive.id, aware.id]


def test_list_records_filters_by_lang(fixture_json):
    ko = _record(fixture_json, id="2026-09-07-ko-aaaaaa", lang="ko")
    en = _record(fixture_json, id="2026-09-07-en-bbbbbb", lang="en")
    store.save_record(ko)
    store.save_record(en)

    assert [r.id for r in store.list_records(lang="en")] == [en.id]
    assert store.latest_record("en").id == en.id
    assert store.latest_record("zh") is None


def test_list_records_skips_invalid_files(fixture_json, caplog):
    store.save_record(_record(fixture_json, id="2026-09-07-ko-aaaaaa"))
    (paths.BRIEFINGS_DIR / "broken.json").write_text('{"id": 1}', encoding="utf-8")
    (paths.BRIEFINGS_DIR / "garbage.json").write_text("not json", encoding="utf-8")

    records = store.list_records()
    assert len(records) == 1
    assert records[0].id == "2026-09-07-ko-aaaaaa"


def test_list_records_empty_dir_is_safe():
    assert store.list_records() == []


# ===== 원자적 쓰기 =====
def test_atomic_write_leaves_no_temp_files(fixture_json):
    store.save_record(_record(fixture_json))
    leftovers = [p.name for p in paths.BRIEFINGS_DIR.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_atomic_write_replaces_existing_content(tmp_path):
    target = tmp_path / "a.json"
    store._atomic_write_text(target, '{"v": 1}')
    store._atomic_write_text(target, '{"v": 2}')
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 2}
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_creates_parent_dirs(tmp_path):
    target = tmp_path / "deep" / "nested" / "a.json"
    store._atomic_write_text(target, "{}")
    assert target.is_file()


def test_oversized_file_is_skipped(fixture_json, monkeypatch):
    record = _record(fixture_json)
    store.save_record(record)
    monkeypatch.setattr(store, "_MAX_RECORD_BYTES", 10)
    assert store.load_record(record.id) is None


# ===== 시세 / 차트 / 스코어카드 / 포트폴리오 =====
def test_market_roundtrip(fixture_json):
    bundle = MarketBundle.model_validate(fixture_json("briefing_sample.json")["market"])
    store.save_market(bundle)
    loaded = store.load_market()
    assert loaded is not None
    assert loaded.updatedAt == bundle.updatedAt
    assert loaded.chartKeys == bundle.chartKeys


def test_load_market_missing_returns_none():
    assert store.load_market() is None


def test_chart_roundtrip(fixture_json):
    candles = fixture_json("chart_spx_1d.json")
    store.save_chart("sp500_1d", candles)
    charts = store.load_charts()
    assert set(charts) == {"sp500_1d"}
    assert charts["sp500_1d"] == candles


def test_load_charts_skips_bad_keys(fixture_json):
    store.save_chart("sp500_1d", fixture_json("chart_spx_1d.json"))
    (paths.CHARTS_DIR / "EVIL_1y.json").write_text("[]", encoding="utf-8")
    assert set(store.load_charts()) == {"sp500_1d"}


def test_scorecard_roundtrip():
    sc = Scorecard(updatedAt="2026-09-07T07:00:00+09:00", sampleWindow=30)
    store.save_scorecard(sc)
    loaded = store.load_scorecard()
    assert loaded is not None and loaded.sampleWindow == 30


def test_portfolio_load_and_format():
    assert store.load_portfolio() is None
    assert store.format_portfolio_for_llm(None) == ""

    portfolio = Portfolio(
        allocations=[{"name": "주식", "percentage": 60, "details": "미국 대형주"}],
        watchlist=["NVDA"],
    )
    paths.PORTFOLIO_JSON.write_text(portfolio.model_dump_json(indent=2), encoding="utf-8")
    loaded = store.load_portfolio()
    assert loaded is not None and loaded.allocations[0].percentage == 60

    text = store.format_portfolio_for_llm(loaded)
    assert "주식: 60%" in text
    assert "Watchlist: NVDA" in text


def test_portfolio_invalid_json_returns_none():
    paths.PORTFOLIO_JSON.write_text("{ not json", encoding="utf-8")
    assert store.load_portfolio() is None


# ===== v1 마이그레이션 =====
_V1_RECORD = {
    "id": "a1b2c3d4",
    "date": "2026-08-10",
    "generatedAt": "2026-08-10T07:02:00+09:00",
    "content": "## PART 1\n어젯밤 파월 의장이...",
    "model": "gemini-2.5-flash",
    "newsCount": 42,
    "generationTimeMs": 31000,
    "preview": "어젯밤 파월 의장이 금리 인하 신호를 보냈다",
}


def test_legacy_import_converts_and_backs_up(fixture_json):
    legacy_path = paths.BRIEFINGS_DIR / "2026-08-10_a1b2c3d4.json"
    legacy_path.write_text(json.dumps(_V1_RECORD, ensure_ascii=False), encoding="utf-8")
    store.save_record(_record(fixture_json, id="2026-09-07-ko-cccccc"))  # v2는 건드리지 않아야 한다

    converted = store.legacy_import()
    assert len(converted) == 1
    record = converted[0]
    assert record.provider == "gemini"
    assert record.lang == "ko"
    assert record.date == "2026-08-10"
    assert record.structured is None
    assert record.contentMarkdown == _V1_RECORD["content"]
    assert record.headline == _V1_RECORD["preview"]
    assert store.RECORD_ID_RE.match(record.id)

    assert not legacy_path.exists()
    assert (paths.BRIEFINGS_DIR / (legacy_path.name + store.LEGACY_SUFFIX)).is_file()
    assert store.load_record("2026-09-07-ko-cccccc") is not None


def test_legacy_import_is_idempotent():
    (paths.BRIEFINGS_DIR / "old.json").write_text(json.dumps(_V1_RECORD), encoding="utf-8")
    assert len(store.legacy_import()) == 1
    assert store.legacy_import() == []


@pytest.mark.parametrize("model,provider", [
    ("gemini-2.5-flash", "gemini"),
    ("gpt-4o", "openai"),
    ("claude-sonnet-4-5", "claude_cli"),
    ("mystery-model", "legacy"),
    ("", "legacy"),
])
def test_legacy_provider_mapping(model, provider):
    assert store._legacy_provider(model) == provider


def test_legacy_import_skips_bad_date():
    bad = dict(_V1_RECORD, date="not-a-date")
    (paths.BRIEFINGS_DIR / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    assert store.legacy_import() == []
    assert (paths.BRIEFINGS_DIR / "bad.json").is_file()  # 원본 보존


def test_legacy_import_no_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "BRIEFINGS_DIR", tmp_path / "missing")
    assert store.legacy_import() == []


def test_record_id_regex_shape():
    assert re.fullmatch(r"\^\[0-9\]\{4\}.*\$", store.RECORD_ID_RE.pattern)


def test_structured_stays_valid_after_roundtrip(fixture_json):
    record = _record(fixture_json)
    store.save_record(record)
    loaded = store.load_record(record.id)
    assert isinstance(loaded.structured, StructuredBriefing)
    assert len(loaded.structured.sectorLeaders) == 3
    assert loaded.topNews[0].time.endswith("UTC")


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Path Traversal: 레코드 ID·차트 키의 상위 경로/구분자/대문자 입력이 모두 거부되는지 검증
#   - Race Condition (TOCTOU): 원자적 쓰기 후 임시 파일이 남지 않고 내용이 교체되는지 검증
#   - Improper Random Value: 레코드 ID가 50회 생성에서 충돌하지 않는지 검증 (CSPRNG 사용 확인)
#   - Unsafe Deserialization: 손상 JSON/스키마 불일치 파일이 건너뛰어지는지 검증
#   - 자원 고갈 방지: 파일 크기 상한 초과 시 로드가 거부되는지 검증
# Not Applied:
#   - [WARN] 파일 권한(ACL)은 검증하지 않는다. Windows에서 os.chmod 의미가 제한적이다.
# --------------------------------------------------
