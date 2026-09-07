"""텔레그램 알림 (선택).

TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 없으면 조용히 건너뛴다.
HTML parse mode를 쓰므로 모든 동적 텍스트는 html.escape로 인코딩한다.
"""
from __future__ import annotations

import html
import logging
from collections.abc import Sequence

import httpx

from mbrief.config import settings
from mbrief.llm.prompts import labels
from mbrief.schemas import BriefingRecord

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
_TIMEOUT_SEC = 15
_MAX_MESSAGE_CHARS = 3800  # 텔레그램 상한 4096자 — 여유를 둔다
_TOP_RISKS = 3
_MAX_NOTICES = 3

_DIRECTION_ICONS = {"up": "📈", "flat": "➡️", "down": "📉"}


def _esc(text: object) -> str:
    """[SECURE] HTML 출력 인코딩 - XSS/마크업 깨짐 방지 (Category 1)."""
    return html.escape(str(text or ""), quote=False)


def archive_url(site_url: str, record_id: str) -> str:
    """아카이브 페이지 URL. site_url이 없으면 빈 문자열."""
    base = (site_url or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/archive/{record_id}.html"


def build_message(record: BriefingRecord, site_url: str,
                  notices: "Sequence[str] | None" = None) -> str:
    """알림 본문 (HTML parse mode). notices는 맨 아래에 경고로 덧붙인다."""
    lb = labels(record.lang)
    lines: list[str] = [
        f"<b>{_esc(settings.site_name)}</b> · {_esc(record.date)}",
        "",
        f"<b>{_esc(record.headline)}</b>",
    ]

    if record.structured is not None:
        d = record.structured.direction
        lines.append("")
        lines.append(
            f"{_DIRECTION_ICONS['up']} {_esc(lb['up'])} {d.up}%  "
            f"{_DIRECTION_ICONS['flat']} {_esc(lb['flat'])} {d.flat}%  "
            f"{_DIRECTION_ICONS['down']} {_esc(lb['down'])} {d.down}%"
        )
        risks = record.structured.risks[:_TOP_RISKS]
        if risks:
            lines.append("")
            lines.append("<b>Risks</b>")
            for risk in risks:
                lines.append(f"• {_esc(risk.title)} ({_esc(lb.get(risk.severity, risk.severity))})")

    link = archive_url(site_url, record.id)
    if link:
        lines.append("")
        lines.append(f'<a href="{_esc(link)}">{_esc(link)}</a>')

    # 운영 안내 (예: 로컬 Claude CLI 미로그인으로 API 폴백 사용)
    for note in list(notices or [])[:_MAX_NOTICES]:
        lines.append("")
        lines.append(f"⚠️ {_esc(note)}")

    message = "\n".join(lines)
    if len(message) > _MAX_MESSAGE_CHARS:
        message = message[:_MAX_MESSAGE_CHARS] + "…"
    return message


async def send_telegram(record: BriefingRecord, site_url: str = "",
                        notices: "Sequence[str] | None" = None) -> bool:
    """브리핑 알림 전송. 토큰/챗ID가 없으면 info 로그 후 False."""
    token = settings.telegram_bot_token.strip()
    chat_id = settings.telegram_chat_id.strip()
    if not token or not chat_id:
        logger.info("[Notify] 텔레그램 미설정 - 알림 건너뜀")
        return False

    payload = {
        "chat_id": chat_id,
        "text": build_message(record, site_url or settings.site_url, notices),
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        # [SECURE] 토큰은 URL 경로에만 사용하고 로그에는 남기지 않는다 (Category 2)
        # [SECURE] 모든 외부 호출에 타임아웃 지정 (Category 3)
        async with httpx.AsyncClient(timeout=_TIMEOUT_SEC) as client:
            resp = await client.post(f"{TELEGRAM_API}/bot{token}/sendMessage", json=payload)
            if resp.status_code != 200:
                # [SECURE] 응답 본문에 토큰이 섞일 수 있으므로 상태 코드만 기록 (Category 4)
                logger.warning("[Notify] 텔레그램 전송 실패: HTTP %d", resp.status_code)
                return False
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("[Notify] 텔레그램 전송 오류: %s", type(e).__name__)
        return False

    logger.info("[Notify] 텔레그램 전송 완료: %s", record.id)
    return True


# --------------------------------------------------
# Security Checklist
# Applied:
#   - XSS / 마크업 인젝션: 모든 동적 텍스트를 html.escape로 인코딩 후 HTML parse mode 전송
#   - Hard-coded Credentials: 봇 토큰·챗 ID는 settings(환경변수) 경유, 소스 상수 없음
#   - Unencrypted Sensitive Data: 토큰을 로그·예외 메시지에 기록하지 않음 (상태 코드/예외 타입만)
#   - Infinite Loop: HTTP 타임아웃 15초 고정
#   - Improper Resource Release: httpx.AsyncClient를 async context manager로 사용
#   - Improper Exception Handling: 빈 except 없음, 실패 시 경고 로깅 후 False 반환
#   - 자원 고갈 방지: 메시지 길이 상한(3800자) 적용
# Not Applied:
#   - [WARN] Improper Certificate Validation: httpx 기본 TLS 검증에 의존 (핀닝 없음).
#   - [WARN] 텔레그램 봇 토큰은 URL 경로에 포함된다(API 규격). 프록시 로그에 남을 수 있으므로
#     신뢰할 수 없는 네트워크에서는 사용을 피할 것.
# --------------------------------------------------
