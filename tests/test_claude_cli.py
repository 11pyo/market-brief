"""claude CLI 프로바이더 테스트 — 실제 `claude -p` 호출은 절대 하지 않는다.

argv 구성, 환경변수 스크럽, 결과 파싱(구조화/텍스트/401), 모델 화이트리스트,
재시도 정책, 프로세스 정리(취소 안전성)를 검증한다.
프로세스 정리 테스트만 로컬 python 인터프리터를 자식으로 띄운다 (네트워크 없음).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys

import pytest

from mbrief.llm import claude_cli
from mbrief.llm.prompts import STRUCTURED_JSON_SCHEMA

CLI = r"C:\fake\claude.exe"
PROMPT_FILE = r"C:\fake\tmp\sys.txt"


# ===== argv 구성 =====
def test_build_argv_core_flags():
    argv = claude_cli.build_argv(CLI, "sonnet", PROMPT_FILE)
    assert argv[0] == CLI
    assert argv[1] == "-p"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--system-prompt-file") + 1] == PROMPT_FILE
    assert "--safe-mode" in argv
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--tools") + 1] == ""


def test_build_argv_never_uses_bare():
    """--bare는 keychain을 건너뛰어 OAuth 구독 인증을 깨뜨린다. 절대 포함되면 안 된다."""
    argv = claude_cli.build_argv(CLI, "opus", PROMPT_FILE,
                                 json_schema=STRUCTURED_JSON_SCHEMA, fallback_model="opus")
    assert "--bare" not in argv


def test_build_argv_without_schema_has_no_json_schema_flag():
    argv = claude_cli.build_argv(CLI, "sonnet", PROMPT_FILE)
    assert "--json-schema" not in argv
    assert "--fallback-model" not in argv


def test_build_argv_with_schema_serializes_json():
    argv = claude_cli.build_argv(CLI, "sonnet", PROMPT_FILE, json_schema=STRUCTURED_JSON_SCHEMA)
    payload = argv[argv.index("--json-schema") + 1]
    parsed = json.loads(payload)
    assert parsed["type"] == "object"
    assert "headline" in parsed["properties"]


def test_build_argv_with_fallback_model():
    argv = claude_cli.build_argv(CLI, "sonnet", PROMPT_FILE, fallback_model="opus")
    assert argv[argv.index("--fallback-model") + 1] == "opus"


# ===== 환경변수 스크럽 =====
def test_child_env_scrubs_nested_session_vars():
    base = {
        "CLAUDECODE": "1",
        "ANTHROPIC_BASE_URL": "https://internal.example",
        "CLAUDE_CODE_SSE_PORT": "1234",
        "CLAUDE_AGENT_ID": "abc",
        "CLAUDE_PID": "999",
        "CLAUDE_EFFORT": "high",
        "CLAUDE_PREVIEW_FLAG": "on",
        "PATH": "C:/bin",
        "HOME": "C:/Users/x",
    }
    env = claude_cli.child_env(base)
    for removed in ("CLAUDECODE", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_SSE_PORT",
                    "CLAUDE_AGENT_ID", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_PREVIEW_FLAG"):
        assert removed not in env, f"{removed}가 자식 환경에 남아 인증을 깨뜨린다"
    assert env["PATH"] == "C:/bin"
    assert env["HOME"] == "C:/Users/x"


def test_child_env_keeps_session_vars_outside_nested_session():
    base = {"CLAUDE_CODE_SSE_PORT": "1234", "ANTHROPIC_BASE_URL": "x", "PATH": "C:/bin"}
    env = claude_cli.child_env(base)
    assert env == base  # CLAUDECODE != "1" 이면 세션 전용 변수는 제거하지 않는다


def test_child_env_always_scrubs_api_keys():
    """중첩 실행이 아니어도 API 키는 항상 제거한다 (구독 OAuth 경로 보존)."""
    base = {
        "ANTHROPIC_API_KEY": "sk-ant-should-not-leak",
        "ANTHROPIC_AUTH_TOKEN": "should-not-leak",
        "PATH": "C:/bin",
    }
    env = claude_cli.child_env(base)
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["PATH"] == "C:/bin"
    assert base["ANTHROPIC_API_KEY"] == "sk-ant-should-not-leak"  # 입력 dict는 변형하지 않는다


def test_child_env_scrubs_api_keys_when_nested_too():
    base = {"CLAUDECODE": "1", "ANTHROPIC_API_KEY": "sk-ant-x", "PATH": "C:/bin"}
    env = claude_cli.child_env(base)
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDECODE" not in env


def test_child_env_logs_only_key_names(caplog):
    """[SECURE] 제거된 자격증명의 '값'은 로그에 남기지 않는다."""
    import logging

    with caplog.at_level(logging.INFO, logger="mbrief.llm.claude_cli"):
        claude_cli.child_env({"ANTHROPIC_API_KEY": "sk-ant-secret-value"})
    assert "ANTHROPIC_API_KEY" in caplog.text
    assert "sk-ant-secret-value" not in caplog.text


def test_child_env_does_not_mutate_input():
    base = {"CLAUDECODE": "1", "CLAUDE_PID": "1"}
    claude_cli.child_env(base)
    assert base == {"CLAUDECODE": "1", "CLAUDE_PID": "1"}


# ===== 결과 파싱 =====
def _payload(**kw) -> dict:
    base = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "본문",
        "duration_ms": 4321,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "modelUsage": {
            "claude-haiku-4-5": {"inputTokens": 5, "outputTokens": 3},
            "claude-sonnet-4-5": {"inputTokens": 100, "outputTokens": 900},
        },
    }
    base.update(kw)
    return base


def test_parse_structured_output_key():
    structured = {"headline": "h", "overnight": "o"}
    result = claude_cli.parse_payload(_payload(result="", structured_output=structured))
    assert result.structured == structured
    assert result.model_id == "claude-sonnet-4-5"  # 출력 토큰이 가장 많은 모델
    assert result.duration_ms == 4321
    assert result.usage["output_tokens"] == 20


def test_parse_structured_from_result_json_string():
    structured = {"headline": "h"}
    result = claude_cli.parse_payload(_payload(result=json.dumps(structured)))
    assert result.structured == structured


def test_parse_plain_text_leaves_structured_none():
    result = claude_cli.parse_payload(_payload(result="## PART 1 — 어젯밤"))
    assert result.structured is None
    assert result.text.startswith("## PART 1")


def test_parse_non_dict_json_result_is_text():
    result = claude_cli.parse_payload(_payload(result="[1, 2, 3]"))
    assert result.structured is None
    assert result.text == "[1, 2, 3]"


def test_parse_401_raises_auth_error():
    payload = {
        "type": "result",
        "is_error": True,
        "api_error_status": 401,
        "result": "Failed to authenticate. API Error: 401 OAuth access token has been revoked.",
    }
    with pytest.raises(claude_cli.ClaudeCliAuthError):
        claude_cli.parse_payload(payload)


def test_parse_revoked_phrase_without_status_raises_auth_error():
    payload = {"is_error": True, "result": "OAuth access token has been revoked"}
    with pytest.raises(claude_cli.ClaudeCliAuthError):
        claude_cli.parse_payload(payload)


def test_parse_not_logged_in_raises_auth_error():
    payload = {"is_error": True, "result": "Not logged in. Run claude auth login."}
    with pytest.raises(claude_cli.ClaudeCliAuthError):
        claude_cli.parse_payload(payload)


def test_parse_generic_error_is_not_auth_error():
    payload = {"is_error": True, "result": "Model overloaded, try again"}
    with pytest.raises(claude_cli.ClaudeCliError) as exc:
        claude_cli.parse_payload(payload)
    assert not isinstance(exc.value, claude_cli.ClaudeCliAuthError)


def test_parse_empty_response_raises():
    with pytest.raises(claude_cli.ClaudeCliError):
        claude_cli.parse_payload({"is_error": False, "result": ""})


def test_parse_non_object_raises():
    with pytest.raises(claude_cli.ClaudeCliError):
        claude_cli.parse_payload(["not", "an", "object"])


def test_parse_stdout_bad_json_raises():
    with pytest.raises(claude_cli.ClaudeCliError):
        claude_cli.parse_stdout("not json at all")


def test_looks_like_auth_error_phrases():
    for phrase in ("OAuth access token has been revoked", "Not logged in",
                   "please run /login", "Invalid API key", "authentication_error"):
        assert claude_cli.looks_like_auth_error(phrase)


def test_looks_like_auth_error_uses_api_error_status():
    assert claude_cli.looks_like_auth_error("", 401)
    assert claude_cli.looks_like_auth_error("무관한 본문", 401)
    assert not claude_cli.looks_like_auth_error("무관한 본문", 500)


def test_looks_like_auth_error_ignores_bare_substrings():
    """'401'/'oauth' 부분 문자열만으로는 인증 오류로 판정하지 않는다 (오분류 → 불필요한 백오프 방지)."""
    for phrase in ("rate limit exceeded", "401 unauthorized", "oauth flow documentation",
                   "HTTP 401 while fetching quote", "please authenticate later"):
        assert not claude_cli.looks_like_auth_error(phrase)


# ===== 프로세스 정리 (취소 안전성) =====
def _spawn_sleeper():
    """30초 자는 자식 프로세스. 정리 로직이 실제로 죽이는지 확인하는 데 쓴다."""
    return asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


def test_terminate_shielded_kills_child_process():
    async def scenario():
        proc = await _spawn_sleeper()
        assert proc.returncode is None
        await claude_cli._terminate_shielded(proc)
        return proc.returncode

    assert asyncio.run(scenario()) is not None


def test_terminate_shielded_still_kills_child_when_cancelled():
    """[SECURE] 상위가 취소되어도 자식 프로세스가 고아로 남지 않는다 (Category 5)."""
    async def scenario():
        proc = await _spawn_sleeper()
        task = asyncio.ensure_future(claude_cli._terminate_shielded(proc))
        await asyncio.sleep(0)      # 정리 작업을 시작시킨 뒤
        task.cancel()               # 대기를 취소한다
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # shield 덕분에 종료 작업 자체는 계속 진행되어야 한다
        await asyncio.wait_for(proc.wait(), timeout=20)
        return proc.returncode

    assert asyncio.run(scenario()) is not None


def test_terminate_shielded_is_noop_for_finished_process():
    async def scenario():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "pass",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        await claude_cli._terminate_shielded(proc)  # 예외 없이 즉시 반환
        return proc.returncode

    assert asyncio.run(scenario()) == 0


def test_generate_accepts_retries_override(monkeypatch):
    """translate 패스처럼 1회만 시도해야 하는 호출자를 위한 retries=0 경로."""
    attempts: list[int] = []

    async def failing_run_once(*args, **kwargs):
        attempts.append(1)
        raise claude_cli.ClaudeCliError("CLI 응답 타임아웃(720s)")

    monkeypatch.setattr(claude_cli, "_run_once", failing_run_once)
    monkeypatch.setattr(claude_cli, "_auth_blocked_until", 0.0, raising=False)
    monkeypatch.setattr(claude_cli, "resolve_cli", lambda: "C:/bin/claude.exe")

    with pytest.raises(claude_cli.ClaudeCliError):
        asyncio.run(claude_cli.generate("sys", "user", retries=0))
    assert len(attempts) == 1


def test_generate_does_not_retry_usage_errors(monkeypatch):
    attempts: list[int] = []

    async def failing_run_once(*args, **kwargs):
        attempts.append(1)
        raise claude_cli.ClaudeCliError("error: unknown option '--bogus'")

    monkeypatch.setattr(claude_cli, "_run_once", failing_run_once)
    monkeypatch.setattr(claude_cli, "_auth_blocked_until", 0.0, raising=False)
    monkeypatch.setattr(claude_cli, "resolve_cli", lambda: "C:/bin/claude.exe")
    monkeypatch.setattr(claude_cli, "_RETRY_BACKOFF_SEC", 0.0)

    with pytest.raises(claude_cli.ClaudeCliError):
        asyncio.run(claude_cli.generate("sys", "user", retries=4))
    assert len(attempts) == 1  # 재시도해도 결과가 같으므로 즉시 중단


def test_generate_retries_transient_errors(monkeypatch):
    attempts: list[int] = []

    async def flaky_run_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise claude_cli.ClaudeCliError("CLI 응답 본문이 비어 있습니다.")
        return claude_cli.CliResult(text="ok", model_id="claude-sonnet-5")

    monkeypatch.setattr(claude_cli, "_run_once", flaky_run_once)
    monkeypatch.setattr(claude_cli, "_auth_blocked_until", 0.0, raising=False)
    monkeypatch.setattr(claude_cli, "resolve_cli", lambda: "C:/bin/claude.exe")
    monkeypatch.setattr(claude_cli, "_RETRY_BACKOFF_SEC", 0.0)

    result = asyncio.run(claude_cli.generate("sys", "user", retries=2))
    assert result.text == "ok"
    assert len(attempts) == 2


def test_is_usage_error_detects_argument_faults():
    for phrase in ("error: unknown option '--bogus'", "claude: command not found",
                   "error: unrecognized arguments"):
        assert claude_cli.is_usage_error(phrase)
    for phrase in ("CLI 응답 타임아웃(720s)", "overloaded_error", "CLI 응답 본문이 비어 있습니다."):
        assert not claude_cli.is_usage_error(phrase)


# ===== 모델 화이트리스트 =====
@pytest.mark.parametrize("value", ["sonnet", "opus", "fable", "claude-sonnet-4-5"])
def test_resolve_model_allows_whitelisted(value):
    assert claude_cli.resolve_model(value) == value


@pytest.mark.parametrize("value", [
    "gpt-4o",
    "sonnet; rm -rf /",
    "--dangerously-skip-permissions",
    "../../bin/sh",
    "claude-" + "x" * 200,
])
def test_resolve_model_rejects_unlisted(value):
    assert claude_cli.resolve_model(value) == "sonnet"


# ===== 최소 모델 정책: haiku 금지 =====
@pytest.mark.parametrize("value", ["haiku", "HAIKU", " haiku ", "claude-haiku-4-5"])
def test_resolve_model_downgrades_haiku_to_sonnet(value):
    """소유자 정책: Claude 호출은 sonnet 이상만 쓴다. 별칭이든 전체 모델 ID든 강등한다."""
    assert claude_cli.resolve_model(value) == "sonnet"


def test_haiku_is_not_an_allowed_alias():
    assert "haiku" not in claude_cli._ALLOWED_MODEL_ALIASES
    assert {"sonnet", "opus"} <= claude_cli._ALLOWED_MODEL_ALIASES


def test_resolve_model_warns_with_minimum_model_note(caplog):
    with caplog.at_level("WARNING"):
        claude_cli.resolve_model("haiku")
    assert "sonnet" in caplog.text
    assert claude_cli.MIN_MODEL_NOTE in caplog.text


def test_resolve_fallback_model_rejects_unlisted():
    assert claude_cli._resolve_fallback_model("evil --flag") == ""
    assert claude_cli._resolve_fallback_model("") == ""
    assert claude_cli._resolve_fallback_model("opus") == "opus"


@pytest.mark.parametrize("value", ["haiku", "claude-haiku-4-5"])
def test_resolve_fallback_model_rejects_haiku(value):
    assert claude_cli._resolve_fallback_model(value) == ""


# ===== 경로 화이트리스트 =====
def test_validate_cli_path_rejects_wrong_basename(tmp_path):
    fake = tmp_path / "evil.exe"
    fake.write_text("", encoding="utf-8")
    assert claude_cli._validate_cli_path(str(fake)) is None


def test_validate_cli_path_rejects_missing(tmp_path):
    assert claude_cli._validate_cli_path(str(tmp_path / "claude.exe")) is None


def test_validate_cli_path_accepts_allowed_name(tmp_path):
    good = tmp_path / "claude.exe"
    good.write_text("", encoding="utf-8")
    assert claude_cli._validate_cli_path(str(good)) == str(good.resolve())


# --------------------------------------------------
# Security Checklist
# Applied:
#   - OS Command Injection: argv에 --bare 미포함 및 모델 인자 화이트리스트 거부를 회귀 테스트로 고정
#   - Improper Input for Security Decision: 허용되지 않은 모델/폴백 모델 값이 안전 기본값으로 대체되는지 검증
#   - Path Traversal: 실행 파일 basename 화이트리스트 동작 검증
#   - 세션 자격증명 격리: CLAUDECODE=1 중첩 실행 시 세션 전용 환경변수 제거 검증.
#     ANTHROPIC_API_KEY/AUTH_TOKEN은 중첩 여부와 무관하게 항상 제거되고, 로그에는 '이름'만 남는지 검증
#   - Unsafe Deserialization: 비-dict/손상 JSON 응답이 예외로 처리되는지 검증
#   - Improper Input for Security Decision: 인증 오류 판별이 api_error_status==401 또는 명시 문구에만
#     반응하고 "401"/"oauth" 부분 문자열에는 오반응하지 않는지 검증
#   - Improper Resource Release: 취소(CancelledError) 중에도 자식 프로세스가 종료되는지 검증
#   - Infinite Loop: 사용법 오류는 재시도하지 않고, retries=0이면 정확히 1회만 시도하는지 검증
# Not Applied:
#   - [WARN] 실제 `claude -p` 호출은 테스트하지 않는다 (계정 쿼터 소모 및 인증 상태 의존).
#     구독 인증 경로는 `python -m mbrief doctor`의 probe로만 수동 확인한다.
# --------------------------------------------------
