"""Claude Code CLI headless(-p) 프로바이더 — v2 브리핑 생성의 1순위 경로.

API 키 종량제가 아니라, 로컬에 로그인된 Claude 계정의 **구독 요금제 쿼터**를 사용한다.
  - 실행 방식 : Claude Code headless mode (print mode), `claude -p`
  - 인증 방식 : OAuth 구독 인증 (Pro/Max 플랜) — ANTHROPIC_API_KEY 불필요
  - 대비 개념 : Anthropic Messages API (API 키 + 크레딧 종량 과금)

전제: 터미널에서 `claude auth login`이 1회 완료되어 있어야 한다.

v1 services/claude_cli.py를 이식하고 다음을 추가했다:
  - --json-schema 구조화 출력 (structured_output 우선, result JSON 폴백)
  - --fallback-model (과부하 시 CLI 자체 폴백)
  - CliResult 데이터클래스 (본문 + 구조화 dict + 모델ID + 소요시간 + 토큰 사용량)
  - probe(): 실제 소형 호출로 인증 상태 진단 (doctor 명령)

절대 금지: `--bare`. keychain 읽기를 건너뛰어 OAuth 구독 인증이 깨진다. `--safe-mode`만 사용한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mbrief.config import settings

logger = logging.getLogger(__name__)

# [SECURE] 실행 파일 basename 화이트리스트 - OS Command Injection / 임의 바이너리 실행 방지 (Category 1)
_ALLOWED_CLI_NAMES = {"claude", "claude.exe", "claude.cmd", "claude.bat"}

# [SECURE] 모델 인자 화이트리스트 - argv 주입 방지 (Category 1: Improper Input for Security Decision)
# 운영 정책: Claude 호출의 최소 모델은 sonnet이다. haiku는 브리핑 품질(수치·실명 추출)이 떨어져
# 별칭 목록에서 제외한다. 설정에 남아 있어도 아래 resolve_model이 sonnet으로 강등한다.
_ALLOWED_MODEL_ALIASES = {"sonnet", "opus", "fable"}
_MODEL_ID_PATTERN = re.compile(r"^claude-[a-z0-9][a-z0-9.\-]{0,63}$")
# 전체 모델 ID(claude-haiku-4-5 등)로 우회해서 최소 모델 정책을 깨뜨리지 못하게 한다.
_FORBIDDEN_MODEL_SUBSTRINGS = ("haiku",)
MIN_MODEL_NOTE = "Claude 호출의 최소 모델은 sonnet입니다 (haiku 사용 금지)"

# npm 전역 설치 시 셸 shim(claude.cmd) 대신 사용할 실제 바이너리 상대 경로.
# shim 경유(cmd.exe)는 개행/따옴표 포함 인자를 재파싱하며 깨뜨리므로 실제 exe를 우선한다.
_WIN_REAL_BINARY = Path("node_modules") / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"

# 중첩 실행(이 CLI를 Claude Code 세션 안에서 실행) 시 자식에게 상속되면
# 인증이 깨지는 세션 전용 환경변수. CLAUDECODE=1 일 때만 제거한다.
_NESTED_ENV_BLOCKLIST = (
    "CLAUDECODE",
    "ANTHROPIC_BASE_URL",
)
_NESTED_ENV_PREFIXES = (
    "CLAUDE_CODE_",
    "CLAUDE_AGENT_",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_PREVIEW",
)

# 중첩 실행 여부와 무관하게 항상 제거한다. 이 값이 남아 있으면 CLI가 구독 OAuth 대신
# API 키 종량 과금 경로를 타거나, 유효하지 않은 키로 인증 오류를 낸다.
_ALWAYS_SCRUBBED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)

_MAX_STDOUT_BYTES = 8 * 1024 * 1024  # [SECURE] 출력 상한 - 메모리 고갈(DoS) 방지
_MIN_TIMEOUT_SEC = 30
_MAX_TIMEOUT_SEC = 1800
_RETRY_BACKOFF_SEC = 5.0
_PROBE_TIMEOUT_SEC = 90

_resolved_path: str | None = None
_resolve_done: bool = False
_auth_blocked_until: float = 0.0  # 인증 실패 후 재시도 억제 시각 (self-healing)
_AUTH_BACKOFF_SEC = 600

LOGIN_HINT = "로그인 필요: 터미널에서 `claude auth login` 실행"


class ClaudeCliError(RuntimeError):
    """CLI 실행/파싱 실패."""


class ClaudeCliAuthError(ClaudeCliError):
    """로그인이 안 되어 있거나 토큰이 만료·폐기됨."""


@dataclass
class CliResult:
    """`claude -p` 1회 호출 결과."""
    text: str = ""
    structured: dict | None = None
    model_id: str = ""
    duration_ms: int = 0
    usage: dict = field(default_factory=dict)


# ===== 경로 해석 =====
def _validate_cli_path(candidate: str) -> str | None:
    """[SECURE] 경로·파일명 검증 후 절대경로 반환. 부적합하면 None (Category 1: Path Traversal)."""
    try:
        path = Path(candidate).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as e:
        logger.warning("[ClaudeCLI] 경로 확인 실패: %s", e)
        return None
    if not path.is_file():
        return None
    # [SECURE] basename 화이트리스트 검증 - 임의 실행 파일 지정 차단 (Category 1)
    if path.name.lower() not in _ALLOWED_CLI_NAMES:
        logger.warning("[ClaudeCLI] 허용되지 않은 실행 파일명: %s", path.name)
        return None
    return str(path)


def _prefer_real_binary(shim_path: str) -> str:
    """Windows npm shim(.cmd)이면 같은 트리의 실제 claude.exe를 우선 사용."""
    path = Path(shim_path)
    if path.suffix.lower() not in (".cmd", ".bat"):
        return shim_path
    real = path.parent / _WIN_REAL_BINARY
    if real.is_file():
        logger.info("[ClaudeCLI] shim 대신 실제 바이너리 사용: %s", real)
        return str(real)
    logger.warning("[ClaudeCLI] 실제 바이너리 미발견 - shim 경유(%s). 인자 파싱 이슈 가능.", path.name)
    return shim_path


def resolve_cli() -> str | None:
    """CLI 실행 경로 확인 (1회 캐시). 미설치·비활성 시 None."""
    global _resolved_path, _resolve_done
    if _resolve_done:
        return _resolved_path

    _resolve_done = True
    if not settings.claude_cli_enabled:
        logger.info("[ClaudeCLI] 비활성화됨 (CLAUDE_CLI_ENABLED=false)")
        _resolved_path = None
        return None

    if settings.claude_cli_path:
        validated = _validate_cli_path(settings.claude_cli_path)
        if validated is None:
            logger.warning("[ClaudeCLI] CLAUDE_CLI_PATH가 유효하지 않음 - 자동 탐색으로 대체")
        else:
            _resolved_path = _prefer_real_binary(validated)
            logger.info("[ClaudeCLI] 경로(설정): %s", _resolved_path)
            return _resolved_path

    found = shutil.which("claude")
    if not found:
        logger.info("[ClaudeCLI] PATH에서 claude를 찾지 못함 - CLI 프로바이더 건너뜀")
        _resolved_path = None
        return None

    validated = _validate_cli_path(found)
    _resolved_path = _prefer_real_binary(validated) if validated else None
    if _resolved_path:
        logger.info("[ClaudeCLI] 경로(자동): %s", _resolved_path)
    return _resolved_path


def reset_cache() -> None:
    """경로 해석 캐시 초기화 (테스트·설정 변경용)."""
    global _resolved_path, _resolve_done
    _resolved_path = None
    _resolve_done = False


def _is_allowed_model(candidate: str) -> bool:
    """화이트리스트 별칭이거나 claude-<id> 형식이면서, 금지 모델이 아닐 것."""
    if any(token in candidate for token in _FORBIDDEN_MODEL_SUBSTRINGS):
        return False
    return candidate in _ALLOWED_MODEL_ALIASES or bool(_MODEL_ID_PATTERN.match(candidate))


def resolve_model(model: str | None = None) -> str:
    """[SECURE] 모델 인자 화이트리스트 검증 - argv 주입 방지 (Category 1).

    허용되지 않은 값(오타·주입 시도·haiku 같은 금지 모델)은 'sonnet'으로 강등한다.
    """
    candidate = (model or settings.claude_cli_model or "sonnet").strip().lower()
    if _is_allowed_model(candidate):
        return candidate
    logger.warning(
        "[ClaudeCLI] 허용되지 않은 모델값(%r) - 'sonnet'으로 대체. %s",
        candidate[:40], MIN_MODEL_NOTE,
    )
    return "sonnet"


def _resolve_fallback_model(model: str | None) -> str:
    """--fallback-model 값 검증. 비었거나 부적합하면 빈 문자열(미사용)."""
    candidate = (model or "").strip().lower()
    if not candidate:
        return ""
    if _is_allowed_model(candidate):
        return candidate
    logger.warning(
        "[ClaudeCLI] 허용되지 않은 폴백 모델값(%r) - 미사용. %s",
        candidate[:40], MIN_MODEL_NOTE,
    )
    return ""


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """자식 프로세스 환경 구성.

    API 키 변수는 항상 제거하고(구독 OAuth 경로 보존), 중첩 실행일 때는 세션 전용 변수도 제거한다.
    """
    env = dict(os.environ if base is None else base)

    # [SECURE] API 키 자격증명을 자식에게 상속시키지 않는다 - 잘못된 자격증명 사용/과금 경로 전환 방지 (Category 2)
    scrubbed = [key for key in _ALWAYS_SCRUBBED_ENV if env.pop(key, None) is not None]
    if scrubbed:
        # [SECURE] 값이 아닌 변수 '이름'만 기록 - 자격증명 로그 노출 방지 (Category 2)
        logger.info("[ClaudeCLI] API 키 환경변수 제거 후 실행 (구독 OAuth 사용): %s", ", ".join(scrubbed))

    if env.get("CLAUDECODE") != "1":
        return env
    # [SECURE] 상위 세션 토큰/엔드포인트 상속 차단 - 잘못된 자격증명 사용 방지 (Category 2)
    for key in list(env):
        if key in _NESTED_ENV_BLOCKLIST or key.startswith(_NESTED_ENV_PREFIXES):
            env.pop(key, None)
    logger.info("[ClaudeCLI] 중첩 실행 감지 - 세션 전용 환경변수 제거 후 실행")
    return env


# 하위 호환 별칭 (v1 명칭)
_child_env = child_env


def is_auth_blocked() -> bool:
    """직전 인증 실패로 백오프 중인지 여부."""
    return time.monotonic() < _auth_blocked_until


def available() -> bool:
    """CLI 사용 가능 여부 (설치 + 인증 백오프 아님)."""
    return resolve_cli() is not None and not is_auth_blocked()


# 인증 실패로 단정할 수 있는 문구만 나열한다. "401"/"oauth" 같은 단독 부분 문자열은
# 본문 텍스트나 무관한 오류에도 흔히 등장해 정상 응답을 인증 오류로 오분류한다.
_AUTH_ERROR_PHRASES = (
    "oauth access token has been revoked",
    "not logged in",
    "please run /login",
    "authentication_error",
    "invalid api key",
)


def looks_like_auth_error(message: str, api_error_status: object = None) -> bool:
    """인증 실패 판별.

    [SECURE] api_error_status == 401 또는 명시적 인증 실패 문구에만 반응한다 - 오분류로 인한
    불필요한 600초 백오프 및 잘못된 조치 안내 방지 (Category 1: Improper Input for Security Decision).
    """
    if api_error_status == 401:
        return True
    low = (message or "").lower()
    return any(phrase in low for phrase in _AUTH_ERROR_PHRASES)


# CLI 사용법 오류 표식. 재시도해도 동일하게 실패하므로 즉시 중단한다.
_USAGE_ERROR_MARKERS = (
    "unknown option",
    "not found",
    "error: unrecognized",
)


def is_usage_error(message: str) -> bool:
    """CLI 사용법/인자 오류 여부. 참이면 재시도하지 않는다."""
    low = (message or "").lower()
    return any(marker in low for marker in _USAGE_ERROR_MARKERS)


# ===== 프로세스 종료 =====
async def _kill_tree_windows(pid: int) -> None:
    """Windows에서 자식 프로세스까지 함께 종료.

    claude 실행 파일은 하위 프로세스를 띄우므로 proc.kill()만으로는 손자 프로세스가
    고아로 남아 메모리를 점유한 채 계속 살아있다(v1 실측: 타임아웃 후 14분 생존).
    """
    try:
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/F", "/T", "/PID", str(pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(killer.wait(), timeout=10)
    except (OSError, asyncio.TimeoutError) as e:
        logger.warning("[ClaudeCLI] taskkill 실패(pid=%s): %s", pid, e)


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """[SECURE] 타임아웃 시 자식 프로세스 트리까지 정리 - 리소스 누수 방지 (Category 5)."""
    if proc.returncode is not None:
        return

    if sys.platform == "win32":
        await _kill_tree_windows(proc.pid)
    else:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        except OSError as e:
            logger.warning("[ClaudeCLI] 프로세스 종료 실패: %s", e)
            return

    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except (asyncio.TimeoutError, ProcessLookupError):
        logger.warning("[ClaudeCLI] 프로세스가 종료 신호에 응답하지 않음 (pid=%s)", proc.pid)


async def _terminate_shielded(proc: asyncio.subprocess.Process) -> None:
    """취소(CancelledError) 중에도 자식 프로세스 트리를 정리한다.

    [SECURE] asyncio.shield로 정리 작업 자체가 취소되지 않게 한다. 대기가 취소되더라도
    종료 작업은 백그라운드에서 끝까지 진행되므로 고아 프로세스가 남지 않는다 (Category 5).
    이 함수는 CancelledError를 상위로 던지지 않는다 — 호출부가 원래 예외/취소를 그대로 전파한다.
    """
    if proc.returncode is not None:
        return
    task = asyncio.ensure_future(_terminate(proc))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # [SECURE] 빈 except 없음 - 정리 진행 상황을 남기고 취소는 호출부가 재전파 (Category 4)
        logger.warning("[ClaudeCLI] 취소 중 프로세스 정리 - 백그라운드에서 계속 진행 (pid=%s)", proc.pid)
    except (OSError, RuntimeError) as e:
        logger.warning("[ClaudeCLI] 프로세스 정리 실패(pid=%s): %s", proc.pid, e)


# ===== argv 구성 · 결과 파싱 =====
def build_argv(
    cli_path: str,
    model: str,
    prompt_path: str,
    *,
    json_schema: dict | None = None,
    fallback_model: str = "",
) -> list[str]:
    """실행 argv 구성. 순수 함수 — 테스트가 직접 검증한다.

    `--bare`는 절대 넣지 않는다 (keychain 미조회로 OAuth 인증이 깨짐).
    """
    argv = [
        cli_path,
        "-p",                                   # headless(print) 모드
        "--model", model,
        "--output-format", "json",
        "--system-prompt-file", prompt_path,    # 기본 에이전트 프롬프트 대체
        "--safe-mode",                          # CLAUDE.md·스킬·플러그인·훅·MCP 비활성
        "--no-session-persistence",             # 세션 파일 미생성
        "--tools", "",                          # [SECURE] 모든 도구 비활성 - 파일/셸 부작용 차단 (Category 1)
    ]
    if json_schema is not None:
        argv += ["--json-schema", json.dumps(json_schema, ensure_ascii=False)]
    if fallback_model:
        argv += ["--fallback-model", fallback_model]
    return argv


def pick_main_model(model_usage: object) -> str:
    """실제 본문을 생성한 모델 ID 추출.

    CLI는 제목 생성 등 부수 작업에 별도 모델을 쓸 수 있어 modelUsage에 여러 키가 담긴다.
    첫 키를 고르면 CLI가 내부적으로 쓰는 소형 보조 모델이 잡히므로 출력 토큰이 가장 많은 항목을 택한다.
    (보조 모델 선택은 CLI 내부 동작이라 이 프로젝트의 최소 모델 정책이 적용되지 않는다.)
    """
    if not isinstance(model_usage, dict) or not model_usage:
        return ""

    def output_tokens(entry: object) -> int:
        if isinstance(entry, dict):
            value = entry.get("outputTokens")
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    return str(max(model_usage.items(), key=lambda kv: output_tokens(kv[1]))[0])


def parse_payload(payload: Any) -> CliResult:
    """`--output-format json` 응답 dict → CliResult.

    구조화 출력은 두 형태를 모두 처리한다:
      1) payload["structured_output"]가 dict이면 그대로 사용
      2) 아니면 payload["result"]를 json.loads 해서 dict가 나오면 사용
      3) 둘 다 아니면 텍스트로 취급
    """
    if not isinstance(payload, dict):
        raise ClaudeCliError("CLI 응답이 JSON 객체가 아닙니다.")

    message = str(payload.get("result") or "").strip()

    # subtype이 'success'여도 is_error=true인 경우가 있으므로 is_error를 기준으로 판정한다.
    if payload.get("is_error"):
        if looks_like_auth_error(message, payload.get("api_error_status")):
            raise ClaudeCliAuthError(message or "인증 실패")
        raise ClaudeCliError(message or "CLI가 오류를 반환했습니다.")

    structured: dict | None = None
    raw_structured = payload.get("structured_output")
    if isinstance(raw_structured, dict):
        structured = raw_structured
    elif message:
        try:
            candidate = json.loads(message)
            if isinstance(candidate, dict):
                structured = candidate
        except (json.JSONDecodeError, ValueError):
            structured = None  # 텍스트 응답 — 정상 경로

    if not message and structured is None:
        raise ClaudeCliError("CLI 응답 본문이 비어 있습니다.")

    duration = payload.get("duration_ms")
    usage = payload.get("usage")
    return CliResult(
        text=message,
        structured=structured,
        model_id=pick_main_model(payload.get("modelUsage")),
        duration_ms=int(duration) if isinstance(duration, (int, float)) else 0,
        usage=usage if isinstance(usage, dict) else {},
    )


def parse_stdout(stdout_text: str) -> CliResult:
    """stdout 문자열 → CliResult."""
    try:
        payload = json.loads(stdout_text)
    except json.JSONDecodeError as e:
        raise ClaudeCliError(f"CLI 응답 JSON 파싱 실패: {e}") from e
    return parse_payload(payload)


# ===== 실행 =====
async def _run_once(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str,
    timeout_sec: int,
    json_schema: dict | None,
    fallback_model: str,
) -> CliResult:
    """1회 실행. 실패 시 ClaudeCliError/ClaudeCliAuthError를 던진다."""
    cli_path = resolve_cli()
    if cli_path is None:
        raise ClaudeCliError("claude CLI가 설치되어 있지 않습니다.")

    # [SECURE] 시스템 프롬프트를 임시파일로 전달 - 개행/따옴표 포함 argv 파싱 붕괴 방지 (Category 1)
    # mkstemp는 0600 권한 + 예측 불가 파일명으로 TOCTOU를 회피한다 (Category 3)
    fd, prompt_path = tempfile.mkstemp(prefix="mbrief_sys_", suffix=".txt")
    proc: asyncio.subprocess.Process | None = None
    started = time.time()
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:  # [SECURE] context manager로 FD 확실히 해제 (Category 5)
            f.write(system_prompt)

        argv = build_argv(
            cli_path, model, prompt_path,
            json_schema=json_schema, fallback_model=fallback_model,
        )
        logger.info(
            "[ClaudeCLI] 호출 중... model=%s timeout=%ss structured=%s fallback=%s",
            model, timeout_sec, json_schema is not None, fallback_model or "-",
        )

        # [SECURE] shell=False + argv 리스트 실행 - OS Command Injection 방지 (Category 1)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tempfile.gettempdir(),  # 프로젝트 파일과 격리
            env=child_env(),
        )

        try:
            # [SECURE] 프롬프트는 stdin으로 전달 - argv 길이 제한 및 인자 주입 회피 (Category 1)
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(user_prompt.encode("utf-8")), timeout=timeout_sec
            )
        except asyncio.TimeoutError:
            await _terminate_shielded(proc)
            raise ClaudeCliError(f"CLI 응답 타임아웃({timeout_sec}s)") from None
        except asyncio.CancelledError:
            # [SECURE] 상위 취소 시에도 자식 트리를 정리한 뒤 취소를 그대로 전파 - 고아 프로세스 방지 (Category 5)
            await _terminate_shielded(proc)
            raise

        stderr_text = stderr_b.decode("utf-8", errors="replace").strip()
        if len(stdout_b) > _MAX_STDOUT_BYTES:  # [SECURE] 자원 고갈 방지
            raise ClaudeCliError(f"CLI 출력이 상한({_MAX_STDOUT_BYTES}B)을 초과했습니다.")
        stdout_text = stdout_b.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0 and not stdout_text:
            detail = stderr_text[:500] or f"exit={proc.returncode}"
            if looks_like_auth_error(detail):
                raise ClaudeCliAuthError(detail)
            raise ClaudeCliError(detail)

        if stderr_text:
            logger.debug("[ClaudeCLI] stderr: %s", stderr_text[:500])

        result = parse_stdout(stdout_text)
        if not result.model_id:
            result.model_id = f"claude-{model}"
        if not result.duration_ms:
            result.duration_ms = int((time.time() - started) * 1000)
        return result

    finally:
        if proc is not None:
            # [SECURE] 어떤 경로(성공/실패/취소)로 빠져나가도 자식 트리를 정리 - 리소스 누수 방지 (Category 5)
            await _terminate_shielded(proc)
        try:
            os.unlink(prompt_path)  # [SECURE] 임시파일 확실히 제거 - 정보 잔존 방지 (Category 2)
        except OSError as e:
            logger.warning("[ClaudeCLI] 임시파일 삭제 실패: %s", e)


async def generate(
    system_prompt: str,
    user_prompt: str,
    *,
    json_schema: dict | None = None,
    model: str | None = None,
    timeout_sec: int | None = None,
    fallback_model: str | None = None,
    retries: int | None = None,
) -> CliResult:
    """`claude -p`로 본문 생성.

    json_schema를 주면 --json-schema 구조화 출력을 요청하고 CliResult.structured를 채운다.
    인증 실패와 사용법 오류는 재시도하지 않는다. 그 외 실패는 retries(기본
    settings.claude_cli_retries)만큼 재시도한다. retries=0을 주면 1회만 시도한다.
    """
    global _auth_blocked_until

    if is_auth_blocked():
        raise ClaudeCliAuthError(f"직전 인증 실패로 백오프 중입니다. {LOGIN_HINT}")

    resolved_model = resolve_model(model)
    # [SECURE] 타임아웃 상·하한 고정 - 무한 대기 방지 (Category 3)
    raw_timeout = settings.claude_cli_timeout_sec if timeout_sec is None else timeout_sec
    timeout = max(_MIN_TIMEOUT_SEC, min(int(raw_timeout), _MAX_TIMEOUT_SEC))
    fallback = _resolve_fallback_model(
        settings.claude_cli_fallback_model if fallback_model is None else fallback_model
    )

    # [SECURE] 재시도 횟수 상한 고정 - 무한 루프 방지 (Category 3)
    raw_retries = settings.claude_cli_retries if retries is None else retries
    attempts = max(1, min(int(raw_retries) + 1, 5))
    last_error: ClaudeCliError | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await _run_once(
                system_prompt, user_prompt,
                model=resolved_model, timeout_sec=timeout,
                json_schema=json_schema, fallback_model=fallback,
            )
        except ClaudeCliAuthError:
            # 인증 실패는 즉시 회복되지 않으므로 일정 시간 재시도를 억제한다.
            _auth_blocked_until = time.monotonic() + _AUTH_BACKOFF_SEC
            logger.error("[ClaudeCLI] 인증 실패 - %s (%d초 동안 건너뜀)", LOGIN_HINT, _AUTH_BACKOFF_SEC)
            raise
        except ClaudeCliError as e:
            last_error = e
            # [SECURE] 상세 오류는 로컬 로그에만 기록 (Category 4)
            logger.warning("[ClaudeCLI] 시도 %d/%d 실패: %s", attempt, attempts, e)
            if is_usage_error(str(e)):
                # 인자·경로 오류는 재시도해도 결과가 같다. 쿼터와 시간을 낭비하지 않는다.
                logger.error("[ClaudeCLI] 사용법/실행 파일 오류로 판단 - 재시도하지 않음")
                break
            if attempt < attempts:
                await asyncio.sleep(_RETRY_BACKOFF_SEC)

    raise last_error or ClaudeCliError("CLI 호출에 실패했습니다.")


async def healthcheck() -> dict:
    """설치/버전 확인. 네트워크 호출 없음."""
    cli_path = resolve_cli()
    if cli_path is None:
        return {"installed": False, "path": None, "version": None, "authBlocked": False}

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            cli_path, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=child_env(),
        )
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        version = stdout_b.decode("utf-8", errors="replace").strip()[:80]
        return {"installed": True, "path": cli_path, "version": version, "authBlocked": is_auth_blocked()}
    except asyncio.TimeoutError:
        if proc is not None:
            await _terminate(proc)
        logger.warning("[ClaudeCLI] --version 타임아웃")
        return {"installed": True, "path": cli_path, "version": None, "authBlocked": is_auth_blocked()}
    except OSError as e:
        # [SECURE] 상세 오류는 로컬 로그로만 - 내부 경로 노출 방지 (Category 4)
        logger.warning("[ClaudeCLI] 버전 확인 실패: %s", e)
        return {"installed": False, "path": cli_path, "version": None, "authBlocked": is_auth_blocked()}


PROBE_PROMPT = "Reply with exactly: PONG"
PROBE_SYSTEM = "You are a connectivity probe. Reply with exactly one word and nothing else."


async def probe() -> dict:
    """실제 소형 호출 1회로 인증 상태 진단 (doctor 전용).

    Returns: {ok, authBlocked, needsLogin, message, durationMs}
    전역 인증 백오프를 설정하지 않는다 (진단이 이후 실행을 막지 않도록).
    """
    started = time.time()
    result = {
        "ok": False,
        "authBlocked": is_auth_blocked(),
        "needsLogin": False,
        "message": "",
        "durationMs": 0,
    }

    if resolve_cli() is None:
        result["message"] = "claude CLI가 설치되어 있지 않습니다."
        return result

    try:
        cli = await _run_once(
            # 최소 모델 정책(sonnet 이상)을 진단 호출에도 그대로 적용한다.
            PROBE_SYSTEM, PROBE_PROMPT,
            model="sonnet", timeout_sec=_PROBE_TIMEOUT_SEC,
            json_schema=None, fallback_model="",
        )
        result["ok"] = True
        result["message"] = (cli.text or "").strip()[:80] or "(빈 응답)"
    except ClaudeCliAuthError as e:
        result["needsLogin"] = True
        result["message"] = LOGIN_HINT
        logger.warning("[ClaudeCLI] probe 인증 실패: %s", str(e)[:200])
    except ClaudeCliError as e:
        detail = str(e)
        if looks_like_auth_error(detail):
            result["needsLogin"] = True
            result["message"] = LOGIN_HINT
        else:
            # [SECURE] 사용자 표시 메시지는 요약만, 상세는 로그로 (Category 4)
            result["message"] = detail[:200]
        logger.warning("[ClaudeCLI] probe 실패: %s", detail[:200])
    except OSError as e:
        result["message"] = "CLI 실행에 실패했습니다."
        logger.warning("[ClaudeCLI] probe 실행 오류: %s", e)

    result["durationMs"] = int((time.time() - started) * 1000)
    return result


# --------------------------------------------------
# Security Checklist
# Applied:
#   - OS Command Injection: shell=False + argv 리스트 실행, 프롬프트는 stdin 전달, 실행 파일 basename 화이트리스트
#   - Improper Input for Security Decision: 모델/폴백모델 인자를 화이트리스트+정규식으로 검증 후 argv 구성.
#     금지 토큰(haiku)은 별칭이든 전체 모델 ID든 거부하고 sonnet으로 강등한다 (최소 모델 정책)
#   - Path Traversal: CLI 경로 resolve(strict=True) + is_file() + 파일명 화이트리스트 검증
#   - Race Condition (TOCTOU): tempfile.mkstemp(0600)로 예측 불가 임시파일 생성 후 fd로 직접 기록
#   - Infinite Loop: 실행 타임아웃 상·하한 고정(30~1800s), 재시도 횟수 상한(최대 5회), 초과 시 프로세스 트리 kill.
#     사용법 오류(unknown option / not found / unrecognized)는 재시도하지 않고 즉시 중단
#   - Improper Resource Release: finally + CancelledError 경로 모두에서 asyncio.shield로 자식 트리 종료를
#     보장하고 임시파일을 삭제한다. FD는 context manager로 해제
#   - Improper Input for Security Decision: 인증 오류 판별을 api_error_status==401 또는 명시적 문구로 한정해
#     "401"/"oauth" 부분 문자열 오분류를 제거
#   - Hard-coded Credentials / 자격증명 격리: ANTHROPIC_API_KEY·ANTHROPIC_AUTH_TOKEN을 항상 자식 환경에서
#     제거하고, 제거 사실은 변수 '이름'만 로깅한다
#   - Error Message Information Exposure: 상세 stderr/경로는 로컬 로그에만 기록, 상위에는 요약 전달
#   - Improper Exception Handling: 빈 except 없음, 모든 예외 경로에서 로깅 또는 재전파
#   - Hard-coded Credentials: 없음 - 인증은 로컬 OAuth 세션(keychain)에 위임
#   - Unencrypted Sensitive Data: 자격증명을 코드/로그/argv에 기록하지 않음
#   - 세션 자격증명 격리: CLAUDECODE=1 중첩 실행 시 CLAUDE_CODE_*/CLAUDE_AGENT_*/CLAUDE_PID/
#     CLAUDE_EFFORT/CLAUDE_PREVIEW/ANTHROPIC_BASE_URL 제거 후 실행
#   - 자원 고갈 방지: stdout 8MB 상한 검사
#   - Unsafe Deserialization: CLI 응답은 json.loads 후 dict 여부를 검사하고 pydantic 계층에서 재검증
# Not Applied:
#   - [WARN] Code Download Without Integrity Check: claude 실행 파일의 서명·해시를 검증하지 않는다.
#     신뢰된 경로(npm 전역 설치)만 사용한다는 전제이며, 강화하려면 해시 핀닝이 필요하다.
#   - [WARN] --bare 미사용은 인증 유지를 위한 의도적 선택이다. 즉 CLI는 keychain을 읽으며,
#     로컬 사용자 계정이 탈취되면 구독 자격증명도 함께 노출된다 (OS 계정 보안에 의존).
# --------------------------------------------------
