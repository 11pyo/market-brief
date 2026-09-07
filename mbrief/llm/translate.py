"""추가 언어 번역 패스.

원문(기본 언어) StructuredBriefing을 그대로 유지한 채 문자열 필드만 번역한다.
분석을 다시 하지 않으므로 저렴한 모델(settings.claude_cli_translate_model, 기본 haiku)을 쓴다.
실패하면 None을 반환하고 상위(pipeline)가 해당 언어를 건너뛴다.
"""
from __future__ import annotations

import json
import logging

from pydantic import ValidationError

from mbrief.config import settings
from mbrief.llm import claude_cli, prompts
from mbrief.schemas import StructuredBriefing

logger = logging.getLogger(__name__)

TRANSLATE_SYSTEM_PROMPT = """You are a professional financial translator.

You receive one JSON object describing a market briefing and must return the SAME JSON object
with every human-readable string field translated into the target language.

RULES:
  - Translate every string field faithfully. Do not summarize, expand, reorder, or add commentary.
  - Keep all numbers, percentages, price levels, dates and ratios exactly as they are.
  - Keep tickers and ETF symbols (NVDA, XLK, SPX, BTC, KOSPI, ...) untranslated.
  - Keep markdown formatting (bold, bullets, line breaks) intact.
  - Keep enum values in English exactly: severity = high|medium|low, side = long|short.
  - Keep integer fields (direction.up / flat / down) unchanged.
  - Keep null as null (portfolioNote).
  - Output the JSON object only. No prose, no markdown fence."""

_MAX_PAYLOAD_CHARS = 200_000  # [SECURE] 프롬프트 입력 상한 - 자원 고갈 방지 (Category 3)


def build_translate_prompt(structured: StructuredBriefing, source_lang: str, target_lang: str) -> str:
    """번역 요청 프롬프트."""
    source_name = prompts.LANG_NAMES.get(source_lang, source_lang)
    target_name = prompts.LANG_NAMES.get(target_lang, target_lang)
    payload = structured.model_dump_json(indent=None)
    if len(payload) > _MAX_PAYLOAD_CHARS:
        raise ValueError("번역 대상 본문이 상한을 초과했습니다.")
    return (
        f"Source language: {source_name}\n"
        f"Target language: {target_name}\n\n"
        "Translate every string value of this JSON object into the target language and return the JSON object:\n\n"
        f"{payload}"
    )


async def translate_structured(
    structured: StructuredBriefing,
    source_lang: str,
    target_lang: str,
) -> StructuredBriefing | None:
    """구조화 브리핑 번역. 실패 시 None (경고 로깅)."""
    if source_lang == target_lang:
        return structured

    try:
        user_prompt = build_translate_prompt(structured, source_lang, target_lang)
    except ValueError as e:
        logger.warning("[Translate] 프롬프트 생성 실패: %s", e)
        return None

    if not claude_cli.available():
        logger.warning("[Translate] claude CLI 사용 불가 - %s 번역 건너뜀", target_lang)
        return None

    try:
        # [SECURE] 번역 패스는 보조 단계다. 1회 시도 + claude_cli_timeout_sec 상한으로 전체 소요시간을
        #          유한하게 묶는다 - 무한 대기/파이프라인 지연 방지 (Category 3: Infinite Loop)
        result = await claude_cli.generate(
            TRANSLATE_SYSTEM_PROMPT,
            user_prompt,
            json_schema=prompts.STRUCTURED_JSON_SCHEMA,
            model=settings.claude_cli_translate_model,
            timeout_sec=int(settings.claude_cli_timeout_sec),
            retries=0,
        )
    except claude_cli.ClaudeCliAuthError:
        logger.warning("[Translate] 인증 실패 - %s", claude_cli.LOGIN_HINT)
        return None
    except (claude_cli.ClaudeCliError, OSError) as e:
        # [SECURE] 상세 오류는 로컬 로그로만 (Category 4)
        logger.warning("[Translate] %s 번역 실패: %s", target_lang, e)
        return None

    payload = result.structured
    if payload is None and result.text:
        try:
            candidate = json.loads(result.text)
            payload = candidate if isinstance(candidate, dict) else None
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("[Translate] 응답 JSON 파싱 실패: %s", e)
            return None

    if payload is None:
        logger.warning("[Translate] %s 번역 응답에 구조화 출력이 없습니다.", target_lang)
        return None

    try:
        # [SECURE] 번역 결과도 pydantic으로 재검증 - Unsafe Deserialization 방지 (Category 5)
        return StructuredBriefing.model_validate(payload)
    except ValidationError as e:
        logger.warning("[Translate] %s 번역 결과 검증 실패(%d건)", target_lang, e.error_count())
        logger.debug("[Translate] 검증 오류: %s", str(e)[:1000])
        return None


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Unsafe Deserialization: 번역 결과 dict를 StructuredBriefing.model_validate로 검증 후 사용
#   - Infinite Loop: 1회 시도(retries=0) + claude_cli_timeout_sec 명시 상한으로 번역 패스의
#     최대 소요시간을 유한하게 고정. 자체 루프 없음
#   - Improper Exception Handling: 빈 except 없음, 모든 실패 경로에서 경고 로깅 후 None 반환
#   - Error Message Information Exposure: 검증 오류 상세는 DEBUG 레벨 로컬 로그로만 기록
#   - 자원 고갈 방지: 번역 대상 본문 길이 상한(200k자) 검사
# Not Applied:
#   - [WARN] 번역 품질은 검증하지 않는다. 스키마만 통과하면 저장되므로, 오역이 남을 수 있다.
#   - [WARN] 원문에 포함된 비공개 포트폴리오 조언(portfolioNote)도 함께 번역 요청에 실린다.
#     로컬 CLI 경로이므로 외부 API로 나가지 않지만, 폴백 API 사용 시에는 이 함수가 호출되지 않는다.
# --------------------------------------------------
