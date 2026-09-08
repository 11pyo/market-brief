"""배포 테스트 — 로컬 bare 저장소를 리모트로 사용한다 (네트워크 없음)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from mbrief import publish

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git이 없습니다")


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, shell=False,
    )


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    result = _git(["init", "--bare", "--initial-branch", "main", str(remote)])
    if result.returncode != 0:  # 구버전 git은 --initial-branch를 모른다
        result = _git(["init", "--bare", str(remote)])
    assert result.returncode == 0, result.stderr
    return remote


@pytest.fixture
def site_dir(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    (site / "archive").mkdir(parents=True)
    (site / "index.html").write_text("<h1>증시흐름</h1>", encoding="utf-8")
    (site / "archive" / "2026-09-07-ko-a1b2c3.html").write_text("<p>brief</p>", encoding="utf-8")
    (site / "data").mkdir()
    (site / "data" / "index.json").write_text('{"entries": []}', encoding="utf-8")
    return site


def _branch_files(remote: Path, branch: str) -> list[str]:
    proc = _git(["--git-dir", str(remote), "ls-tree", "-r", "--name-only", branch])
    assert proc.returncode == 0, proc.stderr
    return sorted(line.strip() for line in proc.stdout.splitlines() if line.strip())


# ===== 이름/URL 검증 =====
def test_resolve_remote_accepts_url():
    assert publish.resolve_remote("https://github.com/u/r.git") == "https://github.com/u/r.git"


def test_resolve_remote_accepts_local_path(bare_remote):
    assert publish.resolve_remote(str(bare_remote)) == str(bare_remote)


@pytest.mark.parametrize("bad", ["", "origin; rm -rf /", "--upload-pack=evil", "a b", "-o", "--exec"])
def test_resolve_remote_rejects_bad_names(bad):
    with pytest.raises(publish.PublishError):
        publish.resolve_remote(bad)


@pytest.mark.parametrize("bad", ["", "gh pages", "--force", "-b", "br;anch", "../evil"])
def test_resolve_branch_rejects_bad_names(bad):
    with pytest.raises(publish.PublishError):
        publish.resolve_branch(bad)


def test_redact_hides_credentials():
    url = "https://user:ghp_secrettoken@github.com/u/r.git"
    assert "ghp_secrettoken" not in publish.redact(url)
    assert publish.redact(url) == "https://***@github.com/u/r.git"


# ===== 실제 배포 =====
def test_publish_creates_branch_with_index_html(bare_remote, site_dir):
    ok = publish.publish_github(site_dir, remote=str(bare_remote), branch="gh-pages")
    assert ok is True

    files = _branch_files(bare_remote, "gh-pages")
    assert "index.html" in files
    assert "archive/2026-09-07-ko-a1b2c3.html" in files
    assert "data/index.json" in files
    assert ".nojekyll" in files  # GitHub Pages Jekyll 비활성 파일


def test_publish_updates_existing_branch(bare_remote, site_dir):
    assert publish.publish_github(site_dir, remote=str(bare_remote), branch="gh-pages")

    # 두 번째 배포: 파일 하나를 제거하고 하나를 추가
    (site_dir / "archive" / "2026-09-07-ko-a1b2c3.html").unlink()
    (site_dir / "feed.xml").write_text("<rss/>", encoding="utf-8")
    assert publish.publish_github(site_dir, remote=str(bare_remote), branch="gh-pages")

    files = _branch_files(bare_remote, "gh-pages")
    assert "feed.xml" in files
    assert "archive/2026-09-07-ko-a1b2c3.html" not in files  # 삭제가 반영되어야 한다


def test_publish_no_change_is_success(bare_remote, site_dir):
    assert publish.publish_github(site_dir, remote=str(bare_remote), branch="gh-pages")
    assert publish.publish_github(site_dir, remote=str(bare_remote), branch="gh-pages")
    proc = _git(["--git-dir", str(bare_remote), "rev-list", "--count", "gh-pages"])
    assert proc.stdout.strip() == "1"  # 변경이 없으면 새 커밋을 만들지 않는다


def test_publish_dry_run_does_not_push(bare_remote, site_dir):
    assert publish.publish_github(site_dir, remote=str(bare_remote), branch="gh-pages", dry_run=True)
    proc = _git(["--git-dir", str(bare_remote), "rev-parse", "--verify", "gh-pages"])
    assert proc.returncode != 0  # 브랜치가 생성되지 않았어야 한다


def test_publish_custom_commit_message(bare_remote, site_dir):
    assert publish.publish_github(site_dir, remote=str(bare_remote), branch="gh-pages",
                                message="publish: 2026-09-07 테스트 헤드라인")
    proc = _git(["--git-dir", str(bare_remote), "log", "-1", "--pretty=%s", "gh-pages"])
    assert proc.stdout.strip() == "publish: 2026-09-07 테스트 헤드라인"


def test_publish_missing_site_dir_returns_false(bare_remote, tmp_path):
    assert publish.publish_github(tmp_path / "nope", remote=str(bare_remote), branch="gh-pages") is False


def test_publish_bad_branch_returns_false(bare_remote, site_dir):
    assert publish.publish_github(site_dir, remote=str(bare_remote), branch="bad branch") is False


# ===== 오류 진단 =====
def test_git_env_keeps_user_askpass_helper(monkeypatch):
    """빈 GIT_ASKPASS로 사용자의 core.askPass를 무력화하지 않는다."""
    monkeypatch.delenv("GIT_ASKPASS", raising=False)
    assert "GIT_ASKPASS" not in publish._git_env()

    monkeypatch.setenv("GIT_ASKPASS", "C:/tools/askpass.exe")
    env = publish._git_env()
    assert env["GIT_ASKPASS"] == "C:/tools/askpass.exe"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_publish_error_carries_git_stderr(tmp_path):
    """진단 가능한 원인이 예외 메시지에 담긴다 (자격증명은 마스킹된 상태)."""
    with pytest.raises(publish.PublishError) as exc:
        publish._run_git(["rev-parse", "--verify", "refs/heads/does-not-exist"], cwd=tmp_path)
    message = str(exc.value)
    assert message.startswith("git rev-parse 실패")
    assert len(message) > len("git rev-parse 실패")  # stderr 원인이 붙어 있다


def test_publish_error_message_is_redacted(monkeypatch):
    class _Proc:
        returncode = 1
        stderr = "fatal: could not read from https://user:ghp_secret@github.com/u/r.git"
        stdout = ""

    monkeypatch.setattr(publish.subprocess, "run", lambda *a, **kw: _Proc())
    with pytest.raises(publish.PublishError) as exc:
        publish._run_git(["push", "origin", "HEAD"])
    assert "ghp_secret" not in str(exc.value)
    assert "***" in str(exc.value)


def test_publish_retries_push_once_before_failing(bare_remote, site_dir, monkeypatch):
    attempts: list[list[str]] = []
    real_run_git = publish._run_git

    def flaky(args, cwd=None, check=True):
        if args and args[0] == "push":
            attempts.append(list(args))
            if len(attempts) == 1:
                raise publish.PublishError("git push 실패: fatal: 일시적 오류")
        return real_run_git(args, cwd=cwd, check=check)

    monkeypatch.setattr(publish, "_run_git", flaky)
    monkeypatch.setattr(publish, "_PUSH_RETRY_PAUSE_SEC", 0.0)

    assert publish.publish_github(site_dir, remote=str(bare_remote), branch="gh-pages") is True
    assert len(attempts) == 2  # 1회 실패 후 정확히 1회만 재시도
    assert "index.html" in _branch_files(bare_remote, "gh-pages")


def test_publish_gives_up_after_one_retry(bare_remote, site_dir, monkeypatch):
    attempts: list[list[str]] = []
    real_run_git = publish._run_git

    def always_failing(args, cwd=None, check=True):
        if args and args[0] == "push":
            attempts.append(list(args))
            raise publish.PublishError("git push 실패: fatal: 원격 거부")
        return real_run_git(args, cwd=cwd, check=check)

    monkeypatch.setattr(publish, "_run_git", always_failing)
    monkeypatch.setattr(publish, "_PUSH_RETRY_PAUSE_SEC", 0.0)

    assert publish.publish_github(site_dir, remote=str(bare_remote), branch="gh-pages") is False
    assert len(attempts) == 2  # 무한 재시도하지 않는다


# --------------------------------------------------
# Security Checklist
# Applied:
#   - OS Command Injection: 리모트 이름/브랜치명에 셸 메타문자·git 플래그가 들어오면 거부되는지 검증
#   - Unencrypted Sensitive Data: URL 내 자격증명이 로그 문자열에서 마스킹되는지 검증
#   - Improper Resource Release: 임시 작업 디렉터리는 publish 내부 context manager가 정리 (잔존 없음)
#   - 원격 이력 보호: dry-run이 실제로 푸시하지 않는지 검증
#   - Error Message Information Exposure: 예외 메시지에 원인이 담기되 자격증명은 마스킹되는지 검증
#   - Infinite Loop: 푸시 재시도가 정확히 1회로 제한되는지 검증
# Not Applied:
#   - [WARN] 실제 원격(GitHub) 푸시는 테스트하지 않는다. 인증·네트워크가 필요하므로
#     로컬 bare 저장소로 대체 검증한다.
# --------------------------------------------------


# ===== 배포 대상 분기 =====
# 회귀 배경: publish_site를 설정 기반 디스패처로 바꾼 뒤, .env의 PUBLISH_TARGET이
# cloudflare로 바뀌자 git 경로를 검증하던 테스트들이 조용히 다른 함수로 들어갔다.
# 분기 자체를 고정해 둔다.
def _spy(calls, name, result=True):
    def fn(*args, **kwargs):
        calls.append(name)
        return result
    return fn


def test_publish_site_routes_to_github(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(publish, "publish_github", _spy(calls, "github"))
    monkeypatch.setattr(publish, "publish_cloudflare", _spy(calls, "cloudflare"))
    assert publish.publish_site(target="github") is True
    assert calls == ["github"]


def test_publish_site_routes_to_cloudflare(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(publish, "publish_github", _spy(calls, "github"))
    monkeypatch.setattr(publish, "publish_cloudflare", _spy(calls, "cloudflare"))
    assert publish.publish_site(target="cloudflare") is True
    assert calls == ["cloudflare"]


def test_publish_site_follows_settings_when_target_omitted(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(publish, "publish_github", _spy(calls, "github"))
    monkeypatch.setattr(publish, "publish_cloudflare", _spy(calls, "cloudflare"))
    monkeypatch.setattr(publish.settings, "publish_target", "cloudflare")
    assert publish.publish_site() is True
    assert calls == ["cloudflare"]


def test_publish_site_rejects_unknown_target(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(publish, "publish_github", _spy(calls, "github"))
    monkeypatch.setattr(publish, "publish_cloudflare", _spy(calls, "cloudflare"))
    # [SECURE] 알 수 없는 대상은 조용히 기본값으로 떨어지지 않고 실패한다 - 의도치 않은 공개 방지
    assert publish.publish_site(target="ftp") is False
    assert calls == []


def test_cf_project_name_whitelist():
    for bad in ("BAD NAME", "../x", "-flag", "a" * 60, "proj;rm -rf /"):
        with pytest.raises(publish.PublishError):
            publish.resolve_cf_project(bad)
    assert publish.resolve_cf_project("market-brief") == "market-brief"


def test_cf_project_falls_back_to_settings(monkeypatch):
    monkeypatch.setattr(publish.settings, "cloudflare_project", "from-settings")
    assert publish.resolve_cf_project() == "from-settings"
    assert publish.resolve_cf_project("") == "from-settings"   # 빈 인자는 설정값으로 대체


def test_cf_project_requires_a_value(monkeypatch):
    monkeypatch.setattr(publish.settings, "cloudflare_project", "")
    with pytest.raises(publish.PublishError):
        publish.resolve_cf_project("")


# ===== 불완전한 디렉터리 배포 차단 =====
def test_cloudflare_refuses_directory_that_is_not_a_built_site(tmp_path, monkeypatch):
    """회귀: 테스트용 더미 사이트(index.html 한 장)가 운영 도메인에 배포된 사고를 막는다."""
    fake = tmp_path / "fake_site"
    fake.mkdir()
    (fake / "index.html").write_text("<h1>증시흐름</h1>", encoding="utf-8")

    monkeypatch.setattr(publish.settings, "cloudflare_project", "market-brief")
    # cf_whoami까지 가기 전에 막혀야 한다 (네트워크 호출 없음)
    assert publish.publish_cloudflare(fake) is False


def test_cloudflare_accepts_a_real_built_site(tmp_path, monkeypatch):
    real = tmp_path / "site"
    (real / "data").mkdir(parents=True)
    (real / "index.html").write_text("<html></html>", encoding="utf-8")
    (real / "data" / "index.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(publish.settings, "cloudflare_project", "market-brief")
    monkeypatch.setattr(publish, "cf_whoami", lambda: (False, "인증 없음"))
    # 사이트 검증은 통과하고 그 다음 단계(인증)에서 멈춘다
    assert publish.publish_cloudflare(real) is False
    assert publish._looks_like_built_site(real) is True
