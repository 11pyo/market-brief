"""API 폴백 프로바이더 (텍스트 모드 전용).

claude_cli가 실패했을 때만 사용한다. 키가 설정된 프로바이더만 체인에 참여하며,
구조화 출력은 지원하지 않는다(마크다운 텍스트 → engine이 그대로 contentMarkdown에 저장).

각 함수 시그니처: async def call(system_prompt, user_prompt) -> tuple[content, model]
클라이언트는 최초 호출 시점에 지연 초기화한다 (미사용 SDK 임포트 비용 회피).
"""
from __future__ import annotations

import asyncio
import logging

from mbrief.config import settings

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 4000
TEMPERATURE = 0.7

_gemini_client = None
_openai_client = None
_anthropic_client = None


class ProviderError(RuntimeError):
    """API 프로바이더 호출 실패."""


def available_providers() -> list[str]:
    """설정에 나열되고 키가 있는 폴백 프로바이더 이름 목록 (설정 순서 유지)."""
    keys = {
        "gemini": settings.gemini_api_key,
        "openai": settings.openai_api_key,
        "anthropic": settings.anthropic_api_key,
    }
    result: list[str] = []
    for name in settings.fallback_providers:
        # [SECURE] 설정 문자열을 그대로 신뢰하지 않고 지원 목록과 대조 (Category 1)
        if name in keys and keys[name].strip() and name not in result:
            result.append(name)
        elif name not in keys:
            logger.warning("[LLM] 알 수 없는 폴백 프로바이더 무시: %r", str(name)[:30])
    return result


# ===== Gemini =====
def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        # [SECURE] API 키는 settings(환경변수)에서만 로드 - Hard-coded Credentials 방지 (Category 2)
        _gemini_client = genai.Client(api_key=settings.gemini_api_key)
    return _gemini_client


async def call_gemini(system_prompt: str, user_prompt: str) -> tuple[str, str]:
    from google.genai import types

    client = _get_gemini_client()
    model = settings.gemini_model
    logger.info("[LLM] Gemini %s 호출 중...", model)
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=TEMPERATURE,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        ),
    )
    content = (response.text or "").strip()
    if not content:
        raise ProviderError("Gemini 응답이 비어 있습니다.")
    return content, model


# ===== OpenAI =====
def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI
        _openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _openai_client


async def call_openai(system_prompt: str, user_prompt: str) -> tuple[str, str]:
    client = _get_openai_client()
    model = settings.openai_model
    logger.info("[LLM] OpenAI %s 호출 중...", model)
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=TEMPERATURE,
        max_tokens=MAX_OUTPUT_TOKENS,
    )
    content = (resp.choices[0].message.content or "").strip() if resp.choices else ""
    if not content:
        raise ProviderError("OpenAI 응답이 비어 있습니다.")
    return content, model


# ===== Anthropic (Messages API, 종량 과금) =====
def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _anthropic_client


async def call_anthropic(system_prompt: str, user_prompt: str) -> tuple[str, str]:
    client = _get_anthropic_client()
    model = settings.anthropic_model
    logger.info("[LLM] Anthropic %s 호출 중...", model)
    message = await client.messages.create(
        model=model,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        temperature=TEMPERATURE,
    )
    parts = [block.text for block in (message.content or []) if getattr(block, "type", "") == "text"]
    content = "\n".join(parts).strip()
    if not content:
        raise ProviderError("Anthropic 응답이 비어 있습니다.")
    return content, model


PROVIDERS = {
    "gemini": call_gemini,
    "openai": call_openai,
    "anthropic": call_anthropic,
}


def reset_clients() -> None:
    """클라이언트 캐시 초기화 (테스트·키 변경용)."""
    global _gemini_client, _openai_client, _anthropic_client
    _gemini_client = _openai_client = _anthropic_client = None


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Hard-coded Credentials: 모든 API 키는 settings(.env/환경변수) 경유, 소스 상수 없음
#   - Improper Input for Security Decision: 폴백 프로바이더 이름을 지원 목록과 대조 후 사용
#   - Improper Exception Handling: 빈 except 없음, 빈 응답은 ProviderError로 명시적 실패 처리
#   - Error Message Information Exposure: 예외 상세는 상위 engine이 로그로만 기록
#   - Uninitialized Variable: 모듈 전역 클라이언트를 None으로 초기화하고 사용 전 지연 생성
# Not Applied:
#   - [WARN] Infinite Loop / 타임아웃: SDK 자체 기본 타임아웃에 의존한다. 상위 engine.generate_briefing이
#     asyncio.timeout(settings.llm_timeout_sec)으로 감싸 실제 상한을 강제한다.
#   - [WARN] Improper Certificate Validation: 각 SDK의 기본 TLS 검증에 의존 (핀닝 없음).
#   - [WARN] 프롬프트에 포함된 뉴스 본문이 외부 API로 전송된다. 포트폴리오 등 비공개 데이터를
#     폴백 경로로 보내고 싶지 않다면 LLM_FALLBACK_PROVIDERS를 비워 둘 것.
# --------------------------------------------------
