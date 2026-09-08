"""장중 시세 갱신(tick) 테스트 — 네트워크·배포 없이 분기만 검증한다.

회귀 배경: 이 사이트는 정적 파일이라 차트가 '파이프라인이 돌 때의 스냅샷'이다.
한국 낮에 열려 있는 코스피는 07시 브리핑 이후 몇 시간씩 낡는다. tick이 그 간극을 메우되,
값이 그대로면 배포하지 않아야 한다 (Cloudflare 무료 배포 한도 보호).
"""
from __future__ import annotations

import asyncio
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from mbrief import pipeline
from mbrief.schemas import MarketBundle, Quote, SectorQuote

KST = ZoneInfo("Asia/Seoul")


def _bundle(price: float = 100.0, updated: str = "2026-09-08T13:00:00+09:00") -> MarketBundle:
    return MarketBundle(
        updatedAt=updated,
        quotes={"KOSPI": Quote(price=price, change=1.0, changePercent=1.0)},
        sectors={"Technology": SectorQuote(symbol="XLK", price=200.0, changePercent=0.5)},
    )


def _charts(last_time: int = 1788528600) -> dict[str, list[dict]]:
    return {"kospi_1d": [{"time": last_time, "open": 1, "high": 1, "low": 1, "close": 1}]}


# ===== 지문 =====
def test_fingerprint_ignores_updated_at():
    """updatedAt은 매번 바뀐다. 그것만 다른 스냅샷은 '변경 없음'이어야 한다."""
    a = pipeline.market_fingerprint(_bundle(updated="2026-09-08T13:00:00+09:00"), _charts())
    b = pipeline.market_fingerprint(_bundle(updated="2026-09-08T13:30:00+09:00"), _charts())
    assert a == b


def test_fingerprint_changes_when_price_moves():
    a = pipeline.market_fingerprint(_bundle(price=100.0), _charts())
    b = pipeline.market_fingerprint(_bundle(price=100.5), _charts())
    assert a != b


def test_fingerprint_changes_when_a_new_candle_arrives():
    a = pipeline.market_fingerprint(_bundle(), _charts(last_time=1788528600))
    b = pipeline.market_fingerprint(_bundle(), _charts(last_time=1788528900))
    assert a != b


# ===== 시간대 게이트 =====
def test_tick_skips_outside_market_hours(monkeypatch):
    called: list[str] = []

    async def fake_refresh(**kwargs):
        called.append("refresh")
        return _bundle()

    monkeypatch.setattr(pipeline, "refresh", fake_refresh)
    monkeypatch.setattr(pipeline.marketclock, "in_intraday_window", lambda **kw: False)

    assert asyncio.run(pipeline.tick()) == pipeline.EXIT_SKIPPED
    assert called == []          # 네트워크를 아예 건드리지 않는다


def test_tick_force_runs_outside_market_hours(monkeypatch):
    monkeypatch.setattr(pipeline.marketclock, "in_intraday_window", lambda **kw: False)
    monkeypatch.setattr(pipeline, "_current_fingerprint", lambda: "same")
    monkeypatch.setattr(pipeline, "market_fingerprint", lambda b, c: "same")

    async def fake_refresh(**kwargs):
        return _bundle()

    monkeypatch.setattr(pipeline, "refresh", fake_refresh)
    monkeypatch.setattr(pipeline.store, "load_charts", lambda: _charts())
    assert asyncio.run(pipeline.tick(force=True)) == pipeline.EXIT_OK


# ===== 변경 없으면 배포하지 않는다 =====
def test_tick_skips_build_and_publish_when_nothing_changed(monkeypatch):
    built: list[str] = []
    monkeypatch.setattr(pipeline.marketclock, "in_intraday_window", lambda **kw: True)
    monkeypatch.setattr(pipeline, "_current_fingerprint", lambda: "same")
    monkeypatch.setattr(pipeline, "market_fingerprint", lambda b, c: "same")
    monkeypatch.setattr(pipeline, "build_site", lambda: built.append("build") or True)
    monkeypatch.setattr(pipeline.store, "load_charts", lambda: _charts())

    async def fake_refresh(**kwargs):
        return _bundle()

    monkeypatch.setattr(pipeline, "refresh", fake_refresh)

    assert asyncio.run(pipeline.tick()) == pipeline.EXIT_OK
    assert built == []           # 빌드조차 하지 않는다


def test_tick_builds_when_data_changed(monkeypatch):
    built: list[str] = []
    monkeypatch.setattr(pipeline.marketclock, "in_intraday_window", lambda **kw: True)
    monkeypatch.setattr(pipeline, "_current_fingerprint", lambda: "old")
    monkeypatch.setattr(pipeline, "market_fingerprint", lambda b, c: "new")
    monkeypatch.setattr(pipeline, "build_site", lambda: built.append("build") or True)
    monkeypatch.setattr(pipeline.store, "load_charts", lambda: _charts())

    async def fake_refresh(**kwargs):
        return _bundle()

    monkeypatch.setattr(pipeline, "refresh", fake_refresh)

    assert asyncio.run(pipeline.tick(no_publish=True)) == pipeline.EXIT_OK
    assert built == ["build"]


def test_tick_reports_failure_when_refresh_raises(monkeypatch):
    monkeypatch.setattr(pipeline.marketclock, "in_intraday_window", lambda **kw: True)
    monkeypatch.setattr(pipeline, "_current_fingerprint", lambda: None)

    async def boom(**kwargs):
        raise RuntimeError("네트워크 실패")

    monkeypatch.setattr(pipeline, "refresh", boom)
    assert asyncio.run(pipeline.tick()) == pipeline.EXIT_FAILED
