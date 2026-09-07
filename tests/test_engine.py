"""LLM 프로바이더 체인 테스트 — 실제 CLI/API 호출은 전부 대체한다 (네트워크·쿼터 미사용).

여기서 고정하는 회귀
  1. claude_cli 경로에는 llm_timeout_sec(바깥 타임아웃)을 씌우지 않는다.
     씌우면 CLI 자체 타임아웃(claude_cli_timeout_sec)보다 먼저 취소되어 재시도가 실행되지 않고
     불필요하게 API 폴백(Gemini 등)으로 떨어진다.
  2. API 폴백 구간에는 여전히 바깥 타임아웃이 적용된다.
  3. 엔진은 마크다운을 렌더하지 않는다 (PART 7 공개 판단을 pipeline 한 곳으로 모으기 위함).
"""
from __future__ import annotations

import asyncio

import pytest

from mbrief.llm import api_providers, claude_cli, engine, prompts, translate
from mbrief.schemas import StructuredBriefing


def _structured_payload() -> dict:
    return {
        "headline": "테스트 헤드라인",
        "overnight": "간밤 이벤트 본문",
        "direction": {"up": 50, "flat": 30, "down": 20},
        "sectorLeaders": [],
        "sectorLaggards": [],
        "risks": [],
        "trades": [],
        "dashboard": [],
        "portfolioNote": "비공개 조언",
    }


@pytest.fixture
def cli_available(monkeypatch):
    monkeypatch.setattr(claude_cli, "available", lambda: True)
    monkeypatch.setattr(api_providers, "available_providers", lambda: [])


def _run(**kwargs) -> engine.LLMResult:
    return asyncio.run(engine.generate_briefing("NEWS", "MARKET", **kwargs))


# ===== 1. claude_cli는 바깥 타임아웃에 묶이지 않는다 =====
def test_claude_cli_is_not_cancelled_by_provider_timeout(monkeypatch, cli_available):
    """회귀: llm_timeout_sec < claude_cli_timeout_sec 여도 구조화 호출이 취소되면 안 된다."""
    monkeypatch.setattr(engine, "_provider_timeout", lambda: 0)  # 예전 코드라면 즉시 취소되는 값

    async def slow_generate(system_prompt, user_prompt, **kw):
        await asyncio.sleep(0.15)  # CLI가 오래 걸리는 상황을 흉내
        return claude_cli.CliResult(
            text="", structured=_structured_payload(), model_id="claude-sonnet-5",
        )

    monkeypatch.setattr(claude_cli, "generate", slow_generate)

    result = _run()
    assert result.provider == "claude_cli"
    assert result.model == "claude-sonnet-5"
    assert isinstance(result.structured, StructuredBriefing)


def test_claude_cli_text_fallback_is_not_cancelled_by_provider_timeout(monkeypatch, cli_available):
    monkeypatch.setattr(engine, "_provider_timeout", lambda: 0)
    calls: list[bool] = []

    async def generate(system_prompt, user_prompt, *, json_schema=None, **kw):
        calls.append(json_schema is not None)
        await asyncio.sleep(0.1)
        if json_schema is not None:
            return claude_cli.CliResult(text="", structured=None, model_id="claude-sonnet-5")
        return claude_cli.CliResult(text="## PART 1 — 본문", structured=None, model_id="claude-haiku")

    monkeypatch.setattr(claude_cli, "generate", generate)

    result = _run()
    assert calls == [True, False]  # 구조화 실패 → 텍스트 모드 재시도까지 도달했다
    assert result.provider == "claude_cli"
    assert result.structured is None
    assert result.text == "## PART 1 — 본문"


# ===== 2. API 폴백에는 타임아웃이 남아 있다 =====
def test_api_fallback_is_still_bounded_by_provider_timeout(monkeypatch):
    monkeypatch.setattr(claude_cli, "available", lambda: False)
    monkeypatch.setattr(engine, "_provider_timeout", lambda: 0)
    monkeypatch.setattr(api_providers, "available_providers", lambda: ["gemini"])

    async def slow_call(system_prompt, user_prompt):
        await asyncio.sleep(5)
        return "본문", "gemini-2.5-flash"

    monkeypatch.setitem(api_providers.PROVIDERS, "gemini", slow_call)

    with pytest.raises(RuntimeError, match="모든 LLM 프로바이더가 실패"):
        _run()


def test_api_fallback_succeeds_within_timeout(monkeypatch):
    monkeypatch.setattr(claude_cli, "available", lambda: False)
    monkeypatch.setattr(engine, "_provider_timeout", lambda: 30)
    monkeypatch.setattr(api_providers, "available_providers", lambda: ["gemini"])

    async def fast_call(system_prompt, user_prompt):
        return "## PART 1 — 폴백 본문", "gemini-2.5-flash"

    monkeypatch.setitem(api_providers.PROVIDERS, "gemini", fast_call)

    result = _run()
    assert result.provider == "gemini"
    assert result.text == "## PART 1 — 폴백 본문"


# ===== 3. 엔진은 렌더하지 않는다 =====
def test_engine_returns_structured_without_rendering_markdown(monkeypatch, cli_available):
    async def generate(system_prompt, user_prompt, **kw):
        return claude_cli.CliResult(
            text="{json}", structured=_structured_payload(), model_id="claude-sonnet-5",
        )

    monkeypatch.setattr(claude_cli, "generate", generate)

    result = _run()
    assert result.structured is not None
    assert result.text == ""  # 렌더도, CLI 원문 JSON 유출도 없다
    assert "PART 1" not in result.text


# ===== 4. 텍스트 폴백 프롬프트의 PART 7 게이팅 =====
@pytest.mark.parametrize("publish_note,expects_part7", [(False, False), (True, True)])
def test_text_prompt_part7_follows_publish_setting(
    monkeypatch, cli_available, publish_note, expects_part7
):
    monkeypatch.setattr(engine.settings, "publish_portfolio_note", publish_note, raising=False)
    seen: dict[str, str] = {}

    async def generate(system_prompt, user_prompt, *, json_schema=None, **kw):
        if json_schema is not None:
            return claude_cli.CliResult(text="", structured=None, model_id="m")
        seen["text_prompt"] = user_prompt
        return claude_cli.CliResult(text="## PART 1 — 본문", structured=None, model_id="m")

    monkeypatch.setattr(claude_cli, "generate", generate)

    _run(portfolio_text="60% equities")
    assert ('Include this as "PART 7' in seen["text_prompt"]) is expects_part7
    assert ("60% equities" in seen["text_prompt"]) is expects_part7


# ===== 4-b. 비공개 포트폴리오는 외부 API 폴백으로 나가지 않는다 =====
@pytest.mark.parametrize("publish_note", [False, True])
def test_api_fallback_prompt_never_contains_portfolio(monkeypatch, publish_note):
    """회귀: 설정과 무관하게 Gemini/OpenAI/Anthropic 프롬프트에는 포트폴리오가 없어야 한다.

    비공개 보유 내역은 로컬 claude_cli 경로에서만 쓴다 (제3자 서비스 전송 금지).
    """
    monkeypatch.setattr(engine.settings, "publish_portfolio_note", publish_note, raising=False)
    monkeypatch.setattr(claude_cli, "available", lambda: False)
    monkeypatch.setattr(engine, "_provider_timeout", lambda: 30)
    monkeypatch.setattr(api_providers, "available_providers", lambda: ["gemini"])
    seen: dict[str, str] = {}

    async def capture(system_prompt, user_prompt):
        seen["system"] = system_prompt
        seen["user"] = user_prompt
        return "## PART 1 — 폴백 본문", "gemini-2.5-flash"

    monkeypatch.setitem(api_providers.PROVIDERS, "gemini", capture)

    result = _run(portfolio_text="NVDA 40%, 현금 10%, 계좌번호 1234")
    assert result.provider == "gemini"
    assert "NVDA 40%" not in seen["user"]
    assert "1234" not in seen["user"]
    assert "USER PORTFOLIO" not in seen["user"]
    assert 'Include this as "PART 7' not in seen["user"]
    assert "NVDA 40%" not in seen["system"]
    # 뉴스·시세 데이터는 정상적으로 전달된다
    assert "NEWS" in seen["user"] and "MARKET" in seen["user"]


def test_claude_cli_path_still_receives_portfolio(monkeypatch, cli_available):
    """대비 확인: 로컬 CLI 경로에는 포트폴리오가 그대로 전달된다."""
    monkeypatch.setattr(engine.settings, "publish_portfolio_note", False, raising=False)
    seen: dict[str, str] = {}

    async def generate(system_prompt, user_prompt, *, json_schema=None, **kw):
        if json_schema is not None:
            seen["structured"] = user_prompt
            return claude_cli.CliResult(
                text="", structured=_structured_payload(), model_id="claude-sonnet-5",
            )
        return claude_cli.CliResult(text="", structured=None, model_id="m")

    monkeypatch.setattr(claude_cli, "generate", generate)

    _run(portfolio_text="NVDA 40%, 현금 10%")
    assert "NVDA 40%" in seen["structured"]


# ===== 5. 번역 패스는 유한한 데드라인을 갖는다 =====
def test_translate_uses_single_attempt_with_explicit_timeout(monkeypatch):
    """보조 단계인 번역이 재시도로 파이프라인 전체를 지연시키지 않도록 한다."""
    seen: dict = {}

    async def fake_generate(system_prompt, user_prompt, **kw):
        seen.update(kw)
        return claude_cli.CliResult(
            text="", structured=_structured_payload(), model_id="claude-haiku",
        )

    monkeypatch.setattr(claude_cli, "available", lambda: True)
    monkeypatch.setattr(claude_cli, "generate", fake_generate)

    source = StructuredBriefing.model_validate(_structured_payload())
    result = asyncio.run(translate.translate_structured(source, "ko", "en"))

    assert result is not None
    assert seen["retries"] == 0
    assert seen["timeout_sec"] == int(engine.settings.claude_cli_timeout_sec)


def test_translate_returns_none_when_cli_fails(monkeypatch):
    async def failing(system_prompt, user_prompt, **kw):
        raise claude_cli.ClaudeCliError("CLI 응답 타임아웃(720s)")

    monkeypatch.setattr(claude_cli, "available", lambda: True)
    monkeypatch.setattr(claude_cli, "generate", failing)

    source = StructuredBriefing.model_validate(_structured_payload())
    assert asyncio.run(translate.translate_structured(source, "ko", "en")) is None


# ===== 6. 타임아웃 상·하한 =====
def test_provider_timeout_is_clamped(monkeypatch):
    monkeypatch.setattr(engine.settings, "llm_timeout_sec", 1, raising=False)
    assert engine._provider_timeout() == engine._MIN_PROVIDER_TIMEOUT
    monkeypatch.setattr(engine.settings, "llm_timeout_sec", 999_999, raising=False)
    assert engine._provider_timeout() == engine._MAX_PROVIDER_TIMEOUT


def test_validate_structured_rejects_garbage():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        engine.validate_structured({"headline": "x"})


def test_structured_schema_matches_model_fields():
    assert set(prompts.STRUCTURED_JSON_SCHEMA["properties"]) == set(
        StructuredBriefing.model_fields
    )


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Unencrypted Sensitive Data(제3자 전송 금지): 외부 API 폴백 프롬프트에 비공개 포트폴리오가
#     설정과 무관하게 포함되지 않는지, 반대로 로컬 CLI 경로에는 전달되는지 검증
#   - Improper Authorization: 텍스트 폴백 프롬프트가 publish_portfolio_note에 따라서만 PART 7을 요청하는지 검증
#   - Infinite Loop: API 폴백 타임아웃 상·하한이 적용되고 초과 시 다음 경로로 넘어가는지 검증
#   - Unsafe Deserialization: 스키마에 맞지 않는 LLM 출력이 거부되는지 검증
#   - Error Message Information Exposure: 전체 실패 시 조치 안내 메시지만 노출되는지 검증
# Not Applied:
#   - [WARN] 실제 `claude -p` 호출은 테스트하지 않는다 (구독 쿼터 소모·인증 상태 의존).
# --------------------------------------------------


# ===== 4. 로컬 CLI를 쓰지 못한 사실을 안내로 표면화한다 =====
def test_notice_when_cli_not_installed(monkeypatch):
    """CLI 미설치 → 설치 안내를 담고 API 폴백으로 진행한다 (login_required는 False)."""
    monkeypatch.setattr(claude_cli, "available", lambda: False)
    monkeypatch.setattr(claude_cli, "resolve_cli", lambda: None)
    monkeypatch.setattr(api_providers, "available_providers", lambda: ["openai"])

    async def fake_openai(system_prompt, user_prompt):
        return "# 폴백 본문", "gpt-4o"

    monkeypatch.setitem(api_providers.PROVIDERS, "openai", fake_openai)

    result = _run()
    assert result.provider == "openai"
    assert result.login_required is False
    assert result.notice == engine.CLI_MISSING_NOTICE


def test_notice_when_cli_not_logged_in(monkeypatch):
    """CLI는 있는데 인증 백오프 중 → 로그인 안내 + login_required=True."""
    monkeypatch.setattr(claude_cli, "available", lambda: False)
    monkeypatch.setattr(claude_cli, "resolve_cli", lambda: r"C:\fake\claude.exe")
    monkeypatch.setattr(api_providers, "available_providers", lambda: ["openai"])

    async def fake_openai(system_prompt, user_prompt):
        return "# 폴백 본문", "gpt-4o"

    monkeypatch.setitem(api_providers.PROVIDERS, "openai", fake_openai)

    result = _run()
    assert result.login_required is True
    assert "claude auth login" in result.notice


def test_auth_error_sets_login_notice_and_falls_back(monkeypatch):
    """구조화 호출이 401로 실패하면 로그인 안내를 남기고 첫 폴백 프로바이더로 넘어간다."""
    monkeypatch.setattr(claude_cli, "available", lambda: True)

    async def auth_fail(system_prompt, user_prompt, **kw):
        raise claude_cli.ClaudeCliAuthError("OAuth access token has been revoked")

    monkeypatch.setattr(claude_cli, "generate", auth_fail)
    monkeypatch.setattr(api_providers, "available_providers", lambda: ["openai", "gemini"])

    used: list[str] = []

    async def fake_openai(system_prompt, user_prompt):
        used.append("openai")
        return "# 폴백 본문", "gpt-4o"

    async def fake_gemini(system_prompt, user_prompt):
        used.append("gemini")
        return "# 제미나이", "gemini-2.5-flash"

    monkeypatch.setitem(api_providers.PROVIDERS, "openai", fake_openai)
    monkeypatch.setitem(api_providers.PROVIDERS, "gemini", fake_gemini)

    result = _run()
    assert used == ["openai"]          # 설정 순서대로 openai를 먼저 쓴다
    assert result.provider == "openai"
    assert result.login_required is True
    assert result.notice == engine.CLI_LOGIN_NOTICE


def test_successful_cli_run_has_no_notice(monkeypatch, cli_available):
    """정상 경로에서는 안내를 만들지 않는다 (불필요한 경고로 사용자를 피로하게 하지 않는다)."""
    async def ok(system_prompt, user_prompt, **kw):
        return claude_cli.CliResult(text="", structured=_structured_payload(), model_id="claude-sonnet-5")

    monkeypatch.setattr(claude_cli, "generate", ok)
    result = _run()
    assert result.notice == ""
    assert result.login_required is False


def test_fallback_order_follows_settings(monkeypatch):
    """LLM_FALLBACK_PROVIDERS 순서를 그대로 따른다 (기본 openai → gemini)."""
    from mbrief.config import settings

    monkeypatch.setattr(settings, "llm_fallback_providers", "openai,gemini")
    monkeypatch.setattr(settings, "openai_api_key", "k")
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    assert api_providers.available_providers() == ["openai", "gemini"]

    monkeypatch.setattr(settings, "openai_api_key", "")   # 키 없는 프로바이더는 자동 제외
    assert api_providers.available_providers() == ["gemini"]
