"""파이프라인 mock/offline 테스트 — 네트워크도 LLM 호출도 하지 않는다.

pipeline.refresh를 픽스처 기반 번들로 monkeypatch해 시세 수집 경로를 완전히 차단한다.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from mbrief import paths, pipeline, store
from mbrief.config import settings
from mbrief.schemas import BriefingRecord, MarketBundle
from mbrief.sources import market_data

# no_network 픽스처가 교체하기 전의 원본 refresh (offline 경로 자체를 테스트할 때 되돌린다).
# monkeypatch.undo()를 쓰면 conftest의 데이터 경로 격리까지 함께 풀려 실제 data/에 쓰게 되므로
# 반드시 이 참조로 개별 복원한다.
_REAL_REFRESH = pipeline.refresh


@pytest.fixture
def fixture_bundle(fixture_json) -> MarketBundle:
    return market_data.to_bundle(
        fixture_json("snapshot.json"),
        fixture_json("sectors.json"),
        fixture_json("fear_greed.json"),
        fixture_json("calendar.json"),
        chart_keys=["sp500_1d"],
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch, fixture_bundle: MarketBundle):
    """refresh를 픽스처 번들로 대체 — yfinance/httpx가 호출되지 않는다."""
    calls: list[dict] = []

    async def fake_refresh(**kwargs) -> MarketBundle:
        calls.append(kwargs)
        return fixture_bundle.model_copy(deep=True)

    monkeypatch.setattr(pipeline, "refresh", fake_refresh)
    return calls


def _generate(**kwargs) -> list[BriefingRecord]:
    return asyncio.run(pipeline.generate(mock=True, offline=True, **kwargs))


# ===== generate(mock, offline) =====
def test_generate_writes_structured_record():
    records = _generate()
    assert len(records) == 1

    record = records[0]
    assert record.provider == "mock"
    assert record.model == "mock-fixture"
    assert record.lang == "ko"
    assert record.structured is not None
    assert record.headline == record.structured.headline
    assert len(record.structured.sectorLeaders) == 3
    assert len(record.structured.sectorLaggards) == 2

    path = paths.BRIEFINGS_DIR / f"{record.id}.json"
    assert path.is_file()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["id"] == record.id
    assert saved["structured"]["headline"] == record.structured.headline


def test_generate_uses_refresh(no_network):
    _generate()
    assert no_network == [{"offline": True}]


def test_generate_record_carries_market_from_fixtures(fixture_json):
    record = _generate()[0]
    assert record.market is not None
    snapshot = fixture_json("snapshot.json")
    assert record.market.quotes["S&P 500"].price == snapshot["S&P 500"]["price"]
    assert record.market.quotes["KOSPI"].changePercent == snapshot["KOSPI"]["changePercent"]
    assert record.market.fearGreed is not None
    assert record.market.fearGreed.score == fixture_json("fear_greed.json")["score"]
    assert len(record.market.sectors) == len(fixture_json("sectors.json"))
    assert record.market.calendar["fomc"]


def test_generate_content_markdown_is_rendered():
    record = _generate()[0]
    assert record.contentMarkdown
    for index in range(1, 8):
        assert f"## PART {index} — " in record.contentMarkdown
    assert "📈 상승:" in record.contentMarkdown
    assert "🎯 " in record.contentMarkdown


def test_generate_fills_news_metadata():
    record = _generate()[0]
    assert record.newsCount > 0
    assert sum(record.tierCounts.values()) == record.newsCount
    assert set(record.tierCounts) == {"t1", "t2", "t3"}
    assert 0 < len(record.topNews) <= 12
    assert record.topNews[0].time.endswith("UTC")


def test_generate_id_and_dates_are_consistent():
    record = _generate()[0]
    assert store.RECORD_ID_RE.match(record.id)
    assert record.id.startswith(f"{record.date}-ko-")
    assert record.generatedAt.startswith(record.date)


def test_generate_is_reloadable_from_store():
    record = _generate()[0]
    loaded = store.load_record(record.id)
    assert loaded is not None
    assert loaded.model_dump() == record.model_dump()
    assert store.latest_record("ko").id == record.id


# ===== 다국어 =====
def test_generate_multi_lang_copies_in_mock_mode():
    records = _generate(langs=["ko", "en"])
    assert [r.lang for r in records] == ["ko", "en"]

    primary, translated = records
    assert translated.translatedFrom == primary.id
    assert primary.translatedFrom is None
    assert translated.id.startswith(f"{translated.date}-en-")
    # mock 모드에서는 번역하지 않고 본문을 복제한다
    assert translated.structured.headline == primary.structured.headline
    # 마크다운 헤더는 대상 언어로 렌더된다
    assert "## PART 1 — Previous Session Recap" in translated.contentMarkdown
    assert store.load_record(translated.id) is not None


def test_generate_ignores_unsupported_lang():
    records = _generate(langs=["ko", "xx", "zz"])
    assert [r.lang for r in records] == ["ko"]


def test_generate_falls_back_to_default_lang_when_all_invalid():
    records = _generate(langs=["xx"])
    assert records[0].lang == "ko"


# ===== 픽스처 로더 =====
def test_load_fixture_rejects_traversal():
    for bad in ("../secret.json", "sub/dir.json", "..\\evil.json"):
        with pytest.raises(ValueError):
            pipeline.load_fixture(bad)


def test_load_fixture_missing_raises():
    with pytest.raises(FileNotFoundError):
        pipeline.load_fixture("does_not_exist.json")


# ===== refresh(offline) 자체 =====
def test_offline_refresh_saves_market_and_charts(monkeypatch, fixture_json):
    # refresh만 원본으로 되돌린다 (경로 격리는 유지해야 실제 data/를 건드리지 않는다)
    monkeypatch.setattr(pipeline, "refresh", _REAL_REFRESH)
    assert paths.MARKET_JSON.parent != (paths.ROOT / "data" / "cache")
    bundle = asyncio.run(pipeline.refresh(offline=True))

    assert bundle.chartKeys == ["sp500_1d"]
    assert paths.MARKET_JSON.is_file()
    assert (paths.CHARTS_DIR / "sp500_1d.json").is_file()

    loaded = store.load_market()
    assert loaded is not None
    assert loaded.quotes["S&P 500"].price == fixture_json("snapshot.json")["S&P 500"]["price"]
    assert store.load_charts()["sp500_1d"] == fixture_json("chart_spx_1d.json")


# ===== daily 요일 게이트 / lock =====
def test_daily_skips_on_inactive_weekday(monkeypatch):
    monkeypatch.setattr(pipeline, "_is_active_today", lambda: False)
    assert asyncio.run(pipeline.daily(no_publish=True, mock=True, offline=True)) == pipeline.EXIT_SKIPPED


def test_daily_lock_blocks_second_run():
    with pipeline._DailyLock(paths.LOCK_FILE) as first:
        assert first.acquired
        with pipeline._DailyLock(paths.LOCK_FILE) as second:
            assert not second.acquired
    assert not paths.LOCK_FILE.exists()


def test_daily_lock_clears_stale_lock():
    paths.LOCK_FILE.write_text("99999 stale", encoding="utf-8")
    with pipeline._DailyLock(paths.LOCK_FILE, stale_sec=-1) as lock:
        assert lock.acquired
    assert not paths.LOCK_FILE.exists()


def test_daily_lock_clears_lock_of_dead_process(monkeypatch):
    """PID가 죽어 있으면 2시간을 기다리지 않고 즉시 lock을 해제한다."""
    monkeypatch.setattr(pipeline, "_pid_alive", lambda pid: False)
    paths.LOCK_FILE.write_text("4242 2026-09-07T07:00:00+09:00\n", encoding="utf-8")
    with pipeline._DailyLock(paths.LOCK_FILE, stale_sec=pipeline.LOCK_STALE_SEC) as lock:
        assert lock.acquired
    assert not paths.LOCK_FILE.exists()


def test_daily_lock_keeps_lock_of_live_process(monkeypatch):
    monkeypatch.setattr(pipeline, "_pid_alive", lambda pid: True)
    paths.LOCK_FILE.write_text("4242 2026-09-07T07:00:00+09:00\n", encoding="utf-8")
    with pipeline._DailyLock(paths.LOCK_FILE, stale_sec=pipeline.LOCK_STALE_SEC) as lock:
        assert not lock.acquired
    assert paths.LOCK_FILE.exists()  # 남의 lock을 지우지 않는다


def test_daily_lock_falls_back_to_age_when_pid_unknown(monkeypatch):
    monkeypatch.setattr(pipeline, "_pid_alive", lambda pid: None)
    paths.LOCK_FILE.write_text("garbage-not-a-pid\n", encoding="utf-8")
    with pipeline._DailyLock(paths.LOCK_FILE, stale_sec=-1) as lock:
        assert lock.acquired  # 시간 기준으로 stale 판정


def test_pid_alive_reports_current_process():
    assert pipeline._pid_alive(os.getpid()) is True


def test_pid_alive_rejects_invalid_pid():
    assert pipeline._pid_alive(0) is None
    assert pipeline._pid_alive(-1) is None


# ===== daily 멱등성 =====
@pytest.fixture
def daily_env(monkeypatch):
    """daily의 부수효과(빌드/배포/알림)를 차단하고 호출 흔적만 수집한다."""
    calls = {"build": 0, "notify": 0, "score": 0}

    async def fake_notify(record, site_url, notices=None):
        calls["notify"] += 1
        return True

    monkeypatch.setattr(pipeline, "build_site", lambda: (calls.__setitem__("build", calls["build"] + 1), True)[1])
    monkeypatch.setattr(pipeline.scorecard, "update_scorecard",
                        lambda *a, **kw: calls.__setitem__("score", calls["score"] + 1))
    # sys.modules만 갈아끼우면 `from mbrief import notify`가 패키지 속성에 이미 캐시된
    # 실제 모듈을 집어와 대체가 무력화된다(다른 테스트가 먼저 임포트한 경우). 실제 모듈의
    # 함수 자체를 교체해 임포트 순서와 무관하게 동작하도록 한다.
    from mbrief import notify as real_notify
    monkeypatch.setattr(real_notify, "send_telegram", fake_notify)
    return calls


def _run_daily(**kwargs) -> int:
    return asyncio.run(pipeline.daily(no_publish=True, mock=True, offline=True, **kwargs))


def test_daily_generates_once_then_skips_regeneration(daily_env):
    assert _run_daily() == pipeline.EXIT_OK
    first = store.list_records(lang=settings.primary_lang)
    assert len(first) == 1

    # 배포 실패 후 재실행 시나리오: 같은 날 두 번째 실행은 새 브리핑을 만들지 않는다
    assert _run_daily() == pipeline.EXIT_OK
    second = store.list_records(lang=settings.primary_lang)
    assert [r.id for r in second] == [r.id for r in first]

    # 생성은 건너뛰어도 refresh/score/build/notify는 다시 돈다
    assert daily_env["build"] == 2
    assert daily_env["score"] == 2
    assert daily_env["notify"] == 2


def test_daily_force_regenerates(daily_env):
    assert _run_daily() == pipeline.EXIT_OK
    first = store.list_records(lang=settings.primary_lang)

    assert _run_daily(force=True) == pipeline.EXIT_OK
    second = store.list_records(lang=settings.primary_lang)
    assert len(second) == 2
    assert first[0].id in {r.id for r in second}


def test_generate_skips_when_todays_record_exists():
    first = _generate()
    again = _generate()
    assert [r.id for r in again] == [r.id for r in first]
    assert len(store.list_records()) == 1


def test_generate_force_creates_second_record():
    first = _generate()
    again = _generate(force=True)
    assert again[0].id != first[0].id
    assert len(store.list_records()) == 2


def test_todays_records_ignores_other_days(fixture_json):
    assert pipeline.todays_records("ko") == []
    record = _generate()[0]
    assert [r.id for r in pipeline.todays_records("ko")] == [record.id]
    assert pipeline.todays_records("en") == []


def test_daily_does_not_count_publish_as_failed_when_build_fails(monkeypatch, daily_env):
    """빌드가 실패하면 publish는 '시도하지 않은' 것이지 실패가 아니다."""
    monkeypatch.setattr(pipeline, "build_site", lambda: False)
    monkeypatch.setattr(pipeline.settings, "publish_enabled", True, raising=False)

    messages: list[str] = []
    monkeypatch.setattr(pipeline.logger, "warning",
                        lambda msg, *args: messages.append(str(msg) % args if args else str(msg)))

    assert asyncio.run(pipeline.daily(mock=True, offline=True)) == pipeline.EXIT_FAILED
    failure_lines = [m for m in messages if "후속 단계 실패" in m]
    assert failure_lines, messages
    assert "publish" not in failure_lines[-1]
    assert "build" in failure_lines[-1]


def test_build_site_reports_missing_builder(monkeypatch):
    """mbrief.site.builder가 없으면 예외 대신 False + 명확한 로그."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("mbrief.site"):
            raise ImportError("no module named mbrief.site.builder")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert pipeline.build_site() is False


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Path Traversal: 픽스처 로더가 경로 구분자·상위 경로를 거부하는지 검증
#   - Race Condition (TOCTOU): daily lock의 배타적 생성과 stale lock 정리 동작 검증
#   - Improper Input for Security Decision: 지원하지 않는 언어 코드가 무시되는지 검증
#   - Missing Error Handling: 사이트 빌더 부재 시 예외 전파 없이 False를 반환하는지 검증
#   - Unsafe Deserialization: 저장된 레코드가 pydantic 왕복 후에도 동일한지 검증
# Not Applied:
#   - [WARN] 실제 LLM 호출 경로(engine.generate_briefing)는 테스트하지 않는다.
#     구독 쿼터를 소모하고 인증 상태에 의존하므로 mock 경로만 검증한다.
# --------------------------------------------------


# ===== 사용자 안내 큐 =====
def test_notice_queue_dedupes_and_drains():
    pipeline.take_notices()          # 앞선 테스트 잔여물 제거
    pipeline.add_notice("로그인 필요")
    pipeline.add_notice("로그인 필요")   # 중복은 한 번만
    pipeline.add_notice("")            # 빈 문자열은 무시
    assert pipeline.peek_notices() == ["로그인 필요"]
    assert pipeline.take_notices() == ["로그인 필요"]
    assert pipeline.take_notices() == []   # take는 큐를 비운다
