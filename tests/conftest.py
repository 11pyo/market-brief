"""pytest 공통 픽스처.

모든 테스트는 mbrief.paths의 데이터 경로 상수를 tmp 디렉터리로 바꿔치기한 상태로 실행된다.
store/pipeline/scorecard는 호출 시점에 paths 모듈 속성을 조회하므로, 이 monkeypatch만으로
실제 data/ 디렉터리에 쓰지 않는다. ROOT는 픽스처(tests/fixtures) 조회에 필요하므로 그대로 둔다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mbrief import paths

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_data_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """data/ 경로 상수를 테스트별 tmp 디렉터리로 격리."""
    data = tmp_path / "data"
    briefings = data / "briefings"
    cache = data / "cache"
    charts = cache / "charts"
    for directory in (data, briefings, cache, charts):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(paths, "DATA_DIR", data, raising=True)
    monkeypatch.setattr(paths, "BRIEFINGS_DIR", briefings, raising=True)
    monkeypatch.setattr(paths, "CACHE_DIR", cache, raising=True)
    monkeypatch.setattr(paths, "CHARTS_DIR", charts, raising=True)
    monkeypatch.setattr(paths, "MARKET_JSON", cache / "market.json", raising=True)
    monkeypatch.setattr(paths, "SCORECARD_JSON", data / "scorecard.json", raising=True)
    monkeypatch.setattr(paths, "PORTFOLIO_JSON", data / "portfolio.json", raising=True)
    monkeypatch.setattr(paths, "LOCK_FILE", data / ".daily.lock", raising=True)
    monkeypatch.setattr(paths, "SITE_DIR", tmp_path / "site", raising=True)
    monkeypatch.setattr(paths, "LOG_DIR", tmp_path / "logs", raising=True)
    return tmp_path


@pytest.fixture
def fixture_json():
    """tests/fixtures/<name> JSON 로더."""
    def _load(name: str):
        with (FIXTURES / name).open("r", encoding="utf-8") as f:
            return json.load(f)
    return _load


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Race Condition (TOCTOU): pytest tmp_path로 테스트별 격리 디렉터리 사용
#   - Improper Resource Release: 픽스처 파일을 context manager로 읽어 핸들 해제
# Not Applied:
#   - [WARN] 해당 없음 - 테스트 전용 헬퍼, 외부 입출력·자격증명 없음
# --------------------------------------------------
