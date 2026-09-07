"""시간 유틸 — 모든 타임스탬프는 설정 타임존(기본 KST) 기준 ISO 8601로 통일한다.

paths/config/schemas와 달리 계약이 아닌 내부 헬퍼이며, 순환 임포트를 피하려고
sources/llm/store 어디서나 의존 없이 쓸 수 있도록 최소 기능만 둔다.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from mbrief.config import settings

_FALLBACK_TZ = "Asia/Seoul"

#: 파싱 불가한 타임스탬프의 정렬용 하한값 (항상 가장 오래된 값으로 취급된다).
MIN_DATETIME = datetime.min.replace(tzinfo=timezone.utc)


def tzinfo() -> ZoneInfo:
    """설정 타임존. 잘못된 값이면 KST로 대체한다."""
    try:
        return ZoneInfo(settings.tz or _FALLBACK_TZ)
    except Exception as e:  # ZoneInfoNotFoundError 등
        # [SECURE] 잘못된 설정값을 그대로 신뢰하지 않고 안전한 기본값으로 대체 - Improper Input for Security Decision
        import logging

        logging.getLogger(__name__).warning("[TimeUtil] 타임존 설정 오류(%s) - %s 사용", e, _FALLBACK_TZ)
        return ZoneInfo(_FALLBACK_TZ)


def now_local() -> datetime:
    """설정 타임존의 현재 시각 (tz-aware)."""
    return datetime.now(tzinfo())


def now_iso() -> str:
    """설정 타임존 기준 ISO 8601 문자열 (오프셋 포함)."""
    return now_local().isoformat(timespec="seconds")


def today_str() -> str:
    """설정 타임존 기준 YYYY-MM-DD."""
    return now_local().strftime("%Y-%m-%d")


def today_date() -> date:
    """설정 타임존 기준 date."""
    return now_local().date()


def parse_iso(value: str | None) -> datetime:
    """ISO 8601 문자열 → tz-aware datetime.

    정렬 키 전용 헬퍼다. 오프셋이 섞인 문자열을 사전식으로 비교하면 순서가 뒤집히므로
    반드시 이 함수로 파싱한 값을 쓴다. naive 값은 설정 타임존(기본 KST)으로 간주하고,
    파싱할 수 없으면 MIN_DATETIME(UTC)을 돌려줘 항상 가장 오래된 항목으로 밀어낸다.
    """
    text = str(value or "").strip()
    if not text:
        return MIN_DATETIME
    try:
        # [SECURE] 외부 문자열을 형식 검증 없이 비교에 쓰지 않는다 - Improper Input for Security Decision
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return MIN_DATETIME
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tzinfo())
    return dt


def parse_iso_date(value: str | None) -> date | None:
    """ISO 8601 문자열 → 설정 타임존 기준 날짜. 파싱 실패 시 None."""
    dt = parse_iso(value)
    if dt == MIN_DATETIME:
        return None
    return dt.astimezone(tzinfo()).date()


def utc_str(ts: int | float | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Unix timestamp → UTC 문자열. 값이 없으면 'N/A'."""
    if not ts:
        return "N/A"
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(fmt)
    except (OverflowError, OSError, ValueError):
        # [SECURE] 손상된 외부 타임스탬프로 인한 예외를 삼키지 않고 안전한 표시값으로 대체 - Missing Error Handling
        return "N/A"


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Improper Input for Security Decision: 설정 타임존이 유효하지 않으면 하드코드된 안전 기본값으로 대체
#   - Missing Error Handling: 외부 타임스탬프 변환 실패를 개별 처리하여 상위 파이프라인 중단 방지
#   - Improper Exception Handling: 빈 except 없음, 실패 시 로깅 또는 안전 기본값 반환
#   - Improper Input for Security Decision: parse_iso는 ISO 8601 파싱에 성공한 값만 비교에 사용하고,
#     실패 시 MIN_DATETIME으로 대체해 손상된 타임스탬프가 정렬 순서를 조작하지 못하게 한다
# Not Applied:
#   - [WARN] 해당 없음 - 외부 입출력·자격증명·직렬화를 다루지 않는 순수 유틸리티 모듈이다.
# --------------------------------------------------
