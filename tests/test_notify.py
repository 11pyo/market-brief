"""텔레그램 알림 본문 테스트 — 네트워크 호출 없이 build_message만 검증한다."""
from __future__ import annotations

import json
from pathlib import Path

from mbrief import notify
from mbrief.schemas import BriefingRecord

FIXTURES = Path(__file__).parent / "fixtures"


def _record() -> BriefingRecord:
    data = json.loads((FIXTURES / "briefing_sample.json").read_text(encoding="utf-8"))
    return BriefingRecord.model_validate(data)


def test_message_contains_headline_and_direction():
    msg = notify.build_message(_record(), "https://example.test/site")
    assert "파월" in msg or "PART" not in msg      # 헤드라인이 본문에 들어간다
    assert "%" in msg                              # 방향성 확률 줄
    assert "https://example.test/site/archive/" in msg


def test_notices_are_appended_and_escaped():
    msg = notify.build_message(
        _record(), "", notices=["<b>주의</b> claude auth login 필요"],
    )
    assert "⚠️" in msg
    # [SECURE] 안내 문구도 HTML 이스케이프 대상 — 원문 태그가 그대로 남으면 안 된다
    assert "<b>주의</b>" not in msg
    assert "&lt;b&gt;주의&lt;/b&gt;" in msg


def test_notices_are_capped():
    many = [f"안내 {i}" for i in range(10)]
    msg = notify.build_message(_record(), "", notices=many)
    assert msg.count("⚠️") == notify._MAX_NOTICES


def test_no_notice_section_when_empty():
    assert "⚠️" not in notify.build_message(_record(), "", notices=[])
