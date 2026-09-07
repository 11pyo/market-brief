"""정적 사이트 배포 — site/ → gh-pages 브랜치.

임시 디렉터리에서만 작업하므로 작업 트리(master)를 건드리지 않는다.
  1. `git ls-remote --heads <remote> <branch>`로 대상 브랜치 존재 확인
  2. 있으면 `git clone --branch <branch> --single-branch`, 없으면 `git init` + orphan 브랜치
  3. 추적 파일 전부 삭제 → site/ 내용 복사 → .nojekyll 보장
  4. `git add -A` → commit → push

모든 git 호출은 argv 리스트 + shell=False다. 리모트 URL에 자격증명이 들어 있을 수 있으므로
로그에는 항상 마스킹한 값만 남긴다.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from mbrief import paths, store
from mbrief.config import settings
from mbrief.timeutil import today_str

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 300  # [SECURE] 외부 프로세스 상한 - 무한 대기 방지 (Category 3)
_URL_SCHEMES = ("https://", "http://", "ssh://", "git://", "git@")
# [SECURE] 리모트 '이름' 화이트리스트 정규식 - argv 주입 방지 (Category 1)
# 첫 글자는 영숫자/밑줄만 허용한다. '-'로 시작하면 git이 인자를 플래그로 해석한다.
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
# [SECURE] 브랜치명 화이트리스트 정규식 - argv/refspec 주입 방지 (Category 1)
_BRANCH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,99}$")
_CRED_RE = re.compile(r"(?<=//)[^/@\s]+(?=@)")
_PUSH_RETRY_PAUSE_SEC = 2.0  # 일시적 네트워크/원격 오류 대비 1회 재시도 간격


class PublishError(RuntimeError):
    """배포 실패."""


def redact(url: str) -> str:
    """[SECURE] URL의 자격증명 부분을 마스킹 - 로그 자격증명 노출 방지 (Category 2)."""
    return _CRED_RE.sub("***", url or "")


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    # [SECURE] 대화형 자격증명 프롬프트 차단 - 무인 실행 시 무한 대기 방지 (Category 3)
    env["GIT_TERMINAL_PROMPT"] = "0"
    # GIT_ASKPASS는 건드리지 않는다. 빈 값으로 덮으면 사용자의 core.askPass 헬퍼가 무력화되어
    # 정상 설정된 환경에서도 인증이 실패한다. 무인 대기는 GIT_TERMINAL_PROMPT=0으로 이미 막힌다.
    return env


def _run_git(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """[SECURE] shell=False + argv 리스트 실행 - OS Command Injection 방지 (Category 1)."""
    argv = ["git"] + args
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
            env=_git_env(),
            shell=False,
        )
    except FileNotFoundError as e:
        raise PublishError("git이 설치되어 있지 않거나 PATH에 없습니다.") from e
    except subprocess.TimeoutExpired as e:
        raise PublishError(f"git 명령이 {_GIT_TIMEOUT}초 내에 끝나지 않았습니다: {args[0]}") from e

    if check and proc.returncode != 0:
        # [SECURE] 자격증명을 redact()로 마스킹한 뒤에만 예외 메시지에 싣는다 (Category 2/4).
        # 마스킹된 원인 없이 "git push 실패"만 남기면 무인 실행 로그로는 원인 파악이 불가능하다.
        detail = redact((proc.stderr or proc.stdout or "").strip())[:400]
        logger.error("[Publish] git %s 실패: %s", args[0], detail)
        raise PublishError(f"git {args[0]} 실패: {detail}" if detail else f"git {args[0]} 실패")
    return proc


def resolve_remote(remote: str | None = None) -> str:
    """리모트 이름 또는 URL → 실제 URL."""
    value = (remote if remote is not None else settings.publish_remote).strip()
    if not value:
        raise PublishError("PUBLISH_REMOTE가 비어 있습니다.")
    if value.startswith(_URL_SCHEMES) or value.startswith(("/", ".")) or re.match(r"^[A-Za-z]:[\\/]", value):
        return value
    if not _REMOTE_NAME_RE.match(value):
        raise PublishError("PUBLISH_REMOTE 값이 리모트 이름 형식에 맞지 않습니다.")
    proc = _run_git(["-C", str(paths.ROOT), "remote", "get-url", value])
    url = (proc.stdout or "").strip()
    if not url:
        raise PublishError(f"리모트 '{value}'의 URL을 확인할 수 없습니다.")
    return url


def resolve_branch(branch: str | None = None) -> str:
    value = (branch if branch is not None else settings.publish_branch).strip()
    if not _BRANCH_RE.match(value):
        raise PublishError("PUBLISH_BRANCH 값이 브랜치명 형식에 맞지 않습니다.")
    return value


def _remote_has_branch(url: str, branch: str) -> bool:
    proc = _run_git(["ls-remote", "--heads", url, branch], check=False)
    if proc.returncode != 0:
        logger.info("[Publish] 리모트 조회 실패 - 새 브랜치로 생성 시도 (%s)", redact(url))
        return False
    return bool((proc.stdout or "").strip())


def _ensure_identity(work: Path) -> None:
    """커밋 신원이 없으면 로컬 저장소에만 기본값을 설정한다."""
    email = _run_git(["config", "user.email"], cwd=work, check=False)
    if (email.stdout or "").strip():
        return
    _run_git(["config", "user.email", "mbrief@localhost"], cwd=work)
    _run_git(["config", "user.name", "mbrief"], cwd=work)
    logger.info("[Publish] 커밋 신원 미설정 - 임시 저장소에 기본값 사용")


def _wipe_tracked(work: Path) -> None:
    """.git을 제외한 모든 항목 제거 (삭제된 파일도 커밋에 반영되도록)."""
    for entry in work.iterdir():
        if entry.name == ".git":
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError as e:
            # [SECURE] 개별 삭제 실패를 격리하고 로깅 - 빈 except 없음 (Category 4)
            logger.warning("[Publish] 기존 파일 삭제 실패 %s: %s", entry.name, e)


def _copy_site(site_dir: Path, work: Path) -> int:
    shutil.copytree(site_dir, work, dirs_exist_ok=True)
    nojekyll = work / ".nojekyll"
    if not nojekyll.exists():
        nojekyll.write_text("", encoding="utf-8")  # GitHub Pages의 Jekyll 처리 비활성
    return sum(1 for p in work.rglob("*") if p.is_file() and ".git" not in p.parts)


def _commit_message(message: str | None) -> str:
    if message:
        return " ".join(str(message).split())[:200]
    headline = ""
    record = store.latest_record(settings.primary_lang)
    if record is not None:
        headline = " ".join(record.headline.split())[:120]
    return f"publish: {today_str()} {headline}".strip()


def publish_site(
    site_dir: Path | None = None,
    *,
    remote: str | None = None,
    branch: str | None = None,
    dry_run: bool = False,
    message: str | None = None,
    force: bool = False,
) -> bool:
    """site/ 내용을 대상 브랜치로 커밋·푸시한다. 성공하면 True."""
    source = Path(site_dir) if site_dir is not None else paths.SITE_DIR
    if not source.is_dir():
        logger.error("[Publish] 빌드 결과가 없습니다: %s (먼저 build 실행)", source)
        return False
    if not (source / "index.html").is_file():
        logger.warning("[Publish] index.html이 없습니다 - 빌드가 완전한지 확인하세요.")

    try:
        url = resolve_remote(remote)
        target_branch = resolve_branch(branch)
    except PublishError as e:
        logger.error("[Publish] %s", e)
        return False

    logger.info("[Publish] 대상 %s (%s)", redact(url), target_branch)

    # [SECURE] 임시 디렉터리를 context manager로 사용 - 잔존 파일 방지 (Category 5)
    with tempfile.TemporaryDirectory(prefix="mbrief_publish_") as tmp:
        work = Path(tmp) / "repo"
        try:
            if _remote_has_branch(url, target_branch):
                _run_git(["clone", "--branch", target_branch, "--single-branch", "--depth", "1",
                          url, str(work)])
                logger.info("[Publish] 기존 브랜치 클론 완료")
            else:
                work.mkdir(parents=True, exist_ok=True)
                _run_git(["init"], cwd=work)
                orphan = _run_git(["checkout", "--orphan", target_branch], cwd=work, check=False)
                if orphan.returncode != 0:
                    # 일부 git 버전은 빈 저장소에서 --orphan을 거부한다.
                    _run_git(["symbolic-ref", "HEAD", f"refs/heads/{target_branch}"], cwd=work)
                logger.info("[Publish] 새 브랜치 %s 생성", target_branch)

            _ensure_identity(work)
            _wipe_tracked(work)
            file_count = _copy_site(source, work)
            _run_git(["add", "-A"], cwd=work)

            status = _run_git(["status", "--porcelain"], cwd=work, check=False)
            if not (status.stdout or "").strip():
                logger.info("[Publish] 변경 사항 없음 - 커밋·푸시 건너뜀")
                return True

            _run_git(["commit", "-m", _commit_message(message)], cwd=work)

            if dry_run:
                logger.info(
                    "[Publish] dry-run 완료 - 푸시하지 않음 | 파일 %d개 | 작업 경로 %s | 대상 %s (%s)",
                    file_count, work, redact(url), target_branch,
                )
                return True

            push_args = ["push", url, f"HEAD:refs/heads/{target_branch}"]
            if force:
                # [SECURE] 강제 푸시는 명시적 요청이 있을 때만 - 원격 이력 손실 방지
                push_args.insert(1, "--force")
                logger.warning("[Publish] 강제 푸시 요청됨")
            try:
                _run_git(push_args, cwd=work)
            except PublishError as e:
                # 일시적 네트워크·원격 오류가 흔하다. [SECURE] 재시도는 정확히 1회로 고정 (Category 3)
                logger.warning("[Publish] 푸시 실패 - %.0f초 후 1회 재시도: %s", _PUSH_RETRY_PAUSE_SEC, e)
                time.sleep(_PUSH_RETRY_PAUSE_SEC)
                _run_git(push_args, cwd=work)
            logger.info("[Publish] 푸시 완료: %s → %s", target_branch, redact(url))
            return True

        except PublishError as e:
            logger.error("[Publish] 배포 실패: %s", e)
            return False
        except OSError as e:
            logger.error("[Publish] 파일 작업 실패: %s", e)
            return False


# --------------------------------------------------
# Security Checklist
# Applied:
#   - OS Command Injection: 모든 git 호출이 shell=False + argv 리스트, 사용자 값은 별도 인자로 전달
#   - Improper Input for Security Decision: 리모트 이름·브랜치명을 정규식 화이트리스트로 검증
#   - Unencrypted Sensitive Data / Sensitive Info in Logs: URL의 자격증명을 redact()로 마스킹 후 로깅
#   - Infinite Loop: 모든 git 호출에 300초 타임아웃, GIT_TERMINAL_PROMPT=0으로 대화형 대기 차단,
#     푸시 재시도는 1회로 고정
#   - Improper Resource Release: TemporaryDirectory context manager로 작업 디렉터리 정리
#   - Missing Error Handling: git 실패·파일 실패를 각각 처리하고 False 반환, 예외를 상위로 흘리지 않음
#   - Improper Exception Handling: 빈 except 없음, 모든 실패 경로에서 로깅
#   - Error Message Information Exposure: git stderr는 마스킹 후 로컬 로그에만 기록
#   - 원격 이력 보호: --force는 force=True를 명시했을 때만 사용
# Not Applied:
#   - [WARN] Improper Certificate Validation: git의 기본 TLS 검증에 의존한다 (핀닝 없음).
#   - [WARN] 자격증명 저장: 푸시 인증은 OS의 git credential helper에 위임한다. 토큰이 포함된 URL을
#     PUBLISH_REMOTE에 직접 넣으면 .env 평문에 남으므로 credential helper 사용을 권장한다.
#   - [WARN] 배포 대상 저장소가 공개인지 검증하지 않는다. 비공개 저장소는 GitHub Pages가 동작하지 않는다.
# --------------------------------------------------
