"""Unit tests for S+ Tier Architectural Enhancements:
1. Event-Driven Micro-Batch & Volatility Trigger
2. IndexNow Push & Temporal Fencing
3. Researcher View (All-Expand Mode) & Print Optimization
"""

from datetime import datetime, timezone
import pytest
from mbrief.schemas import BriefingRecord, MarketBundle, Quote, Scorecard
from mbrief.pipeline import check_volatility_trigger
from mbrief import indexing
from mbrief.site import builder
from tests.test_builder import sample_record, candles, scorecard, make_settings


def test_volatility_trigger_triggers_on_vix_spike():
    """VIX 변동폭 >= 10.0% 시 변동성 트리거가 발동해야 한다."""
    bundle = MarketBundle(
        updatedAt="2026-10-14T07:00:00Z",
        quotes={
            "VIX": Quote(price=18.5, change=2.1, changePercent=10.5),
            "S&P 500": Quote(price=5800.0, change=-10.0, changePercent=-0.2),
        },
    )
    triggered, reason = check_volatility_trigger(bundle)
    assert triggered is True
    assert "VIX" in reason
    assert "+10.5%" in reason


def test_volatility_trigger_triggers_on_sp500_plunge():
    """S&P 500 변동폭 >= 2.0% 시 변동성 트리거가 발동해야 한다."""
    bundle = MarketBundle(
        updatedAt="2026-10-14T07:00:00Z",
        quotes={
            "VIX": Quote(price=16.0, change=0.5, changePercent=3.2),
            "S&P 500": Quote(price=5700.0, change=-120.0, changePercent=-2.4),
        },
    )
    triggered, reason = check_volatility_trigger(bundle)
    assert triggered is True
    assert "S&P 500" in reason
    assert "-2.4%" in reason


def test_volatility_trigger_calm_market_returns_false():
    """일반적인 변동폭에서는 트리거가 발동하지 않아야 한다."""
    bundle = MarketBundle(
        updatedAt="2026-10-14T07:00:00Z",
        quotes={
            "VIX": Quote(price=15.0, change=0.3, changePercent=2.0),
            "S&P 500": Quote(price=5820.0, change=10.0, changePercent=0.2),
        },
    )
    triggered, reason = check_volatility_trigger(bundle)
    assert triggered is False
    assert reason == ""


def test_temporal_fencing_metadata_present_in_llms_txt(tmp_path, sample_record, candles, scorecard):
    """llms-full.txt 최상단에 TEMPORAL FENCING 헤더가 명시되어야 한다."""
    result = builder.build(
        [sample_record], sample_record.market, scorecard, {"sp500_1d": candles},
        out_dir=tmp_path / "site", settings=make_settings(),
    )
    llms_full = (result.out_dir / "llms-full.txt").read_text(encoding="utf-8")
    
    assert "[TEMPORAL FENCING & VALIDITY METADATA]" in llms_full
    assert "VALID_UNTIL" in llms_full
    assert "TEMPORAL_STATUS" in llms_full
    assert "ACTIVE_SESSION" in llms_full
    assert "Do NOT extrapolate or cite as current if system_time > VALID_UNTIL" in llms_full


def test_jsonld_contains_temporal_fencing(tmp_path, sample_record, candles, scorecard):
    """HTML 페이지의 JSON-LD에 expires 및 validThrough 메타데이터가 주입되어야 한다."""
    import json
    import re
    
    result = builder.build(
        [sample_record], sample_record.market, scorecard, {"sp500_1d": candles},
        out_dir=tmp_path / "site", settings=make_settings(),
    )
    index_html = (result.out_dir / "index.html").read_text(encoding="utf-8")
    
    # JSON-LD 추출
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', index_html, re.DOTALL)
    assert m is not None, "JSON-LD script tag not found"
    data = json.loads(m.group(1))
    
    article = next(item for item in data["@graph"] if "#article" in item.get("@id", ""))
    assert "expires" in article
    assert "validThrough" in article
    assert article["expires"] == article["validThrough"]
    
    dataset = next(item for item in data["@graph"] if item.get("@type") == "Dataset")
    assert "temporalCoverage" in dataset


def test_indexnow_key_file_exists(tmp_path, sample_record, candles, scorecard):
    """IndexNow 검증 키 파일이 사이트 루트에 정상 복사되어야 한다."""
    result = builder.build(
        [sample_record], sample_record.market, scorecard, {"sp500_1d": candles},
        out_dir=tmp_path / "site", settings=make_settings(),
    )
    key_file = result.out_dir / f"{indexing.INDEXNOW_KEY}.txt"
    assert key_file.exists()
    assert key_file.read_text(encoding="utf-8").strip() == indexing.INDEXNOW_KEY
