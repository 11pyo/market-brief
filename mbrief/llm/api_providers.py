"""API 폴백 프로바이더 (구조화 출력 우선, 텍스트 폴백).

claude_cli가 실패했을 때만 사용한다. 키가 설정된 프로바이더만 체인에 참여한다.

각 함수 시그니처:
    async def call(system_prompt, user_prompt, json_schema: dict | None = None) -> ProviderResult

json_schema를 주면 프로바이더의 네이티브 구조화 출력 기능을 사용한다:
  - OpenAI : chat.completions.create(response_format={"type": "json_schema", ...})
  - Gemini : GenerateContentConfig(response_mime_type="application/json", response_schema=...)
  - Anthropic : 미지원(텍스트 전용). 인자는 받되 무시한다.
스키마가 거부되면 **같은 프로바이더로 1회만** 스키마 없이 재호출하고 텍스트만 돌려준다.
구조화 결과의 최종 검증(pydantic)은 engine이 수행한다 — 여기서는 JSON 객체까지만 만든다.

클라이언트는 최초 호출 시점에 지연 초기화한다 (미사용 SDK 임포트 비용 회피).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from mbrief.config import settings

logger = logging.getLogger(__name__)

# 구조화 JSON은 마크다운보다 토큰을 더 먹는다(필드명·따옴표·이스케이프). 상한이 낮으면
# 응답이 중간에서 잘려 JSON 파싱이 실패하고 텍스트 폴백으로 떨어진다.
MAX_OUTPUT_TOKENS = 8000
TEMPERATURE = 0.7

# 구조화 출력을 지원하는 프로바이더. engine이 json_schema 전달 여부를 판단할 때 쓴다.
STRUCTURED_PROVIDERS = frozenset({"openai", "gemini"})

# Gemini의 response_schema(OpenAPI 3.0 서브셋)가 해석하지 못하는 JSON Schema 키.
_GEMINI_UNSUPPORTED_KEYS = frozenset({
    "additionalProperties", "$schema", "$defs", "$ref", "examples",
})
# [SECURE] 스키마 변환 재귀 깊이 상한 - 무한 재귀 방지 (Category 3: Infinite Loop or Recursion)
_MAX_SCHEMA_DEPTH = 32

_gemini_client = None
_openai_client = None
_anthropic_client = None


class ProviderError(RuntimeError):
    """API 프로바이더 호출 실패."""


@dataclass
class ProviderResult:
    """프로바이더 1회 호출 결과.

    text       : 모델이 돌려준 원문 (구조화 모드에서는 JSON 문자열)
    structured : JSON 객체로 파싱된 구조화 출력. 없으면 None (텍스트 폴백)
    model      : 실제 사용한 모델 ID
    """
    text: str = ""
    structured: dict | None = None
    model: str = ""


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


def supports_structured(name: str) -> bool:
    """해당 프로바이더가 네이티브 구조화 출력을 지원하는지."""
    return name in STRUCTURED_PROVIDERS


# ===== 공통 헬퍼 =====
def _json_object(text: str) -> dict:
    """모델 응답 문자열 → JSON 객체. 객체가 아니면 ValueError.

    [SECURE] 여기서는 형식(dict)만 확인한다. 필드 검증은 engine의 pydantic 계층이 수행한다
    - Unsafe Deserialization 방지 (Category 5).
    """
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("구조화 응답이 JSON 객체가 아닙니다.")
    return data


def _optional_exception_types(import_path: str, *names: str) -> tuple[type[BaseException], ...]:
    """설치돼 있을 때만 SDK 예외 타입을 모아 온다. 미설치면 빈 튜플.

    SDK를 선택 의존성으로 두면서도 except 절에서 구체 타입을 쓰기 위한 우회다
    (광범위한 except Exception을 피한다).
    """
    try:
        module = __import__(import_path, fromlist=["*"])
    except ImportError as e:
        logger.debug("[LLM] 선택 SDK 미설치(%s): %s", import_path, e)
        return ()
    found: list[type[BaseException]] = []
    for name in names:
        candidate = getattr(module, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            found.append(candidate)
    return tuple(found)


def to_gemini_schema(schema: Any, _depth: int = 0) -> Any:
    """JSON Schema → Gemini response_schema 호환 형태.

    Gemini(OpenAPI 3.0 서브셋)는 additionalProperties / $schema / $defs / $ref / examples 를
    해석하지 못한다. 재귀적으로 제거하고, 유니온 타입(["string","null"])은 nullable로 바꾼다.
    원본 딕셔너리는 변형하지 않는다 (호출부가 같은 상수를 계속 재사용하므로 필수).
    """
    if _depth > _MAX_SCHEMA_DEPTH:
        # [SECURE] 재귀 깊이 상한 초과는 조용히 자르지 않고 명시적으로 실패시킨다 (Category 3)
        raise ValueError("JSON 스키마 중첩 깊이가 상한을 초과했습니다.")

    if isinstance(schema, dict):
        converted: dict[str, Any] = {}
        for key, value in schema.items():
            if key in _GEMINI_UNSUPPORTED_KEYS:
                continue
            if key == "type" and isinstance(value, list):
                # ["string", "null"] → type=string + nullable=true
                concrete = [t for t in value if t != "null"]
                converted["type"] = concrete[0] if concrete else "string"
                if len(concrete) != len(value):
                    converted["nullable"] = True
                continue
            converted[key] = to_gemini_schema(value, _depth + 1)
        return converted
    if isinstance(schema, list):
        return [to_gemini_schema(item, _depth + 1) for item in schema]
    return schema


# ===== Gemini =====
def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        # [SECURE] API 키는 settings(환경변수)에서만 로드 - Hard-coded Credentials 방지 (Category 2)
        _gemini_client = genai.Client(api_key=settings.gemini_api_key)
    return _gemini_client


def _gemini_schema_error_types() -> tuple[type[BaseException], ...]:
    """스키마 거부로 볼 수 있는 예외 타입 묶음."""
    return (
        *_optional_exception_types("google.genai.errors", "ClientError", "UnknownFunctionCallArgumentError"),
        TypeError, ValueError, AttributeError, KeyError,
    )


async def call_gemini(
    system_prompt: str,
    user_prompt: str,
    json_schema: dict | None = None,
) -> ProviderResult:
    """Gemini 호출. 지정 모델 실패 시 flash-lite 계열로 자동 폴백."""
    from google.genai import types

    client = _get_gemini_client()
    candidate_models = [settings.gemini_model, "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    # 중복 제거 (순서 보존)
    seen = set()
    models = [m for m in candidate_models if not (m in seen or seen.add(m))]

    last_error: Exception | None = None

    for model in models:
        def _config(schema: dict | None):
            kwargs: dict[str, Any] = {
                "system_instruction": system_prompt,
                "temperature": TEMPERATURE,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
            }
            if schema is not None:
                kwargs["response_mime_type"] = "application/json"
                kwargs["response_schema"] = schema
            return types.GenerateContentConfig(**kwargs)

        async def _call(schema: dict | None, m: str) -> str:
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=m,
                contents=user_prompt,
                config=_config(schema),
            )
            return (getattr(response, "text", "") or "").strip()

        if json_schema is not None:
            logger.info("[LLM] Gemini %s 호출 중... (구조화)", model)
            try:
                content = await _call(to_gemini_schema(json_schema), model)
                if not content:
                    raise ValueError(f"Gemini {model} 구조화 응답이 비어 있습니다.")
                return ProviderResult(text=content, structured=_json_object(content), model=model)
            except _gemini_schema_error_types() as e:
                logger.warning(
                    "[LLM] Gemini %s 구조화 실패(%s: %s) - 텍스트 재시도",
                    model, type(e).__name__, str(e)[:200],
                )
                last_error = e
            except Exception as e:
                logger.warning(
                    "[LLM] Gemini %s 모델 호출 실패(%s: %s) - 다음 후보 모델 시도",
                    model, type(e).__name__, str(e)[:200],
                )
                last_error = e
                continue

        logger.info("[LLM] Gemini %s 호출 중... (텍스트)", model)
        try:
            content = await _call(None, model)
            if content:
                return ProviderResult(text=content, structured=None, model=model)
        except Exception as e:
            logger.warning("[LLM] Gemini %s 텍스트 호출 실패: %s", model, str(e)[:200])
            last_error = e
            continue

    if last_error:
        raise ProviderError(f"Gemini 모든 모델 호출 실패: {last_error}")
    raise ProviderError("Gemini 응답이 비어 있습니다.")


# ===== OpenAI =====
def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI
        _openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _openai_client


def _openai_schema_error_types() -> tuple[type[BaseException], ...]:
    """json_schema 거부로 볼 수 있는 예외 타입 묶음 (json.JSONDecodeError ⊂ ValueError)."""
    return (
        *_optional_exception_types(
            "openai", "BadRequestError", "UnprocessableEntityError", "NotFoundError",
        ),
        TypeError, ValueError,
    )


def _openai_content(resp: Any) -> str:
    """chat.completions 응답 → 본문 문자열. 형태가 다르면 빈 문자열."""
    choices = getattr(resp, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    return (getattr(message, "content", "") or "").strip()


async def call_openai(
    system_prompt: str,
    user_prompt: str,
    json_schema: dict | None = None,
) -> ProviderResult:
    """OpenAI 호출. json_schema를 주면 response_format=json_schema 구조화 출력을 요청한다.

    모델이나 API가 json_schema를 거부하면(구형 모델·미지원 파라미터) 같은 모델로
    response_format 없이 1회 재시도하고 텍스트만 돌려준다.
    """
    client = _get_openai_client()
    model = settings.openai_model
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    if json_schema is not None:
        logger.info("[LLM] OpenAI %s 호출 중... (구조화)", model)
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=TEMPERATURE,
                max_tokens=MAX_OUTPUT_TOKENS,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "market_briefing",
                        # strict=False: 스키마 전체를 OpenAI의 엄격 서브셋에 맞추지 않아도 되게 한다
                        # (엄격 모드는 모든 프로퍼티를 required로 요구하는 등 제약이 크다).
                        "strict": False,
                        "schema": json_schema,
                    },
                },
            )
            content = _openai_content(resp)
            if not content:
                raise ValueError("OpenAI 구조화 응답이 비어 있습니다.")
            return ProviderResult(text=content, structured=_json_object(content), model=model)
        except _openai_schema_error_types() as e:
            # [SECURE] 빈 except 없음 - 원인을 로그로 남기고 스키마 없이 1회만 재시도 (Category 4)
            logger.warning(
                "[LLM] OpenAI 구조화 출력 실패(%s: %s) - response_format 없이 1회 재시도",
                type(e).__name__, str(e)[:200],
            )

    logger.info("[LLM] OpenAI %s 호출 중... (텍스트)", model)
    resp = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=TEMPERATURE,
        max_tokens=MAX_OUTPUT_TOKENS,
    )
    content = _openai_content(resp)
    if not content:
        raise ProviderError("OpenAI 응답이 비어 있습니다.")
    return ProviderResult(text=content, structured=None, model=model)


# ===== Anthropic (Messages API, 종량 과금) =====
def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _anthropic_client


async def call_anthropic(
    system_prompt: str,
    user_prompt: str,
    json_schema: dict | None = None,
) -> ProviderResult:
    """Anthropic 호출 — 텍스트 전용.

    종량 과금 경로라 우선순위가 낮다. 구조화 출력(tool 강제)은 구현하지 않고,
    json_schema 인자는 계약을 맞추기 위해 받되 무시한다.
    """
    if json_schema is not None:
        logger.debug("[LLM] Anthropic 프로바이더는 구조화 출력을 지원하지 않음 - 텍스트 모드로 호출")

    client = _get_anthropic_client()
    model = settings.anthropic_model
    logger.info("[LLM] Anthropic %s 호출 중... (텍스트)", model)
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
    return ProviderResult(text=content, structured=None, model=model)


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
#   - Unsafe Deserialization: 구조화 응답은 json.loads 후 dict 여부만 확인하고, 필드 검증은
#     engine의 StructuredBriefing.model_validate에 위임한다 (검증 전 사용 금지)
#   - Improper Exception Handling: 빈 except 없음. 스키마 거부는 구체 예외 타입으로만 잡고
#     반드시 경고 로그를 남긴 뒤 1회 재시도한다 (무한 재시도 없음)
#   - Infinite Loop or Recursion: to_gemini_schema에 재귀 깊이 상한(32), 스키마 폴백은 1회로 고정
#   - Error Message Information Exposure: 예외 상세는 200자로 잘라 로컬 로그에만 기록
#   - Uninitialized Variable: 모듈 전역 클라이언트를 None으로 초기화하고 사용 전 지연 생성
# Not Applied:
#   - [WARN] Infinite Loop / 타임아웃: SDK 자체 기본 타임아웃에 의존한다. 상위 engine.generate_briefing이
#     asyncio.timeout(settings.llm_timeout_sec)으로 감싸 실제 상한을 강제한다. 스키마 폴백 재시도까지
#     같은 타임아웃 안에서 일어난다.
#   - [WARN] Improper Certificate Validation: 각 SDK의 기본 TLS 검증에 의존 (핀닝 없음).
#   - [WARN] 프롬프트에 포함된 뉴스 본문이 외부 API로 전송된다. 포트폴리오 등 비공개 데이터를
#     폴백 경로로 보내고 싶지 않다면 LLM_FALLBACK_PROVIDERS를 비워 둘 것.
#     (engine은 설정과 무관하게 포트폴리오를 이 경로로 보내지 않는다.)
# --------------------------------------------------
