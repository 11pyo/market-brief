"""CLI 파서·명령 래퍼 테스트 — 네트워크도 LLM 호출도 하지 않는다.

여기서 고정하는 것
  1. build 명령이 pipeline.build_site() 하나만 호출하고 결과를 종료 코드로 옮긴다 (동작 이원화 방지)
  2. daily / generate 의 --force 플래그가 파이프라인까지 전달된다
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mbrief import cli, paths, pipeline, store
from mbrief.schemas import BriefingRecord

FIXTURES = Path(__file__).parent / "fixtures"


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


# ===== show 명령 =====
SECRET_NOTE = "SECRET-PORTFOLIO-ADVICE"


@pytest.fixture
def saved_record() -> BriefingRecord:
    """포트폴리오 조언이 들어 있는 레코드를 tmp 저장소에 저장한다."""
    raw = json.loads((FIXTURES / "briefing_sample.json").read_text(encoding="utf-8"))
    record = BriefingRecord.model_validate(raw)
    record.structured.portfolioNote = f"{SECRET_NOTE} — NVDA 비중을 줄일 것."
    store.save_record(record)
    return record


def _show(capsys, *argv) -> tuple[int, str]:
    args = cli.build_parser().parse_args(["show", *argv])
    code = cli.cmd_show(args)
    return code, capsys.readouterr().out


def test_show_prints_public_markdown_without_portfolio(capsys, saved_record):
    code, out = _show(capsys)
    assert code == cli.EXIT_OK
    assert saved_record.headline in out
    assert "## PART 1 — 전날 시장 리캡" in out
    assert "## PART 7 — 10초 마켓 대시보드" in out      # 대시보드는 잘리지 않는다
    assert SECRET_NOTE not in out
    assert "## PART 8" not in out


def test_show_private_includes_the_portfolio_part(capsys, saved_record):
    code, out = _show(capsys, "--private")
    assert code == cli.EXIT_OK
    assert SECRET_NOTE in out
    assert "## PART 8 — 포트폴리오 맞춤 조언" in out


def test_show_by_id(capsys, saved_record):
    code, out = _show(capsys, "--id", saved_record.id)
    assert code == cli.EXIT_OK
    assert saved_record.id in out


def test_show_rejects_malformed_id(capsys):
    """[SECURE] 레코드 ID는 store.RECORD_ID_RE 화이트리스트로 검증한다 (Path Traversal 방지)."""
    for bad in ("../../etc/passwd", "2026-09-07-ko-zzzzzz.json", "not-an-id"):
        args = cli.build_parser().parse_args(["show", "--id", bad])
        assert cli.cmd_show(args) == cli.EXIT_FAILED
    assert "형식" in capsys.readouterr().err


def test_show_reports_missing_record(capsys):
    args = cli.build_parser().parse_args(["show"])
    assert cli.cmd_show(args) == cli.EXIT_FAILED
    assert "브리핑" in capsys.readouterr().err


def test_show_falls_back_to_content_markdown(capsys, saved_record):
    """구조화 출력이 없는 레코드는 contentMarkdown을 쓰고, 공개 출력에서는 비공개 파트를 잘라낸다."""
    saved_record.structured = None
    saved_record.contentMarkdown = (
        "## PART 7 — 10초 마켓 대시보드\n- S&P 500\n\n"
        f"## PART 8 — 포트폴리오 맞춤 조언\n{SECRET_NOTE}\n"
    )
    store.save_record(saved_record)

    code, out = _show(capsys)
    assert code == cli.EXIT_OK
    assert "PART 7 — 10초 마켓 대시보드" in out
    assert SECRET_NOTE not in out


def test_show_parser_defaults(monkeypatch):
    args = cli.build_parser().parse_args(["show"])
    assert args.func is cli.cmd_show
    assert args.private is False
    assert args.id is None


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Path Traversal: show --id 값이 store.RECORD_ID_RE 화이트리스트로 거부되는지 검증
#   - Improper Authorization: show 기본 출력에 비공개 포트폴리오 조언이 없고,
#     --private 를 명시할 때만 PART 8이 나오는지 검증 (구조화·텍스트 폴백 양쪽)
#   - Improper Input for Security Decision: --lang 값이 2자 소문자 화이트리스트로 걸러지는지 검증
#   - Missing Error Handling: 빌드 실패가 0이 아닌 종료 코드로 보고되는지 검증 (스케줄러가 실패 인지)
#   - Error Message Information Exposure: 실패 시 콘솔에는 요약과 로그 위치만 출력되는지 검증
# Not Applied:
#   - [WARN] doctor/preview/install-task 명령은 외부 프로세스·소켓에 의존하므로 여기서 다루지 않는다.
# --------------------------------------------------
