"""명령행 인터페이스 — `python -m mbrief <command>`.

명령: refresh | score | generate | build | publish | notify | daily | show | preview | doctor
      install-task | migrate

실패 시 0이 아닌 종료 코드를 반환한다 (Windows 작업 스케줄러가 실패를 인지할 수 있도록).
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import logging.handlers
import platform
import re
import subprocess
import sys
from functools import partial
from pathlib import Path

from mbrief import paths, pipeline, scorecard, store
from mbrief.config import settings

logger = logging.getLogger("mbrief")

TASK_NAME = "MarketBrief Daily"
_LOG_MAX_BYTES = 2 * 1024 * 1024
_LOG_BACKUPS = 5
_PREVIEW_MIN_PORT = 1024
_PREVIEW_MAX_PORT = 65535
_SUBPROCESS_TIMEOUT = 30
# [SECURE] 언어 코드 화이트리스트 정규식 - 임의 값 전달 방지 (Category 1)
_LANG_ARG_RE = re.compile(r"^[a-z]{2}$")

EXIT_OK = 0
EXIT_FAILED = 1


# ===== 로깅 =====
def setup_logging(name: str = "mbrief", level: int = logging.INFO) -> logging.Logger:
    """콘솔(UTF-8) + 회전 파일 로그 설정. 여러 번 호출해도 핸들러가 중복되지 않는다."""
    root = logging.getLogger()
    root.setLevel(level)

    # Windows 콘솔 기본 코드페이지(cp949)에서 한글·이모지 출력이 깨지지 않도록 UTF-8로 재설정
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError) as e:
                # [SECURE] 빈 except 없음 - 실패해도 로깅은 계속 (Category 4)
                print(f"[warn] 콘솔 인코딩 설정 실패: {e}", file=sys.stderr)

    if not any(getattr(h, "_mbrief", False) for h in root.handlers):
        fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")

        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        console._mbrief = True  # type: ignore[attr-defined]
        root.addHandler(console)

        try:
            paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                paths.LOG_DIR / "mbrief.log",
                maxBytes=_LOG_MAX_BYTES,
                backupCount=_LOG_BACKUPS,
                encoding="utf-8",
            )
            file_handler.setFormatter(fmt)
            file_handler._mbrief = True  # type: ignore[attr-defined]
            root.addHandler(file_handler)
        except OSError as e:
            print(f"[warn] 파일 로그를 사용할 수 없습니다: {e}", file=sys.stderr)

    # [SECURE] httpx INFO 로그는 요청 URL을 남긴다. 쿼리스트링 자격증명 노출을 원천 차단 (Category 2)
    for noisy in ("httpx", "httpcore", "google_genai", "google.genai", "openai", "anthropic",
                  "yfinance", "peewee", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger(name)


# ===== 명령 구현 =====
def cmd_refresh(args: argparse.Namespace) -> int:
    bundle = asyncio.run(pipeline.refresh(offline=bool(getattr(args, "offline", False))))
    print(f"시세 {len(bundle.quotes)}건 · 섹터 {len(bundle.sectors)}건 · 차트 {len(bundle.chartKeys)}개 갱신")
    print(f"저장: {paths.MARKET_JSON}")
    return EXIT_OK


def cmd_score(_args: argparse.Namespace) -> int:
    sc = scorecard.run()
    hit = f"{sc.hitRate:.1%}" if sc.hitRate is not None else "-"
    brier = f"{sc.avgBrier:.4f}" if sc.avgBrier is not None else "-"
    print(f"스코어카드 {len(sc.entries)}건 | 적중률 {hit} | 평균 Brier {brier} (표본 {sc.sampleSize})")
    return EXIT_OK


def _parse_langs(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    langs = []
    for part in raw.split(","):
        code = part.strip().lower()
        if _LANG_ARG_RE.match(code):
            langs.append(code)
        elif code:
            print(f"[warn] 언어 코드 형식 오류 무시: {code[:10]}", file=sys.stderr)
    return langs or None


def _print_notices() -> None:
    """파이프라인이 남긴 사용자 안내를 눈에 띄게 출력한다.

    로컬 Claude CLI 미로그인처럼 '실행은 됐지만 사용자가 조치해야 하는' 사실을
    로그 파일에만 묻어 두지 않기 위한 출력 경로다.
    """
    for message in pipeline.take_notices():
        print("")
        print("!" * 70)
        print(f"! {message}")
        print("!" * 70, flush=True)


def cmd_generate(args: argparse.Namespace) -> int:
    records = asyncio.run(pipeline.generate(
        _parse_langs(args.lang),
        mock=args.mock,
        offline=args.offline,
        model=args.model,
        force=bool(getattr(args, "force", False)),
    ))
    if not records:
        _print_notices()
        print("생성된 브리핑이 없습니다.", file=sys.stderr)
        return EXIT_FAILED
    for record in records:
        marker = " (번역)" if record.translatedFrom else ""
        print(f"{record.id} [{record.lang}]{marker} · {record.provider}/{record.model}")
        print(f"  {record.headline}")
    _print_notices()
    return EXIT_OK


def cmd_build(_args: argparse.Namespace) -> int:
    # 빌드 동작은 pipeline.build_site() 한 곳에만 둔다 (지연 임포트·예외 처리·로깅 포함).
    # CLI는 결과를 종료 코드로 옮기는 얇은 래퍼다.
    if not pipeline.build_site():
        print(
            f"사이트 빌드에 실패했습니다. 자세한 내용은 {paths.LOG_DIR / 'mbrief.log'} 참고",
            file=sys.stderr,
        )
        return EXIT_FAILED
    print(f"빌드 완료: {paths.SITE_DIR}")
    return EXIT_OK


def cmd_publish(args: argparse.Namespace) -> int:
    from mbrief import publish as publish_mod
    ok = publish_mod.publish_site(dry_run=args.dry_run)
    if not ok:
        return EXIT_FAILED
    print("dry-run 완료 (푸시하지 않음)" if args.dry_run else "배포 완료")
    return EXIT_OK


def cmd_notify(_args: argparse.Namespace) -> int:
    from mbrief import notify as notify_mod

    # 미설정은 '실패'가 아니라 '건너뜀'이다. 전송을 시도했다가 실패한 경우만 0이 아닌 코드를 낸다.
    if not (settings.telegram_bot_token.strip() and settings.telegram_chat_id.strip()):
        print("텔레그램 미설정 (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) - 건너뜀")
        return EXIT_OK

    record = store.latest_record(settings.primary_lang)
    if record is None:
        print("전송할 브리핑이 없습니다.", file=sys.stderr)
        return EXIT_FAILED
    sent = asyncio.run(notify_mod.send_telegram(record, settings.site_url))
    print("텔레그램 전송 완료" if sent else "텔레그램 전송 실패 (logs/mbrief.log 확인)")
    return EXIT_OK if sent else EXIT_FAILED


def cmd_daily(args: argparse.Namespace) -> int:
    code = pipeline.run_daily(
        no_publish=args.no_publish,
        mock=args.mock,
        offline=args.offline,
        force=bool(getattr(args, "force", False)),
    )
    _print_notices()
    return code


def cmd_show(args: argparse.Namespace) -> int:
    """저장된 브리핑 마크다운을 터미널에 출력한다.

    기본은 공개본(포트폴리오 파트 제외)이고, --private를 주면 PART 8까지 렌더한다.
    """
    from mbrief.llm import prompts

    record_id = (getattr(args, "id", None) or "").strip()
    if record_id:
        # [SECURE] 레코드 ID를 store의 화이트리스트 정규식으로 검증 - Path Traversal 방지 (Category 1)
        if not store.RECORD_ID_RE.match(record_id):
            print("브리핑 ID 형식이 올바르지 않습니다 (예: 2026-09-07-ko-a1b2c3).", file=sys.stderr)
            return EXIT_FAILED
        record = store.load_record(record_id)
        missing = f"브리핑을 찾을 수 없습니다: {record_id}"
    else:
        record = store.latest_record(settings.primary_lang)
        missing = f"저장된 브리핑이 없습니다 ({settings.primary_lang}). 먼저 generate를 실행하세요."

    # [SECURE] Optional 반환값을 사용 전 None 검사 - Null Pointer Dereference 방지 (Category 5)
    if record is None:
        print(missing, file=sys.stderr)
        return EXIT_FAILED

    private = bool(getattr(args, "private", False))
    if record.structured is not None:
        markdown = prompts.render_markdown(record.structured, record.lang, include_portfolio=private)
    else:
        markdown = record.contentMarkdown
    if not private:
        # [SECURE] 공개 출력 경로는 사이트 빌더와 같은 규칙으로 비공개 파트를 잘라낸다
        #          - Improper Authorization 방지 (Category 2)
        markdown = prompts.strip_portfolio_section(markdown)

    print(f"# {record.headline}")
    print(f"_{record.id} · {record.date} · {record.lang} · {record.provider}/{record.model}_")
    print("")
    print(markdown.rstrip())
    if private and record.structured is not None and not (record.structured.portfolioNote or "").strip():
        print("")
        print("(포트폴리오 조언 없음 - data/portfolio.json이 있어야 생성됩니다)", file=sys.stderr)
    return EXIT_OK


def cmd_preview(args: argparse.Namespace) -> int:
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    site_dir = paths.SITE_DIR
    if not site_dir.is_dir():
        print(f"빌드 결과가 없습니다: {site_dir}\n먼저 `python -m mbrief build`를 실행하세요.", file=sys.stderr)
        return EXIT_FAILED

    port = int(args.port)
    if not (_PREVIEW_MIN_PORT <= port <= _PREVIEW_MAX_PORT):
        print(f"포트는 {_PREVIEW_MIN_PORT}~{_PREVIEW_MAX_PORT} 범위여야 합니다.", file=sys.stderr)
        return EXIT_FAILED

    handler = partial(SimpleHTTPRequestHandler, directory=str(site_dir))
    try:
        # [SECURE] 루프백(127.0.0.1)에만 바인딩 - 외부 네트워크 노출 방지 (Category 2)
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as e:
        print(f"서버를 시작할 수 없습니다 (포트 {port}): {e}", file=sys.stderr)
        return EXIT_FAILED

    # 파이프로 리다이렉트해도 URL이 바로 보이도록 flush한다 (블록 버퍼링 회피)
    print(f"미리보기: http://127.0.0.1:{port}/  (Ctrl+C로 종료)", flush=True)
    print(f"서빙 경로: {site_dir}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        server.server_close()  # [SECURE] 소켓 확실히 해제 (Category 5)
    return EXIT_OK


# ===== doctor =====
_REQUIRED_MODULES = ["httpx", "yfinance", "pydantic", "pydantic_settings", "jinja2", "defusedxml"]
_OPTIONAL_MODULES = ["google.genai", "openai", "anthropic"]


def _check_modules(names: list[str]) -> list[tuple[str, bool]]:
    result = []
    for name in names:
        try:
            importlib.import_module(name)
            result.append((name, True))
        except ImportError as e:
            logger.debug("[Doctor] 모듈 없음 %s: %s", name, e)
            result.append((name, False))
    return result


def _task_registered() -> str:
    """Windows 작업 스케줄러 등록 여부."""
    if sys.platform != "win32":
        return "해당 없음 (Windows 아님)"
    try:
        # [SECURE] shell=False + argv 리스트 실행 - OS Command Injection 방지 (Category 1)
        proc = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_SUBPROCESS_TIMEOUT, shell=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.debug("[Doctor] schtasks 조회 실패: %s", e)
        return "확인 실패 (schtasks 미사용 가능)"
    return "등록됨" if proc.returncode == 0 else "미등록"


def _git_publish_info() -> str:
    from mbrief import publish as publish_mod
    try:
        url = publish_mod.resolve_remote()
        branch = publish_mod.resolve_branch()
    except publish_mod.PublishError as e:
        return f"확인 실패 ({e})"
    return f"{publish_mod.redact(url)} → {branch}"


def cmd_doctor(_args: argparse.Namespace) -> int:
    from mbrief.llm import claude_cli

    ok = True
    print("=== mbrief doctor ===")
    print(f"Python           : {platform.python_version()} ({sys.executable})")
    print(f"플랫폼           : {platform.system()} {platform.release()}")
    print(f"프로젝트 루트    : {paths.ROOT}")

    py_ok = sys.version_info >= (3, 11)
    print(f"Python 3.11+     : {'OK' if py_ok else '필요 (asyncio.timeout 사용)'}")
    ok = ok and py_ok

    print("\n-- 의존성 --")
    for name, present in _check_modules(_REQUIRED_MODULES):
        print(f"  {name:<20} {'OK' if present else '없음 (pip install -r requirements.txt)'}")
        ok = ok and present
    for name, present in _check_modules(_OPTIONAL_MODULES):
        print(f"  {name:<20} {'OK' if present else '없음 (선택)'}")

    print("\n-- 설정 --")
    env_path = paths.ROOT / ".env"
    print(f"  .env             {'있음' if env_path.is_file() else '없음 (.env.example 참고)'}")
    print(f"  FINNHUB_API_KEY  {'설정됨' if settings.finnhub_api_key.strip() else '미설정 (뉴스는 RSS만 수집)'}")
    print(f"  언어             {', '.join(settings.langs)}")
    print(f"  실행 요일        {', '.join(sorted(settings.active_days)) or '매일'}")
    print(f"  SITE_URL         {settings.site_url or '미설정'}")

    print("\n-- Claude CLI --")
    health = asyncio.run(claude_cli.healthcheck())
    print(f"  설치             {'OK' if health['installed'] else '없음'}")
    print(f"  경로             {health.get('path') or '-'}")
    print(f"  버전             {health.get('version') or '-'}")
    if health["installed"]:
        probe = asyncio.run(claude_cli.probe())
        if probe["ok"]:
            print(f"  호출 테스트      OK ({probe['durationMs']}ms) → {probe['message']}")
        elif probe["needsLogin"]:
            print(f"  호출 테스트      실패 — {claude_cli.LOGIN_HINT}")
            ok = False
        else:
            print(f"  호출 테스트      실패 — {probe['message']}")
            ok = False
    else:
        print(f"  호출 테스트      건너뜀 — {claude_cli.LOGIN_HINT}")
        ok = False

    print("\n-- 배포 --")
    print(f"  대상             {_git_publish_info()}")
    print(f"  site/            {'있음' if paths.SITE_DIR.is_dir() else '없음 (build 필요)'}")

    print("\n-- 스케줄 --")
    print(f"  '{TASK_NAME}'  {_task_registered()}")
    print(f"  등록 방법        python -m mbrief install-task")

    print(f"\n결과: {'정상' if ok else '조치 필요 (위 항목 확인)'}")
    return EXIT_OK if ok else EXIT_FAILED


def cmd_install_task(_args: argparse.Namespace) -> int:
    """작업 등록 명령을 출력만 한다 (직접 등록하지 않는다)."""
    script = paths.ROOT / "scripts" / "install_task.ps1"
    print("Windows 작업 스케줄러에 등록하려면 아래 명령을 PowerShell에서 실행하세요.")
    print("(관리자 권한 없이 현재 사용자 계정으로 등록됩니다)\n")
    print(f'  powershell -ExecutionPolicy Bypass -File "{script}" -Time "07:00"\n')
    print("해제:")
    print(f'  powershell -ExecutionPolicy Bypass -File "{paths.ROOT / "scripts" / "uninstall_task.ps1"}"')
    if not script.is_file():
        print(f"\n[warn] 스크립트를 찾을 수 없습니다: {script}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def cmd_migrate(_args: argparse.Namespace) -> int:
    converted = store.legacy_import()
    if not converted:
        print("변환할 v1 레코드가 없습니다.")
        return EXIT_OK
    print(f"v1 → v2 변환 {len(converted)}건 (원본은 *.v1.bak 으로 보관)")
    for record in converted:
        print(f"  {record.date} → {record.id}")
    return EXIT_OK


# ===== 파서 =====
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mbrief",
        description="로컬 Claude로 매일 아침 마켓 브리핑을 생성하고 정적 사이트로 배포합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "예시:\n"
            "  python -m mbrief daily              전체 파이프라인 (작업 스케줄러가 호출)\n"
            "  python -m mbrief generate --mock --offline   네트워크·LLM 없이 샘플 레코드 생성\n"
            "  python -m mbrief show --private              최신 브리핑을 포트폴리오 조언까지 출력\n"
            "  python -m mbrief preview --port 3000         site/ 로컬 미리보기\n"
            "  python -m mbrief doctor             환경 진단 (로그인 상태 포함)\n"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG 로그 출력")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_refresh = sub.add_parser("refresh", help="시세·섹터·공포탐욕·캘린더·차트 갱신")
    p_refresh.add_argument("--offline", action="store_true", help="네트워크 없이 픽스처 사용")
    p_refresh.set_defaults(func=cmd_refresh)

    p_score = sub.add_parser("score", help="이전 브리핑 방향성 예측 채점")
    p_score.set_defaults(func=cmd_score)

    p_generate = sub.add_parser("generate", help="브리핑 생성")
    p_generate.add_argument("--lang", help="쉼표 구분 언어 (예: ko,en). 기본값은 BRIEFING_LANGS")
    p_generate.add_argument("--mock", action="store_true", help="LLM 호출 없이 픽스처 본문 사용")
    p_generate.add_argument("--offline", action="store_true", help="네트워크 없이 픽스처 사용")
    p_generate.add_argument("--model", help="claude CLI 모델 (sonnet|opus|fable|claude-...). 최소 sonnet")
    p_generate.add_argument("--force", action="store_true",
                            help="오늘 브리핑이 이미 있어도 다시 생성")
    p_generate.set_defaults(func=cmd_generate)

    p_build = sub.add_parser("build", help="data/ + web/ → site/ 정적 빌드")
    p_build.set_defaults(func=cmd_build)

    p_publish = sub.add_parser("publish", help="site/ 를 배포 브랜치로 푸시")
    p_publish.add_argument("--dry-run", action="store_true", help="커밋까지만 하고 푸시하지 않음")
    p_publish.set_defaults(func=cmd_publish)

    p_notify = sub.add_parser("notify", help="최신 브리핑을 텔레그램으로 전송")
    p_notify.set_defaults(func=cmd_notify)

    p_daily = sub.add_parser("daily", help="refresh → score → generate → build → publish → notify")
    p_daily.add_argument("--no-publish", action="store_true", help="배포 단계 건너뜀")
    p_daily.add_argument("--mock", action="store_true", help="LLM 호출 없이 픽스처 본문 사용")
    p_daily.add_argument("--offline", action="store_true", help="네트워크 없이 픽스처 사용")
    p_daily.add_argument("--force", action="store_true",
                         help="오늘 브리핑이 이미 있어도 다시 생성 (기본은 build/publish만 재실행)")
    p_daily.set_defaults(func=cmd_daily)

    p_show = sub.add_parser("show", help="저장된 브리핑 마크다운을 터미널에 출력")
    p_show.add_argument("--private", action="store_true",
                        help="PART 8(포트폴리오 맞춤 조언)까지 포함해 출력 (기본은 제외)")
    p_show.add_argument("--id", help="브리핑 ID (예: 2026-09-07-ko-a1b2c3). 생략하면 최신")
    p_show.set_defaults(func=cmd_show)

    p_preview = sub.add_parser("preview", help="site/ 를 127.0.0.1에서 서빙")
    p_preview.add_argument("--port", type=int, default=8000, help="포트 (기본 8000)")
    p_preview.set_defaults(func=cmd_preview)

    p_doctor = sub.add_parser("doctor", help="환경 진단 (의존성·인증·배포·스케줄)")
    p_doctor.set_defaults(func=cmd_doctor)

    p_task = sub.add_parser("install-task", help="작업 스케줄러 등록 명령 출력")
    p_task.set_defaults(func=cmd_install_task)

    p_migrate = sub.add_parser("migrate", help="v1 브리핑 레코드를 v2 형식으로 변환")
    p_migrate.set_defaults(func=cmd_migrate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK

    setup_logging("mbrief", logging.DEBUG if args.verbose else logging.INFO)

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n중단되었습니다.", file=sys.stderr)
        return 130
    except Exception as e:
        # [SECURE] 스택 트레이스는 로그 파일에만, 콘솔에는 요약 메시지 (Category 4)
        logger.exception("[CLI] '%s' 명령 실패: %s", args.command, e)
        print(f"'{args.command}' 실패: {e}\n자세한 내용은 {paths.LOG_DIR / 'mbrief.log'} 참고", file=sys.stderr)
        return EXIT_FAILED


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Sensitive Info in Logs: httpx/httpcore/SDK 로거를 WARNING으로 낮춰 요청 URL 내 자격증명 노출 차단
#   - OS Command Injection: schtasks 조회를 shell=False + argv 리스트로 실행, 사용자 입력 미포함
#   - Improper Input for Security Decision: --lang 값을 2자 소문자 정규식으로 검증, 포트 범위 검증
#   - Cookie/네트워크 노출: preview 서버를 127.0.0.1에만 바인딩하여 외부 접근 차단
#   - Improper Resource Release: preview 서버 소켓을 finally에서 close, 로그 파일은 회전 핸들러로 상한
#   - Error Message Information Exposure: 예외 스택은 로그 파일로, 콘솔에는 요약과 로그 위치만 출력
#   - Improper Exception Handling: 빈 except 없음, 모든 실패 경로에서 로깅 후 종료 코드 반환
#   - Infinite Loop: 외부 프로세스 호출(schtasks)에 30초 타임아웃
#   - Leftover Debug Code: 디버그 출력은 --verbose 플래그로만 활성화
# Not Applied:
#   - [WARN] preview는 인증 없는 개발용 정적 서버다. 루프백 바인딩에만 의존하므로
#     같은 PC의 다른 사용자에게는 노출된다. 공개 배포 용도로 쓰지 말 것.
#   - [WARN] doctor의 호출 테스트는 실제 claude 호출 1회를 소모한다 (sonnet, 매우 소량).
#   - [WARN] show --private 는 비공개 포트폴리오 조언을 터미널에 그대로 출력한다.
#     화면 공유·로그 캡처 중에는 사용하지 말 것 (출력 내용은 파일로 저장되지 않는다).
# --------------------------------------------------
