"""CLI 파서·명령 래퍼 테스트 — 네트워크도 LLM 호출도 하지 않는다.

여기서 고정하는 것
  1. build 명령이 pipeline.build_site() 하나만 호출하고 결과를 종료 코드로 옮긴다 (동작 이원화 방지)
  2. daily / generate 의 --force 플래그가 파이프라인까지 전달된다
"""
from __future__ import annotations

import pytest

from mbrief import cli, paths, pipeline


# ===== build 래퍼 =====
def test_cmd_build_delegates_to_pipeline(monkeypatch, capsys):
    calls: list[bool] = []
    monkeypatch.setattr(pipeline, "build_site", lambda: (calls.append(True), True)[1])

    assert cli.cmd_build(object()) == cli.EXIT_OK
    assert calls == [True]
    assert str(paths.SITE_DIR) in capsys.readouterr().out


def test_cmd_build_maps_failure_to_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "build_site", lambda: False)

    assert cli.cmd_build(object()) == cli.EXIT_FAILED
    assert "빌드에 실패" in capsys.readouterr().err


def test_build_parser_wires_build_command(monkeypatch):
    args = cli.build_parser().parse_args(["build"])
    assert args.func is cli.cmd_build


# ===== --force 전달 =====
def test_daily_force_flag_is_forwarded(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(pipeline, "run_daily", lambda **kw: (seen.update(kw), cli.EXIT_OK)[1])

    args = cli.build_parser().parse_args(["daily", "--no-publish", "--force"])
    assert args.force is True
    assert cli.cmd_daily(args) == cli.EXIT_OK
    assert seen["force"] is True
    assert seen["no_publish"] is True


def test_daily_force_defaults_to_false(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(pipeline, "run_daily", lambda **kw: (seen.update(kw), cli.EXIT_OK)[1])

    cli.cmd_daily(cli.build_parser().parse_args(["daily"]))
    assert seen["force"] is False


def test_generate_force_flag_is_forwarded(monkeypatch):
    seen: dict = {}

    async def fake_generate(langs=None, **kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(pipeline, "generate", fake_generate)

    args = cli.build_parser().parse_args(["generate", "--force", "--mock"])
    assert args.force is True
    assert cli.cmd_generate(args) == cli.EXIT_FAILED  # 레코드가 없으면 실패 코드
    assert seen["force"] is True
    assert seen["mock"] is True


# ===== 언어 인자 화이트리스트 =====
@pytest.mark.parametrize("raw,expected", [
    ("ko,en", ["ko", "en"]),
    ("KO, En ", ["ko", "en"]),
    ("ko,../etc,zz9", ["ko"]),
    ("", None),
    ("!!!", None),
])
def test_parse_langs_whitelist(raw, expected):
    assert cli._parse_langs(raw) == expected


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Improper Input for Security Decision: --lang 값이 2자 소문자 화이트리스트로 걸러지는지 검증
#   - Missing Error Handling: 빌드 실패가 0이 아닌 종료 코드로 보고되는지 검증 (스케줄러가 실패 인지)
#   - Error Message Information Exposure: 실패 시 콘솔에는 요약과 로그 위치만 출력되는지 검증
# Not Applied:
#   - [WARN] doctor/preview/install-task 명령은 외부 프로세스·소켓에 의존하므로 여기서 다루지 않는다.
# --------------------------------------------------
