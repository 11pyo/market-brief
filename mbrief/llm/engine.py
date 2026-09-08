"""LLM 프로바이더 체인.

순서:
  1. claude_cli 구조화 모드 (--json-schema) → StructuredBriefing 검증  [구독 쿼터, 1순위]
  2. 구조화 검증 실패 시 claude_cli 텍스트 모드 1회 재시도
  3. settings.fallback_providers 순서대로 API 프로바이더 (기본 openai → gemini)
     — 구조화 출력을 지원하는 프로바이더에는 같은 JSON 스키마를 넘긴다. 검증에 실패하면
       그 프로바이더가 돌려준 텍스트를 마크다운 폴백으로 살려 쓴다 (결과를 통째로 버리지 않는다).

로컬 CLI가 미로그인·미설치라 1~2를 건너뛴 경우, 그 사실을 조용히 넘기지 않고
LLMResult.notice에 담아 CLI 출력·로그·텔레그램 알림으로 사용자에게 표면화한다.
모두 실패하면 조치 가능한 한국어 메시지와 함께 RuntimeError를 던진다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from pydantic import ValidationError

from mbrief.config import settings
from mbrief.llm import api_providers, claude_cli, prompts
from mbrief.schemas import StructuredBriefing

logger = logging.getLogger(__name__)

_MIN_PROVIDER_TIMEOUT = 30
_MAX_PROVIDER_TIMEOUT = 1800

# 로컬 CLI(구독 쿼터)를 쓰지 못한 이유를 사용자에게 그대로 보여줄 안내문.
# 생성 자체는 API 폴백으로 계속 진행하되, 원인을 조용히 삼키지 않는다.
CLI_LOGIN_NOTICE = (
    "로컬 Claude Code CLI 인증이 없어 구독 쿼터를 사용하지 못했습니다. "
    "터미널에서 `claude auth login` 실행 후 `python -m mbrief doctor`로 확인하세요. "
    "이번 브리핑은 API 폴백으로 생성했습니다."
)
CLI_MISSING_NOTICE = (
    "로컬 Claude Code CLI를 찾지 못했습니다 (미설치 또는 CLAUDE_CLI_ENABLED=false). "
    "설치: npm i -g @anthropic-ai/claude-code → `claude auth login`. "
    "이번 브리핑은 API 폴백으로 생성했습니다."
)


@dataclass
class LLMResult:
    """브리핑 1건 생성 결과.

    엔진은 마크다운을 렌더하지 않는다. 구조화 본문(structured)과 텍스트 폴백 원문(text)만
    돌려주고, contentMarkdown 렌더는 pipeline._build_record 한 곳에서만 수행한다
    (PART 8 공개 여부 판단이 두 곳으로 갈라지지 않도록).
    """
    provider: str
    model: str
    structured: StructuredBriefing | None
    text: str
    elapsed_ms: int
    # 로컬 CLI를 쓰지 못하고 API 폴백으로 생성했을 때의 사용자 안내 (없으면 빈 문자열)
    login_required: bool = False
    notice: str = ""


def _provider_timeout() -> int:
    """[SECURE] API 폴백 프로바이더 호출 타임아웃 상·하한 고정 - 무한 대기 방지 (Category 3).

    claude_cli 경로에는 적용하지 않는다. CLI는 자체적으로 시도당
    settings.claude_cli_timeout_sec 타임아웃 + 프로세스 트리 kill을 수행하는데,
    그보다 짧은 바깥 타임아웃을 씌우면 재시도가 실행되기도 전에 호출이 취소된다.
    """
    return max(_MIN_PROVIDER_TIMEOUT, min(int(settings.llm_timeout_sec), _MAX_PROVIDER_TIMEOUT))


def validate_structured(payload: dict) -> StructuredBriefing:
    """CLI·API 구조화 출력 dict → StructuredBriefing.

    [SECURE] 외부(LLM) 데이터를 pydantic으로 검증한 뒤에만 사용 - Unsafe Deserialization 방지 (Category 5).
    """
    return StructuredBriefing.model_validate(payload)


def _json_object_or_none(text: str) -> dict | None:
    """텍스트가 JSON 객체면 dict, 아니면 None.

    프로바이더가 스키마를 거부해 텍스트 모드로 재시도했더라도 프롬프트는 JSON을 요구했으므로
    본문이 그대로 JSON일 수 있다. 그 경우를 구조화 출력으로 살려 쓴다.
    """
    body = (text or "").strip()
    if not body.startswith("{"):
        return None
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError, RecursionError) as e:
        # [SECURE] 빈 except 없음 - 파싱 실패는 디버그 로그로 남기고 텍스트로 취급 (Category 4)
        logger.debug("[LLM] 응답 JSON 파싱 실패(텍스트로 취급): %s", str(e)[:200])
        return None
    return data if isinstance(data, dict) else None


def _structured_from_provider(result: api_providers.ProviderResult) -> StructuredBriefing | None:
    """API 프로바이더 응답 → 검증된 StructuredBriefing. 검증 실패면 None (텍스트 폴백 사용)."""
    payload = result.structured
    if payload is None:
        payload = _json_object_or_none(result.text)
    if payload is None:
        return None
    try:
        return validate_structured(payload)
    except ValidationError as e:
        # [SECURE] 검증 실패 상세는 로컬 로그에만 (Category 4)
        logger.warning("[LLM] API 구조화 출력 검증 실패(%d건) - 텍스트 폴백으로 사용", e.error_count())
        logger.debug("[LLM] 검증 오류: %s", str(e)[:1000])
        return None


async def _try_claude_structured(
    system_prompt: str,
    user_prompt: str,
    model: str | None,
) -> tuple[StructuredBriefing, str, str] | None:
    """구조화 모드 시도. 성공 시 (structured, model_id, markdown_source)."""
    result = await claude_cli.generate(
        system_prompt,
        user_prompt,
        json_schema=prompts.STRUCTURED_JSON_SCHEMA,
        model=model,
    )
    if result.structured is None:
        logger.warning("[LLM] 구조화 출력 없음 - 텍스트 폴백으로 전환")
        return None
    try:
        structured = validate_structured(result.structured)
    except ValidationError as e:
        # [SECURE] 검증 실패 상세는 로컬 로그에만 (Category 4)
        logger.warning("[LLM] 구조화 출력 검증 실패(%d건) - 텍스트 폴백으로 전환", e.error_count())
        logger.debug("[LLM] 검증 오류: %s", str(e)[:1000])
        return None
    return structured, result.model_id, result.text


async def generate_briefing(
    news_text: str,
    market_text: str,
    portfolio_text: str = "",
    lang: str = "ko",
    *,
    model: str | None = None,
    clock_text: str = "",
) -> LLMResult:
    """브리핑 생성. 구조화 우선, 실패 시 텍스트 폴백.

    clock_text는 생성 시점의 시장 시계(marketclock.format_for_llm)다. 데이터 경계 앞에 놓여
    모델이 시제를 고르는 근거가 된다 — 휴장일에 "어젯밤 마감" 같은 서술을 막는다.
    """
    # 생성과 공개는 분리한다 (의도적 비대칭):
    #   - 구조화 경로: 포트폴리오가 있으면 항상 portfolioNote를 요청한다. portfolioNote는 독립
    #     필드라서 공개 산출물 생성 직전에 builder.public_record가 통째로 제거할 수 있다.
    #     덕분에 PUBLISH_PORTFOLIO_NOTE를 끈 상태에서도 소유자는 로컬 레코드/`mbrief show --private`로
    #     조언을 볼 수 있다.
    #   - 텍스트 경로: 본문 마크다운 한 덩어리라 사후 제거가 문자열 매칭에 의존한다. 따라서
    #     보수적으로 publish_portfolio_note가 켜진 경우에만 PART 8 생성을 요청한다.
    # [SECURE] 텍스트 폴백 프롬프트는 publish_portfolio_note가 켜졌을 때만 PART 8을 요청한다
    #          - 비공개 조언이 공개 산출물 경로로 새는 것을 방지 (Category 2: Improper Authorization)
    include_portfolio = bool(portfolio_text) and settings.publish_portfolio_note
    structured_prompt = prompts.build_user_prompt(
        news_text, market_text, portfolio_text, lang, clock_text=clock_text,
    )
    text_prompt = prompts.build_text_prompt(
        news_text, market_text, portfolio_text, lang, include_portfolio, clock_text=clock_text,
    )
    # [SECURE] 비공개 포트폴리오는 로컬 claude_cli 경로에만 전달한다. 외부 API 폴백
    #          (Gemini/OpenAI/Anthropic)에는 설정과 무관하게 항상 제외한다. 구조화 프롬프트도
    #          portfolio_text=""로 따로 만든다 - 제3자 서비스로의 개인정보 전송 차단
    #          (Category 2: Unencrypted Sensitive Data)
    api_structured_prompt = prompts.build_user_prompt(
        news_text, market_text, portfolio_text="", lang=lang, clock_text=clock_text,
    )
    api_text_prompt = prompts.build_text_prompt(
        news_text, market_text, portfolio_text="", lang=lang, include_portfolio=False,
        clock_text=clock_text,
    )

    start = time.time()
    timeout = _provider_timeout()
    errors: list[str] = []
    notice = ""
    login_required = False

    # ── 1) claude_cli 구조화 모드 ────────────────────────────────
    # 바깥 asyncio.timeout을 씌우지 않는다: claude_cli.generate가 시도당
    # settings.claude_cli_timeout_sec 타임아웃 + 프로세스 트리 kill을 이미 강제하며,
    # 더 짧은 바깥 타임아웃은 재시도 전에 호출을 취소해 불필요한 API 폴백을 유발한다.
    if claude_cli.available():
        try:
            outcome = await _try_claude_structured(prompts.SYSTEM_PROMPT, structured_prompt, model)
            if outcome is not None:
                structured, model_id, _ = outcome
                elapsed = int((time.time() - start) * 1000)
                logger.info("[LLM] 완료(구조화) %dms | provider=claude_cli | model=%s", elapsed, model_id)
                return LLMResult("claude_cli", model_id, structured, "", elapsed)
        except claude_cli.ClaudeCliAuthError as e:
            errors.append(f"claude_cli 인증 실패: {e}")
            login_required, notice = True, CLI_LOGIN_NOTICE
            logger.error("[LLM] claude_cli 인증 실패 - %s", claude_cli.LOGIN_HINT)
        except (claude_cli.ClaudeCliError, asyncio.TimeoutError, OSError) as e:
            errors.append(f"claude_cli 구조화 실패: {e}")
            logger.warning("[LLM] claude_cli 구조화 실패: %s", e)

        # ── 2) claude_cli 텍스트 모드 1회 재시도 ──────────────────
        if claude_cli.available():
            try:
                result = await claude_cli.generate(prompts.SYSTEM_PROMPT, text_prompt, model=model)
                content = (result.text or "").strip()
                if content:
                    elapsed = int((time.time() - start) * 1000)
                    logger.info("[LLM] 완료(텍스트) %dms | provider=claude_cli | model=%s",
                                elapsed, result.model_id)
                    return LLMResult("claude_cli", result.model_id, None, content, elapsed)
                errors.append("claude_cli 텍스트 응답이 비어 있음")
            except claude_cli.ClaudeCliAuthError as e:
                errors.append(f"claude_cli 인증 실패: {e}")
                login_required, notice = True, CLI_LOGIN_NOTICE
                logger.error("[LLM] claude_cli 인증 실패 - %s", claude_cli.LOGIN_HINT)
            except (claude_cli.ClaudeCliError, asyncio.TimeoutError, OSError) as e:
                errors.append(f"claude_cli 텍스트 실패: {e}")
                logger.warning("[LLM] claude_cli 텍스트 실패: %s", e)
    else:
        # 미설치와 미로그인(인증 백오프)은 사용자가 취해야 할 조치가 다르므로 구분해 안내한다.
        if claude_cli.resolve_cli() is None:
            notice = CLI_MISSING_NOTICE
        else:
            login_required, notice = True, CLI_LOGIN_NOTICE
        errors.append("claude_cli 사용 불가 (미설치 또는 인증 백오프 중)")
        logger.error("[LLM] %s", notice)

    # ── 3) API 폴백 (구조화 우선, 텍스트 폴백) ──────────────────
    # SDK마다 자체 타임아웃 보장이 다르므로 이 구간에만 바깥 타임아웃을 씌운다.
    # 프롬프트는 포트폴리오를 제거한 api_* 프롬프트만 사용한다(외부 전송 금지).
    for name in api_providers.available_providers():
        call = api_providers.PROVIDERS.get(name)
        if call is None:
            continue
        wants_schema = api_providers.supports_structured(name)
        user_prompt = api_structured_prompt if wants_schema else api_text_prompt
        schema = prompts.STRUCTURED_JSON_SCHEMA if wants_schema else None
        try:
            async with asyncio.timeout(timeout):
                outcome = await call(prompts.SYSTEM_PROMPT, user_prompt, json_schema=schema)

            structured = _structured_from_provider(outcome) if wants_schema else None
            content = (outcome.text or "").strip()
            if structured is None and not content:
                raise api_providers.ProviderError("빈 응답")

            elapsed = int((time.time() - start) * 1000)
            if structured is not None:
                # 구조화가 성공하면 원문 JSON은 버린다. 마크다운은 pipeline이 렌더한다
                # (claude_cli 구조화 경로와 동일한 계약).
                logger.info("[LLM] 완료(구조화) %dms | provider=%s | model=%s",
                            elapsed, name, outcome.model)
                return LLMResult(name, outcome.model, structured, "", elapsed,
                                 login_required=login_required, notice=notice)

            # 구조화 실패 → 프로바이더가 돌려준 본문을 마크다운 폴백으로 살려 쓴다.
            logger.info("[LLM] 완료(텍스트) %dms | provider=%s | model=%s",
                        elapsed, name, outcome.model)
            return LLMResult(name, outcome.model, None, content, elapsed,
                             login_required=login_required, notice=notice)
        except Exception as e:  # SDK별 예외 타입이 제각각이라 광범위하게 잡되 반드시 로깅한다
            # [SECURE] 상세 오류는 로컬 로그에만, 다음 프로바이더로 전이 (Category 4)
            errors.append(f"{name} 실패: {type(e).__name__}")
            logger.warning("[LLM] provider '%s' 실패: %s → 다음 프로바이더 시도", name, e)

    detail = " | ".join(errors[:5]) or "사용 가능한 프로바이더 없음"
    raise RuntimeError(
        "모든 LLM 프로바이더가 실패했습니다. "
        f"터미널에서 `claude auth login`으로 재로그인하거나 .env에 OPENAI_API_KEY / GEMINI_API_KEY를 설정하세요. "
        f"(원인: {detail})"
    )


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Unsafe Deserialization: LLM 구조화 출력을 StructuredBriefing.model_validate로 검증한 뒤에만 사용
#   - Infinite Loop: API 폴백은 asyncio.timeout(30~1800s) 상한, claude_cli는 시도당 자체 타임아웃 +
#     프로세스 트리 kill + 재시도 횟수 상한으로 무한 대기를 막는다. 체인은 유한 목록 순회
#   - Improper Exception Handling: 빈 except 없음, 모든 실패를 로깅 후 다음 경로로 전이
#   - Error Message Information Exposure: 실패 상세는 로그에만, 상위에는 요약과 조치 안내만 전달
#   - Hard-coded Credentials: 키는 전부 settings 경유, 소스 상수 없음
#   - Improper Authorization: 텍스트 폴백 프롬프트는 publish_portfolio_note가 켜진 경우에만
#     PART 8(비공개 포트폴리오 조언)을 요청한다. 구조화 모드는 별도 필드(portfolioNote)라
#     항상 요청하되 공개 여부는 builder.public_record / pipeline 렌더가 단독으로 판단한다
#   - Unencrypted Sensitive Data(제3자 전송 금지): 외부 API 폴백(Gemini/OpenAI/Anthropic)에는
#     portfolio_text=""로 만든 api_structured_prompt / api_text_prompt만 보낸다. 설정값과 무관하게
#     비공개 포트폴리오는 로컬 claude_cli 경로에서만 사용된다
#   - Unsafe Deserialization: API 프로바이더의 구조화 출력도 claude_cli와 동일하게
#     StructuredBriefing.model_validate로 검증한 뒤에만 사용하고, 실패 시 텍스트로 강등한다
#   - OS Command Injection: CLI 실행은 claude_cli의 argv 화이트리스트 경로만 사용
# Not Applied:
#   - [WARN] 광범위 except: API 폴백 구간은 SDK별 예외 계층이 제각각이라 Exception을 잡는다.
#     반드시 로깅 후 다음 프로바이더로 넘어가며, 조용히 삼키지 않는다.
#   - [WARN] Missing Rate Limit: 생성 호출 빈도 제한은 pipeline.daily의 단일 lock 파일에만 의존한다.
#   - [WARN] claude_cli 구간에는 전체 소요시간 상한이 없다. 최악의 경우
#     claude_cli_timeout_sec x (claude_cli_retries + 1) x 2(구조화+텍스트)까지 걸릴 수 있다.
# --------------------------------------------------
