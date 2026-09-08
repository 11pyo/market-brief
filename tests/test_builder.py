"""mbrief.site.builder 정적 사이트 빌드 테스트.

회귀로 고정하는 것
  1. 산출물 구성 (index/archive/data/feed/.nojekyll/sw.js 플레이스홀더 치환)
  2. 비공개 포트폴리오 파트(구계약 PART 7 / v2.1 PART 8)가 공개 산출물 어디에도 남지 않는다
  3. LLM 유래 문자열이 이스케이프되지 않은 마크업으로 새어나가지 않는다
  4. 루트 절대 경로(href="/", src="/")가 없다 — GitHub Pages 하위 경로 배포 전제
  5. data/index.json 이 SiteIndex 계약을 만족한다
  6. v2.1 신규 필드(sessionRecap / keyActors)가 있으면 렌더되고, 없으면 빈 섹션 마크업을 남기지 않는다
  7. v2.2 신규 필드(sectorHeat / sectorValuation)도 같은 계약을 따르며,
     밸류에이션 판정 어휘(저평가/고평가)가 산출물 어디에도 나오지 않는다
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from mbrief.schemas import BriefingRecord, MarketBundle, Scorecard, SiteIndex
from mbrief.site import builder

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "SECRET-PART7"
SECRET8 = "SECRET-PART8"          # v2.1 계약: 포트폴리오 파트가 PART 8로 번호가 바뀌었다
XSS = "<script>alert(1)</script>"
#: 섹터 과열도는 가격 스트레치 지표다. 밸류에이션 판정 어휘가 화면에 나오면 계약 위반이다.
VALUATION_VERDICT_WORDS = ("저평가", "고평가")


# ===== 픽스처 =====


@pytest.fixture(scope="module")
def sample_record() -> BriefingRecord:
    raw = json.loads((FIXTURES / "briefing_sample.json").read_text(encoding="utf-8"))
    return BriefingRecord.model_validate(raw)


@pytest.fixture(scope="module")
def candles() -> list[dict]:
    return json.loads((FIXTURES / "chart_spx_1d.json").read_text(encoding="utf-8"))


@pytest.fixture
def fallback_record(sample_record: BriefingRecord) -> BriefingRecord:
    """구조화 출력이 없는 텍스트 폴백 레코드 (PART 2 확률 바 정규식 경로를 태운다)."""
    return BriefingRecord(
        id="2026-09-06-ko-b2c3d4",
        date="2026-09-06",
        lang="ko",
        generatedAt="2026-09-06T07:03:00+09:00",
        provider="gemini",
        model="gemini-2.5-flash",
        generationTimeMs=42000,
        newsCount=37,
        tierCounts={"t1": 5, "t2": 21, "t3": 11},
        headline="텍스트 폴백 헤드라인 — 금리 관망 속 박스권",
        structured=None,
        contentMarkdown=(
            "## PART 1 — 어젯밤 핵심\n"
            "**10년물**이 4.7%에서 횡보했다.\n\n"
            "## PART 2 — 방향성 확률\n"
            "📈 상승: 40%\n"
            "➡️ 횡보: 35%\n"
            "📉 하락: 25%\n\n"
            "- 근거 하나\n"
            "- 근거 둘\n"
        ),
        market=sample_record.market,
        topNews=sample_record.topNews[:4],
    )


@pytest.fixture
def hostile_record(sample_record: BriefingRecord) -> BriefingRecord:
    """헤드라인에 스크립트 태그, PART 7에 비공개 메모가 들어간 레코드."""
    structured = sample_record.structured.model_copy(deep=True)
    structured.headline = XSS
    structured.portfolioNote = f"보유 비중 조정: {SECRET}"
    return BriefingRecord(
        id="2026-09-05-ko-c3d4e5",
        date="2026-09-05",
        lang="ko",
        generatedAt="2026-09-05T07:02:00+09:00",
        provider="claude_cli",
        model="claude-sonnet-5",
        generationTimeMs=310000,
        newsCount=44,
        tierCounts={"t1": 7, "t2": 25, "t3": 12},
        headline=XSS,
        structured=structured,
        contentMarkdown=(
            "## PART 6 — 대시보드\n"
            "- S&P 500: 7,718\n\n"
            f"## PART 7 — 포트폴리오\n{SECRET} 를 절대 공개하지 말 것.\n"
        ),
        market=sample_record.market,
        topNews=sample_record.topNews[:3],
    )


@pytest.fixture
def legacy_record(sample_record: BriefingRecord) -> BriefingRecord:
    """v2.1 이전 레코드 — sessionRecap이 없고 keyActors가 비어 있다.

    실제 data/briefings/ 의 기존 레코드가 전부 이 형태다. 새 섹션이 부재를 허용하지 못하면
    과거 아카이브 페이지가 통째로 깨지므로 렌더 경로 전체를 이 픽스처로 태운다.
    """
    structured = sample_record.structured.model_copy(deep=True)
    structured.sessionRecap = None
    structured.keyActors = []
    return BriefingRecord(
        id="2026-09-03-ko-e5f6a7",
        date="2026-09-03",
        lang="ko",
        generatedAt="2026-09-03T07:00:00+09:00",
        provider="claude_cli",
        model="claude-sonnet-5",
        headline="구버전 레코드 — 리캡·인물 필드 없음",
        structured=structured,
        market=sample_record.market,
        topNews=sample_record.topNews[:2],
    )


@pytest.fixture
def preheat_record(sample_record: BriefingRecord) -> BriefingRecord:
    """v2.2 이전 레코드 — market.sectorHeat / sectorValuation 이 빈 dict.

    실제 data/briefings/ 의 기존 레코드가 전부 이 형태다(필드 자체가 없어 기본값 {}로 채워진다).
    새 섹션이 부재를 허용하지 못하면 과거 아카이브가 통째로 깨진다.
    """
    record = sample_record.model_copy(deep=True)
    record.id = "2026-09-01-ko-a7b8c9"
    record.date = "2026-09-01"
    record.generatedAt = "2026-09-01T07:00:00+09:00"
    record.headline = "구버전 레코드 — 과열도·밸류에이션 필드 없음"
    record.market.sectorHeat = {}
    record.market.sectorValuation = {}
    return record


@pytest.fixture
def part8_record(sample_record: BriefingRecord) -> BriefingRecord:
    """v2.1 번호(PART 8)로 포트폴리오 파트를 쓴 텍스트 폴백 레코드."""
    return BriefingRecord(
        id="2026-09-02-ko-f6a7b8",
        date="2026-09-02",
        lang="ko",
        generatedAt="2026-09-02T07:00:00+09:00",
        provider="openai",
        model="gpt-fallback",
        headline="PART 8 표기 폴백 레코드",
        structured=None,
        contentMarkdown=(
            "## PART 6 — 트레이드 아이디어\n"
            "- XLU 롱 84.0~84.5\n\n"
            "## PART 8 — 포트폴리오 맞춤 조언\n"
            f"{SECRET8} 는 절대 공개하지 말 것.\n"
        ),
        market=sample_record.market,
        topNews=sample_record.topNews[:2],
    )


@pytest.fixture
def scorecard() -> Scorecard:
    entries = []
    for i in range(14):
        day = f"2026-08-{(i + 1):02d}"
        hit = i % 3 != 0
        entries.append(
            {
                "date": day,
                "briefingId": f"{day}-ko-000000",
                "predicted": "up",
                "probs": {"up": 45, "flat": 30, "down": 25},
                "actual": "up" if hit else "down",
                "spxChangePercent": 0.8 if hit else -0.6,
                "scoredAt": f"{day}T08:00:00+09:00",
                "hit": hit,
                "brier": 0.31,
            }
        )
    return Scorecard.model_validate(
        {
            "updatedAt": "2026-09-07T07:10:00+09:00",
            "entries": entries,
            "hitRate": 0.6428,
            "avgBrier": 0.3142,
            "sampleSize": 14,
            "sampleWindow": 30,
        }
    )


def make_settings(**overrides):
    base = dict(
        site_url="",
        site_name="증시흐름",
        site_title="AI 모닝 마켓 브리핑 | 증시흐름",
        site_description="매일 아침 로컬 Claude가 만드는 마켓 브리핑.",
        github_repo_url="https://github.com/11pyo/market-brief-ai",
        primary_lang="ko",
        publish_portfolio_note=False,
        max_briefings_on_site=60,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def isolate_valuation_history(monkeypatch, tmp_path_factory):
    """빌드가 개발 머신의 실제 data/valuation 기록을 집어삼키지 않게 격리한다.

    paths.VALUATION_FILE 은 존재할 수도, 아직 없을 수도 있다(상류 에이전트 소관).
    어느 쪽이든 테스트 산출물이 머신 상태에 좌우되면 안 되므로 없는 경로로 고정한다.
    이 기능을 직접 검증하는 테스트는 나중에 다시 setattr 해서 덮어쓴다.
    """
    monkeypatch.setattr(
        builder._paths,
        "VALUATION_FILE",
        tmp_path_factory.mktemp("no-valuation") / "sector_valuation.jsonl",
        raising=False,
    )


@pytest.fixture
def built(tmp_path, sample_record, fallback_record, hostile_record, candles, scorecard):
    records = [sample_record, fallback_record, hostile_record]
    charts = {"sp500_1d": candles, "sp500_5d": candles, "sp500_1mo": candles}
    result = builder.build(
        records,
        sample_record.market,
        scorecard,
        charts,
        out_dir=tmp_path / "site",
        settings=make_settings(),
    )
    return result


def read_all_html(out_dir: Path) -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8") for p in out_dir.rglob("*.html")}


def read_rendered_pages(out_dir: Path) -> dict[Path, str]:
    """빌더가 렌더한 실제 페이지만. 검색엔진 인증용 평문 .html 파일은 제외한다."""
    return {
        path: html
        for path, html in read_all_html(out_dir).items()
        if html.lstrip().lower().startswith("<!doctype html")
    }


# ===== 1. 산출물 구성 =====


def test_build_result_counts(built, tmp_path):
    assert built.records == 3
    assert built.pages == 5  # index + archive/index + 레코드 3건
    assert built.out_dir == (tmp_path / "site").resolve()
    assert len(built.build_id) == 10


@pytest.mark.parametrize(
    "relative",
    [
        "index.html",
        "archive/index.html",
        "archive/2026-09-07-ko-a1b2c3.html",
        "archive/2026-09-06-ko-b2c3d4.html",
        "archive/2026-09-05-ko-c3d4e5.html",
        "data/index.json",
        "data/latest.json",
        "data/market.json",
        "data/scorecard.json",
        "data/briefings/2026-09-07-ko-a1b2c3.json",
        "data/charts/sp500_1d.json",
        "feed.xml",
        ".nojekyll",
        "sw.js",
        "manifest.json",
        "robots.txt",
        "llms.txt",
        "css/style.css",
        "js/app.js",
        "icons/icon-192.png",
        "googleb6c316d4f0a32191.html",
        "naver9852772a7a775c99a2a8e2b752baaf21.html",
        "BingSiteAuth.xml",
    ],
)
def test_expected_files_exist(built, relative):
    assert (built.out_dir / relative).exists(), f"누락된 산출물: {relative}"


def test_sw_placeholder_is_replaced(built):
    sw = (built.out_dir / "sw.js").read_text(encoding="utf-8")
    assert "__BUILD_ID__" not in sw
    assert built.build_id in sw


def test_robots_drops_sitemap_line_without_site_url(built):
    robots = (built.out_dir / "robots.txt").read_text(encoding="utf-8")
    assert "__SITE_URL__" not in robots
    assert "Sitemap:" not in robots
    assert not (built.out_dir / "sitemap.xml").exists()


def test_sitemap_written_when_site_url_is_set(tmp_path, sample_record, candles):
    result = builder.build(
        [sample_record],
        sample_record.market,
        None,
        {"sp500_1d": candles},
        out_dir=tmp_path / "site",
        settings=make_settings(site_url="https://11pyo.github.io/market-brief-ai"),
    )
    sitemap = (result.out_dir / "sitemap.xml").read_text(encoding="utf-8")
    assert "https://11pyo.github.io/market-brief-ai/" in sitemap
    assert f"market-brief-ai/archive/{sample_record.id}.html" in sitemap
    robots = (result.out_dir / "robots.txt").read_text(encoding="utf-8")
    assert "Sitemap: https://11pyo.github.io/market-brief-ai/sitemap.xml" in robots


def test_index_is_the_latest_primary_language_record(built, sample_record):
    index_html = (built.out_dir / "index.html").read_text(encoding="utf-8")
    # '&'는 autoescape로 '&amp;'가 되므로 앰퍼샌드가 없는 구간으로 확인한다
    assert "파월 잭슨홀 발언 후 10년물 4.78% 재상승" in index_html
    assert "S&amp;P 500 7,718로 0.4% 반락" in index_html
    assert "S&P 500 7,718" not in index_html
    assert 'data-base="./"' in index_html


def test_archive_pages_use_parent_relative_base(built):
    page = (built.out_dir / "archive" / "2026-09-06-ko-b2c3d4.html").read_text(encoding="utf-8")
    assert 'data-base="../"' in page
    assert '"../css/style.css' in page


# ===== 2. 비공개 필드 유출 금지 =====


def test_portfolio_note_never_reaches_any_output_file(built):
    hits = [
        path.relative_to(built.out_dir).as_posix()
        for path in built.out_dir.rglob("*")
        if path.is_file() and path.suffix in {".html", ".json", ".xml", ".txt", ".js", ".css"}
        and SECRET in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert hits == [], f"PART 7 비공개 내용이 유출된 파일: {hits}"


def test_part7_heading_is_stripped_from_fallback_markdown(built):
    record_json = json.loads(
        (built.out_dir / "data" / "briefings" / "2026-09-05-ko-c3d4e5.json").read_text(encoding="utf-8")
    )
    assert record_json["structured"]["portfolioNote"] is None
    assert "PART 7" not in record_json["contentMarkdown"]
    assert "PART 6" in record_json["contentMarkdown"]


def test_input_records_are_never_mutated(tmp_path, hostile_record, sample_record, candles):
    builder.build(
        [hostile_record],
        sample_record.market,
        None,
        {"sp500_1d": candles},
        out_dir=tmp_path / "site",
        settings=make_settings(),
    )
    assert hostile_record.structured.portfolioNote is not None
    assert SECRET in hostile_record.structured.portfolioNote
    assert "PART 7" in hostile_record.contentMarkdown


@pytest.fixture
def part7_variant_record(sample_record: BriefingRecord) -> BriefingRecord:
    """PART 7 표기가 `#` 제목이 아닌 레코드 — 굵게 제목 + 일본어 제목.

    모델은 계약대로 `## PART 7`을 쓰지 않을 수 있다. 표기가 달라도 비공개 내용이
    산출물에 남으면 안 된다.
    """
    return BriefingRecord(
        id="2026-09-04-ko-d4e5f6",
        date="2026-09-04",
        lang="ko",
        generatedAt="2026-09-04T07:01:00+09:00",
        provider="gemini",
        model="gemini-2.5-flash",
        headline="표기 변형 폴백 레코드",
        structured=None,
        contentMarkdown=(
            "## PART 6 — 대시보드\n"
            "- S&P 500: 7,718\n\n"
            "**PART 7 — 포트폴리오**\n"
            f"{SECRET} 노출 금지.\n\n"
            "## パート7 — ポートフォリオ\n"
            f"{SECRET} は公開しないこと。\n"
        ),
        market=sample_record.market,
        topNews=sample_record.topNews[:2],
    )


def test_part7_variants_never_reach_any_output_file(
    tmp_path, part7_variant_record, sample_record, candles
):
    """굵게 표기/일본어 표기 PART 7도 out_dir 전체 어디에도 남지 않는다."""
    result = builder.build(
        [sample_record, part7_variant_record],
        sample_record.market,
        None,
        {"sp500_1d": candles},
        out_dir=tmp_path / "site",
        settings=make_settings(),
    )
    hits = [
        path.relative_to(result.out_dir).as_posix()
        for path in result.out_dir.rglob("*")
        if path.is_file() and SECRET in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert hits == [], f"PART 7 비공개 내용이 유출된 파일: {hits}"

    payload = json.loads(
        (result.out_dir / "data" / "briefings" / f"{part7_variant_record.id}.json")
        .read_text(encoding="utf-8")
    )
    # PART 6까지는 남고 PART 7 표식 이후는 전부 잘린다
    assert "PART 6" in payload["contentMarkdown"]
    assert "PART 7" not in payload["contentMarkdown"]
    assert "パート7" not in payload["contentMarkdown"]


@pytest.mark.parametrize("marker", [
    "## PART 7 — 포트폴리오",
    "**PART 7 — 포트폴리오**",
    "PART 7 — 포트폴리오",
    "## パート7 — ポートフォリオ",
    "### 파트 7",
    "## 第7部 — ポートフォリオ",
    "**Portfolio Advice**",
    "포트폴리오 맞춤 조언",
    # v2.1 재번호 — 포트폴리오가 PART 8이 됐다
    "## PART 8 — 포트폴리오 맞춤 조언",
    "**PART 8 — Portfolio Advice**",
    "PART 8 — 포트폴리오",
    "## パート8 — ポートフォリオ",
    "### 파트 8",
    "## 第8部 — ポートフォリオ",
])
def test_strip_portfolio_markdown_handles_marker_variants(marker):
    text = f"## PART 6 — 대시보드\n- 항목\n\n{marker}\n{SECRET}\n"
    stripped = builder.strip_portfolio_markdown(text)
    assert SECRET not in stripped
    assert "PART 6" in stripped
    # 하위 호환 별칭도 같은 결과를 낸다
    assert builder.strip_part7_markdown(text) == stripped


def test_strip_portfolio_markdown_keeps_text_without_marker():
    text = "## PART 1 — 어젯밤\n본문\n\n## PART 6 — 대시보드\n- 항목\n"
    assert builder.strip_portfolio_markdown(text) == text


def test_part8_portfolio_heading_never_reaches_any_output_file(
    tmp_path, part8_record, sample_record, candles
):
    """`## PART 8 — 포트폴리오 맞춤 조언` 아래 본문이 out_dir 어디에도 남지 않는다."""
    result = builder.build(
        [sample_record, part8_record],
        sample_record.market,
        None,
        {"sp500_1d": candles},
        out_dir=tmp_path / "site",
        settings=make_settings(),
    )
    hits = [
        path.relative_to(result.out_dir).as_posix()
        for path in result.out_dir.rglob("*")
        if path.is_file() and SECRET8 in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert hits == [], f"PART 8 비공개 내용이 유출된 파일: {hits}"

    payload = json.loads(
        (result.out_dir / "data" / "briefings" / f"{part8_record.id}.json")
        .read_text(encoding="utf-8")
    )
    assert "PART 8" not in payload["contentMarkdown"]
    assert "PART 6" in payload["contentMarkdown"]      # 앞선 공개 파트는 남는다


@pytest.mark.parametrize("heading", [
    "## PART 7 — 10초 마켓 대시보드",
    "## PART 7 — 10-Second Dashboard",
    "## パート7 — ダッシュボード",
    "## 第7部 — 十秒仪表盘",
])
def test_public_part7_dashboard_survives_portfolio_stripping(heading):
    """v2.1에서 PART 7은 공개 파트(대시보드)다 — 번호만 보고 잘라내면 안 된다.

    포트폴리오 번호가 구계약 7 → v2.1 8로 바뀌어 정규식이 7·8을 모두 잡는다.
    대시보드 제목이 확인되는 줄은 예외로 두어 공개 본문이 사라지지 않게 한다.
    """
    text = f"## PART 6 — 트레이드\n- 항목\n\n{heading}\n- S&P 500: 7,718\n"
    stripped = builder.strip_portfolio_markdown(text)
    assert "PART 6" in stripped
    assert "7,718" in stripped, "공개 대시보드 본문이 잘렸다"


def test_builder_delegates_portfolio_stripping_to_prompts_module():
    """제거 규칙의 단일 정의처가 mbrief.llm.prompts 인지 고정한다."""
    from mbrief.llm import prompts

    assert callable(getattr(prompts, "strip_portfolio_section", None))
    text = f"## PART 6 — 트레이드\n- 항목\n\n## PART 8 — 포트폴리오\n{SECRET8}\n"
    assert builder.strip_portfolio_markdown(text) == prompts.strip_portfolio_section(text)


def test_local_fallback_matches_the_shared_helper(monkeypatch):
    """prompts import가 실패해도(폴백 경로) 같은 결과를 내야 한다."""
    from mbrief.llm import prompts

    samples = [
        f"## PART 6 — 트레이드\n- 항목\n\n## PART 8 — 포트폴리오\n{SECRET8}\n",
        f"## PART 7 — 10초 마켓 대시보드\n- S&P 500: 7,718\n\n**파트 8**\n{SECRET8}\n",
        f"본문\n\n## 第8部 — ポートフォリオ\n{SECRET8}\n",
        "## PART 1 — 리캡\n본문만 있는 경우\n",
    ]
    expected = [prompts.strip_portfolio_section(t) for t in samples]
    monkeypatch.setattr(builder, "_prompts", None)
    assert [builder.strip_portfolio_markdown(t) for t in samples] == expected


def test_portfolio_note_is_published_when_the_flag_is_on(tmp_path, hostile_record, candles):
    result = builder.build(
        [hostile_record],
        hostile_record.market,
        None,
        {"sp500_1d": candles},
        out_dir=tmp_path / "site",
        settings=make_settings(publish_portfolio_note=True),
    )
    payload = (result.out_dir / "data" / "briefings" / f"{hostile_record.id}.json").read_text(
        encoding="utf-8"
    )
    assert SECRET in payload


# ===== 3. XSS =====


def test_hostile_headline_is_escaped_in_every_html_page(built):
    for path, html in read_all_html(built.out_dir).items():
        assert "<script>alert" not in html, f"이스케이프되지 않은 스크립트: {path}"
    page = (built.out_dir / "archive" / "2026-09-05-ko-c3d4e5.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_hostile_headline_is_escaped_in_feed(built):
    feed = (built.out_dir / "feed.xml").read_text(encoding="utf-8")
    assert "<script>alert" not in feed
    assert "&lt;script&gt;" in feed


def test_only_jsonld_script_tags_are_emitted(built):
    for path, html in read_all_html(built.out_dir).items():
        for chunk in html.split("<script")[1:]:
            head = chunk[:60]
            assert 'type="application/ld+json"' in head or " src=" in head, (
                f"예상치 못한 인라인 스크립트: {path} → {head!r}"
            )


# ===== 4. 상대 경로 =====


@pytest.mark.parametrize("attr", ['href="/', "href='/", 'src="/', "src='/"])
def test_no_root_absolute_paths_in_html(built, attr):
    for path, html in read_all_html(built.out_dir).items():
        assert attr not in html, f"루트 절대 경로 발견: {path} ({attr})"


def test_no_root_absolute_paths_in_client_assets(built):
    js = (built.out_dir / "js" / "app.js").read_text(encoding="utf-8")
    sw = (built.out_dir / "sw.js").read_text(encoding="utf-8")
    assert "fetch('/" not in js and 'fetch("/' not in js
    assert "register('/" not in js
    assert "'./'" in sw and "'./css/style.css'" in sw
    assert "'/'" not in sw and "'/index.html'" not in sw
    manifest = json.loads((built.out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["start_url"] == "./"
    assert manifest["scope"] == "./"
    assert all(icon["src"].startswith("./") for icon in manifest["icons"])


# ===== 5. 데이터 계약 =====


def test_index_json_matches_site_index_schema(built):
    payload = json.loads((built.out_dir / "data" / "index.json").read_text(encoding="utf-8"))
    index = SiteIndex.model_validate(payload)
    assert [e.id for e in index.entries] == [
        "2026-09-07-ko-a1b2c3",
        "2026-09-06-ko-b2c3d4",
        "2026-09-05-ko-c3d4e5",
    ]
    first = index.entries[0]
    assert first.direction is not None and first.direction.up == 30
    assert first.fearGreedScore == pytest.approx(41.9)
    assert first.spxChangePercent == pytest.approx(-0.3757)
    # 텍스트 폴백 레코드는 direction이 없다
    assert index.entries[1].direction is None


def test_latest_json_and_market_json_round_trip(built):
    latest = BriefingRecord.model_validate(
        json.loads((built.out_dir / "data" / "latest.json").read_text(encoding="utf-8"))
    )
    assert latest.id == "2026-09-07-ko-a1b2c3"
    market = MarketBundle.model_validate(
        json.loads((built.out_dir / "data" / "market.json").read_text(encoding="utf-8"))
    )
    assert "S&P 500" in market.quotes


def test_feed_has_items_and_rss_envelope(built):
    feed = (built.out_dir / "feed.xml").read_text(encoding="utf-8")
    assert feed.lstrip().startswith("<?xml")
    assert '<rss version="2.0"' in feed
    assert feed.count("<item>") == 3
    assert "archive/2026-09-07-ko-a1b2c3.html" in feed


def test_invalid_chart_keys_are_skipped(tmp_path, sample_record, candles):
    result = builder.build(
        [sample_record],
        sample_record.market,
        None,
        {"sp500_1d": candles, "../../evil": candles, "sp500_9y": candles},
        out_dir=tmp_path / "site",
        settings=make_settings(),
    )
    written = {p.name for p in (result.out_dir / "data" / "charts").iterdir()}
    assert written == {"sp500_1d.json"}


# ===== 6. 렌더 내용 =====


def test_structured_parts_are_rendered_as_components(built):
    """v2.1 계약: 공개 파트는 PART 1~7, PART 8(포트폴리오)은 공개되지 않는다."""
    html = (built.out_dir / "index.html").read_text(encoding="utf-8")
    for marker in (
        "PART 1", "PART 2", "PART 3", "PART 4", "PART 5", "PART 6", "PART 7",
        "recap-item", "actor-card", "dirbar-seg", "fg-gauge",
        "trade-card", "risk-card", "dash-row",
    ):
        assert marker in html, f"컴포넌트 누락: {marker}"
    assert "PART 8" not in html


def test_part_headings_are_numbered_one_through_seven_in_order(built):
    """파트 번호가 뒤섞이거나 건너뛰지 않는지 (라벨 재번호 회귀 방지)."""
    html = (built.out_dir / "index.html").read_text(encoding="utf-8")
    titles = re.findall(r'<h3 class="part-title">(PART \d+)[^<]*</h3>', html)
    assert titles == [f"PART {n}" for n in range(1, 8)], titles


def test_session_recap_renders_summary_and_every_driver(built, sample_record):
    """PART 1 리캡: 요약 마크다운 + drivers 전부가 번호 행으로 나온다."""
    html = (built.out_dir / "index.html").read_text(encoding="utf-8")
    recap = sample_record.structured.sessionRecap
    assert "part--recap" in html
    assert "PART 1 · 전날 시장 리캡" in html
    assert "시장을 움직인 요인" in html                      # L.recapDrivers
    assert "사흘 만에 반락 마감했다" in html                  # summary 본문
    assert "<strong>7,718.60(-0.41%)</strong>" in html      # summary 가 md 필터를 통과했다
    for driver in recap.drivers:
        assert driver in html, f"누락된 driver: {driver}"
    assert html.count('class="recap-item"') == len(recap.drivers)


def test_key_actor_cards_render_name_role_impact_and_ticker_pills(built):
    """PART 2 인물 카드: 이름·직함·영향 라인 + 티커 pill."""
    html = (built.out_dir / "index.html").read_text(encoding="utf-8")
    assert "시장을 움직인 인물" in html                       # L.partActors
    assert html.count('class="actor-card"') == 2
    for text in ("제롬 파월", "미국 연준(Fed) 의장", "젠슨 황", "NVIDIA CEO"):
        assert text in html, f"누락된 인물 정보: {text}"
    assert '<span class="actor-impact-label">영향</span>' in html
    for ticker in ("TLT", "QQQ", "^TNX", "NVDA", "SMH"):
        assert f">{ticker}</li>" in html, f"누락된 티커 pill: {ticker}"
    # 시장 스냅샷 12심볼과 같은 대상을 가리키는 티커만 강조된다
    assert 'class="actor-pill actor-pill--market">QQQ</li>' in html
    assert 'class="actor-pill actor-pill--market">^TNX</li>' in html
    assert 'class="actor-pill">NVDA</li>' in html
    assert 'class="actor-pill">TLT</li>' in html


def test_key_actor_names_are_escaped_not_injected(tmp_path, sample_record, candles):
    """인물 필드도 LLM 출력이다 — 마크업이 그대로 실행되면 안 된다."""
    record = sample_record.model_copy(deep=True)
    actor = record.structured.keyActors[0]
    actor.name = XSS
    actor.assets = ['"><img src=x>']
    result = builder.build(
        [record], record.market, None, {"sp500_1d": candles},
        out_dir=tmp_path / "site", settings=make_settings(),
    )
    html = (result.out_dir / "index.html").read_text(encoding="utf-8")
    assert "<script>alert" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_record_without_recap_or_actors_leaves_no_empty_section_markup(
    tmp_path, legacy_record, candles, scorecard
):
    """구버전 레코드(sessionRecap=None, keyActors=[])도 모든 페이지가 정상 생성된다."""
    result = builder.build(
        [legacy_record], legacy_record.market, scorecard, {"sp500_1d": candles},
        out_dir=tmp_path / "site", settings=make_settings(),
    )
    assert result.pages == 3          # index + archive/index + 레코드 1건
    for relative in (
        "index.html",
        "archive/index.html",
        f"archive/{legacy_record.id}.html",
        "data/index.json",
        "data/latest.json",
        "feed.xml",
    ):
        assert (result.out_dir / relative).exists(), f"누락된 산출물: {relative}"

    for path, html in read_rendered_pages(result.out_dir).items():
        for leftover in (
            "part--recap", "recap-summary", "recap-drivers", "recap-list", "recap-item",
            "actors-wrap", "actors-title", "actor-card", "actor-pill",
            "시장을 움직인 요인", "PART 1 ·", "data-l-actors",
        ):
            assert leftover not in html, f"빈 섹션 잔재: {path.name} → {leftover}"
        for attr in ('href="/', "href='/", 'src="/', "src='/"):
            assert attr not in html, f"루트 절대 경로 발견: {path.name} ({attr})"

    # 나머지 파트는 그대로 남는다
    html = (result.out_dir / "index.html").read_text(encoding="utf-8")
    for marker in ("PART 2", "PART 3", "PART 4", "PART 5", "PART 6", "PART 7"):
        assert marker in html, f"컴포넌트 누락: {marker}"
    assert "PART 8" not in html


def test_fallback_record_gets_probability_bars(built):
    html = (built.out_dir / "archive" / "2026-09-06-ko-b2c3d4.html").read_text(encoding="utf-8")
    assert "briefing-rendered" in html
    assert 'class="prob-bar-wrap up"' in html
    assert "width:40%" in html
    assert 'class="prob-bar-wrap neutral"' in html
    assert 'class="prob-bar-wrap down"' in html


def test_market_cards_expose_krw_conversion_hooks(built):
    html = (built.out_dir / "index.html").read_text(encoding="utf-8")
    assert 'data-krw-rate="1343.79"' in html
    assert 'data-name="Gold"' in html
    assert 'data-usd="4476.6001"' in html
    # 지수 포인트는 통화가 아니므로 환산 대상이 아니다
    assert 'data-name="S&amp;P 500" data-usd=' not in html


def test_sparkline_and_chart_hook_only_for_slugs_with_chart_data(built):
    html = (built.out_dir / "index.html").read_text(encoding="utf-8")
    assert 'data-chart-slug="sp500"' in html
    assert 'data-chart-slug="nasdaq"' not in html
    assert html.count("<svg class=\"spark\"") == 1


def test_scorecard_block_renders_last_twelve_dots(built):
    html = (built.out_dir / "index.html").read_text(encoding="utf-8")
    assert html.count("score-dot score-dot--") == 12
    assert "64%" in html
    assert "0.31" in html


def test_news_links_are_external_safe(built):
    html = (built.out_dir / "index.html").read_text(encoding="utf-8")
    assert 'rel="noopener noreferrer nofollow ugc"' in html
    assert 'href="javascript' not in html


def test_language_switcher_lists_only_existing_languages(tmp_path, sample_record, candles):
    en = sample_record.model_copy(deep=True)
    en.id = "2026-09-07-en-a1b2c3"
    en.lang = "en"
    en.headline = "Powell pushes back on September cut"
    result = builder.build(
        [sample_record, en],
        sample_record.market,
        None,
        {"sp500_1d": candles},
        out_dir=tmp_path / "site",
        settings=make_settings(),
    )
    html = (result.out_dir / "index.html").read_text(encoding="utf-8")
    assert 'hreflang="en"' in html
    assert 'hreflang="ja"' not in html
    assert "archive/2026-09-07-en-a1b2c3.html" in html


@pytest.fixture
def bilingual_site(tmp_path, sample_record, candles):
    """같은 날짜의 ko/en 레코드 + site_url — hreflang 절대 URL 경로를 검증한다."""
    en = sample_record.model_copy(deep=True)
    en.id = "2026-09-07-en-a1b2c3"
    en.lang = "en"
    en.headline = "Powell pushes back on September cut"
    return builder.build(
        [sample_record, en],
        sample_record.market,
        None,
        {"sp500_1d": candles},
        out_dir=tmp_path / "site",
        settings=make_settings(site_url="https://11pyo.github.io/market-brief-ai"),
    )


def test_hreflang_urls_are_never_mangled(bilingual_site):
    """`../archive/x.html` 문자열 치환 버그(`.archive/`) 재발 방지."""
    for path, html in read_all_html(bilingual_site.out_dir).items():
        assert "/.archive/" not in html, f"깨진 경로가 있는 파일: {path.name}"
        assert "/./" not in html, f"깨진 경로가 있는 파일: {path.name}"


def test_hreflang_uses_absolute_root_relative_paths(bilingual_site):
    base = "https://11pyo.github.io/market-brief-ai"
    index_html = (bilingual_site.out_dir / "index.html").read_text(encoding="utf-8")
    # index는 최신 ko 레코드다 → ko는 사이트 루트, en은 archive 하위
    assert f'hreflang="ko" href="{base}/"' in index_html
    assert f'hreflang="en" href="{base}/archive/2026-09-07-en-a1b2c3.html"' in index_html

    archive_html = (
        bilingual_site.out_dir / "archive" / "2026-09-07-en-a1b2c3.html"
    ).read_text(encoding="utf-8")
    assert f'hreflang="en" href="{base}/archive/2026-09-07-en-a1b2c3.html"' in archive_html
    assert f'hreflang="ko" href="{base}/"' in archive_html


def test_canonical_urls_are_correct_for_index_and_archive(bilingual_site):
    base = "https://11pyo.github.io/market-brief-ai"
    index_html = (bilingual_site.out_dir / "index.html").read_text(encoding="utf-8")
    assert f'<link rel="canonical" href="{base}/">' in index_html

    archive_page = (
        bilingual_site.out_dir / "archive" / "2026-09-07-en-a1b2c3.html"
    ).read_text(encoding="utf-8")
    assert (
        f'<link rel="canonical" href="{base}/archive/2026-09-07-en-a1b2c3.html">' in archive_page
    )

    archive_index = (bilingual_site.out_dir / "archive" / "index.html").read_text(encoding="utf-8")
    assert f'<link rel="canonical" href="{base}/archive/index.html">' in archive_index


# ===== 차트 기간 =====


def test_cards_declare_only_available_chart_periods(tmp_path, sample_record, candles):
    result = builder.build(
        [sample_record],
        sample_record.market,
        None,
        {"sp500_5d": candles, "sp500_1mo": candles},  # 1d 없음
        out_dir=tmp_path / "site",
        settings=make_settings(),
    )
    html = (result.out_dir / "index.html").read_text(encoding="utf-8")
    assert 'data-chart-periods="5d,1mo"' in html
    assert 'data-chart-slug="sp500"' in html
    # 1d 데이터가 없으면 스파크라인도 그리지 않는다
    assert '<svg class="spark"' not in html


def test_cards_declare_all_periods_when_present(built):
    html = (built.out_dir / "index.html").read_text(encoding="utf-8")
    assert 'data-chart-periods="1d,5d,1mo"' in html


# ===== 빌드 ID 안정성 =====


def test_build_id_is_stable_across_rebuilds(tmp_path, sample_record, candles, scorecard):
    """자산·최신 레코드가 그대로면 빌드 ID도 그대로여야 한다 (SW 캐시·?v= 무의미 무효화 방지)."""
    def run(out_name: str):
        return builder.build(
            [sample_record],
            sample_record.market,
            scorecard,
            {"sp500_1d": candles},
            out_dir=tmp_path / out_name,
            settings=make_settings(),
        )

    assert run("site1").build_id == run("site2").build_id


def test_build_id_changes_with_latest_record(tmp_path, sample_record, candles):
    first = builder.build(
        [sample_record], sample_record.market, None, {"sp500_1d": candles},
        out_dir=tmp_path / "site1", settings=make_settings(),
    )
    newer = sample_record.model_copy(deep=True)
    newer.id = "2026-09-08-ko-999999"
    newer.date = "2026-09-08"
    newer.generatedAt = "2026-09-08T07:00:00+09:00"
    second = builder.build(
        [sample_record, newer], sample_record.market, None, {"sp500_1d": candles},
        out_dir=tmp_path / "site2", settings=make_settings(),
    )
    assert first.build_id != second.build_id


# ===== 정렬 =====


def test_records_sorted_by_parsed_datetime_not_string(tmp_path, sample_record, candles):
    """오프셋이 섞여도 실제 시각 순으로 정렬한다 (문자열 비교는 순서가 뒤집힌다)."""
    early = sample_record.model_copy(deep=True)   # 09-07 07:00 +09:00 = 09-06 22:00 UTC
    early.id = "2026-09-07-ko-000001"
    early.generatedAt = "2026-09-07T07:00:00+09:00"
    late = sample_record.model_copy(deep=True)    # 09-07 00:00 -05:00 = 09-07 05:00 UTC (더 나중)
    late.id = "2026-09-07-ko-000002"
    late.generatedAt = "2026-09-07T00:00:00-05:00"

    result = builder.build(
        [early, late], sample_record.market, None, {"sp500_1d": candles},
        out_dir=tmp_path / "site", settings=make_settings(),
    )
    index_json = json.loads((result.out_dir / "data" / "index.json").read_text(encoding="utf-8"))
    assert [e["id"] for e in index_json["entries"]] == [late.id, early.id]


def test_empty_build_writes_friendly_index(tmp_path):
    result = builder.build([], None, None, {}, out_dir=tmp_path / "site", settings=make_settings())
    html = (result.out_dir / "index.html").read_text(encoding="utf-8")
    assert "아직 브리핑이 없습니다" in html
    assert result.records == 0
    assert (result.out_dir / "archive" / "index.html").exists()
    assert (result.out_dir / "feed.xml").exists()


def test_archive_groups_by_month_and_links_latest_to_index(built):
    html = (built.out_dir / "archive" / "index.html").read_text(encoding="utf-8")
    assert "2026년 9월" in html
    assert 'href="../index.html"' in html
    assert 'href="./2026-09-06-ko-b2c3d4.html"' in html


# ===== 7. 안전장치 =====


@pytest.mark.parametrize("bad", [Path("C:/"), Path("/"), Path.home()])
def test_build_refuses_unsafe_output_directories(bad):
    with pytest.raises(ValueError):
        builder._assert_safe_out_dir(bad)


def test_build_refuses_repository_root():
    with pytest.raises(ValueError):
        builder._assert_safe_out_dir(builder.ROOT)


def test_chart_key_regex_rejects_traversal():
    assert builder.CHART_KEY_RE.match("sp500_1d")
    for bad in ("../etc_1d", "sp500_1d/../x", "SP500_1d", "sp500_2d", "sp500_1d.json"):
        assert not builder.CHART_KEY_RE.match(bad), bad


# ===== 8. CSP · SRI (서드파티 코드 공급망) =====

CSP_DIRECTIVES = [
    "default-src 'self'",
    "script-src 'self' https://cdn.jsdelivr.net",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self'",
    "base-uri 'none'",
    "object-src 'none'",
    "form-action 'self'",
]


def test_every_page_declares_content_security_policy(built):
    pages = read_rendered_pages(built.out_dir)
    assert len(pages) == 5, sorted(p.name for p in pages)
    for path, html in pages.items():
        assert 'http-equiv="Content-Security-Policy"' in html, f"CSP 누락: {path}"
        for directive in CSP_DIRECTIVES:
            assert directive in html, f"CSP 지시어 누락: {path} → {directive}"
        # 인라인 스크립트 실행을 허용하는 완화 지시어가 script-src에 들어가면 안 된다
        csp = html.split('http-equiv="Content-Security-Policy" content="')[1].split('"')[0]
        script_src = csp.split("script-src")[1].split(";")[0]
        assert "unsafe-inline" not in script_src
        assert "unsafe-eval" not in script_src


def test_no_inline_executable_script_survives_the_build(built):
    """CSP(script-src에 'unsafe-inline' 없음)가 성립하려면 실행 인라인 스크립트가 0개여야 한다."""
    for path, html in read_rendered_pages(built.out_dir).items():
        for chunk in html.split("<script")[1:]:
            head = chunk[: chunk.index(">")] if ">" in chunk else chunk[:120]
            assert " src=" in head or 'type="application/ld+json"' in head, (
                f"CSP를 위반하는 인라인 스크립트: {path} → {head!r}"
            )
        # 인라인 이벤트 핸들러도 CSP에서 차단된다 (남아 있으면 기능이 조용히 죽는다)
        for handler in (" onclick=", " onload=", " onerror=", " onchange=", " onsubmit="):
            assert handler not in html, f"인라인 이벤트 핸들러: {path} ({handler})"


def test_cdn_script_has_subresource_integrity(built):
    page = (built.out_dir / "archive" / "2026-09-07-ko-a1b2c3.html").read_text(encoding="utf-8")
    assert "cdn.jsdelivr.net" in page
    tag = page[page.index("<script src=\"https://cdn.jsdelivr.net") :]
    tag = tag[: tag.index(">") + 1]
    assert 'integrity="sha384-' in tag, tag
    assert 'crossorigin="anonymous"' in tag, tag
    assert 'referrerpolicy="no-referrer"' in tag, tag
    # 해시가 실린 스크립트는 CSP script-src가 허용한 호스트에서만 온다
    assert page.count("https://cdn.jsdelivr.net") == 2  # CSP 지시어 + script src


def test_external_scripts_are_limited_to_csp_allowed_hosts(built):
    for path, html in read_rendered_pages(built.out_dir).items():
        for chunk in html.split("<script")[1:]:
            head = chunk[: chunk.index(">")] if ">" in chunk else chunk
            if " src=" not in head:
                continue
            src = head.split(' src="')[1].split('"')[0]
            if src.startswith("http"):
                assert src.startswith("https://cdn.jsdelivr.net/"), f"{path} → {src}"
                assert "integrity=" in head, f"SRI 없는 외부 스크립트: {path} → {src}"


# ===== 9. XML 비허용 제어문자 (피드 가용성) =====


def test_feed_xml_parses_when_record_headline_contains_control_chars(
    tmp_path, sample_record, candles
):
    """회귀: 헤드라인의 \\x07 같은 제어문자가 feed.xml을 깨뜨리면 안 된다."""
    from defusedxml import ElementTree as DefusedET

    from mbrief.schemas import NewsRef

    record = sample_record.model_copy(deep=True)
    record.headline = "연준 동결\x07 발표\x0b이후 반등"
    record.structured.headline = "구조화 헤드라인\x07"
    record.structured.overnight = "본문\x07 제어문자 포함"
    record.topNews = [
        NewsRef(headline="뉴스\x07 제목", source="Wire\x0c", tier=2,
                url="https://example.com/a", time="2026-09-07 06:34 UTC", category="general")
    ]

    result = builder.build(
        [record],
        record.market,
        None,
        {"sp500_1d": candles},
        out_dir=tmp_path / "site",
        settings=make_settings(site_url="https://example.github.io/mb"),
    )

    feed_text = (result.out_dir / "feed.xml").read_text(encoding="utf-8")
    assert "\x07" not in feed_text and "\x0b" not in feed_text and "\x0c" not in feed_text
    root = DefusedET.fromstring(feed_text)                 # 파싱 실패 시 예외 → 테스트 실패
    titles = [el.text for el in root.iter("title")]
    assert "연준 동결 발표이후 반등" in titles, titles      # 제어문자만 제거되고 내용은 남는다

    sitemap = (result.out_dir / "sitemap.xml").read_text(encoding="utf-8")
    DefusedET.fromstring(sitemap)


def test_schema_validators_strip_xml_illegal_control_chars():
    from mbrief.schemas import BriefingRecord, NewsRef, StructuredBriefing

    ref = NewsRef(headline="a\x07b", source="c\x00d")
    assert ref.headline == "ab" and ref.source == "cd"

    structured = StructuredBriefing.model_validate(
        {
            "headline": "h\x07x",
            "overnight": "o",
            "direction": {"up": 40, "flat": 30, "down": 30},
        }
    )
    assert structured.headline == "hx"

    record = BriefingRecord(
        id="2026-09-07-ko-a1b2c3", date="2026-09-07",
        generatedAt="2026-09-07T07:00:00+09:00", headline="r\x07e\nc",
    )
    assert record.headline == "re c"


# ===== 10. 섹터 과열도 · 밸류에이션 (v2.2) =====


def test_sector_heat_section_renders_every_label_chip(built):
    """다섯 등급 칩이 모두 나오고, 각 행이 백분위 바를 갖는다."""
    html = (built.out_dir / "index.html").read_text(encoding="utf-8")
    assert "섹터 과열도" in html                                    # L.sectorHeatTitle
    assert html.count('class="heat-row heat-row--') == 5
    for label, text in (
        ("hot", "과열"), ("warm", "다소 과열"), ("neutral", "중립"),
        ("cool", "다소 침체"), ("cold", "침체"),
    ):
        assert f'class="heat-chip heat-chip--{label}">{text}</span>' in html, label
    # 백분위 바는 판정축이므로 행마다 하나씩 있어야 한다
    assert html.count('class="heat-fill"') == 5
    for width in ("width:91%", "width:64%", "width:49%", "width:22%", "width:8%"):
        assert width in html, width


def test_sector_heat_rows_are_sorted_hottest_first(built):
    """가장 달아오른 섹터가 먼저 읽혀야 한다 (gapPercentile 내림차순)."""
    html = (built.out_dir / "index.html").read_text(encoding="utf-8")
    order = re.findall(r'class="heat-name">([^<]+)</span>', html)
    assert order[:5] == ["Technology", "Energy", "Financials", "Utilities", "Real Estate"]
    widths = [int(w) for w in re.findall(r'class="heat-fill" style="width:(\d+)%"', html)]
    assert widths == sorted(widths, reverse=True), widths


def test_sector_heat_rows_show_supporting_metrics(built):
    html = (built.out_dir / "index.html").read_text(encoding="utf-8")
    for label in ("분포 백분위", "200일선 이격", "RSI(14)", "3개월 상대"):
        assert label in html, label
    # 판정축(백분위)은 바 옆에 한 번만 보인다 — 참고 수치 목록(dl)에서 중복하지 않는다
    assert html.count('class="heat-meter-label">분포 백분위</span>') == 5
    assert "<dt>분포 백분위</dt>" not in html
    # 바에는 접근성 이름이 붙는다 (시각 라벨 + aria-label 이라 문자열 자체는 행마다 두 번 나온다)
    assert html.count('aria-label="분포 백분위 ') == 5
    assert "<dd>+8.4%</dd>" in html      # gapPct
    assert "<dd>68.3</dd>" in html       # rsi14
    assert "<dd>+6.1%p</dd>" in html     # rel3m 은 비율이 아니라 퍼센트 포인트다


def test_sector_heat_note_frames_it_as_stretch_not_valuation(built):
    """'밸류에이션 판정이 아니다 + 등급은 백분위에서 온다'가 화면에 보여야 한다."""
    html = (built.out_dir / "index.html").read_text(encoding="utf-8")
    assert 'class="heat-note"' in html
    assert "가치 판단이 아닙니다" in html
    assert "백분위" in html


def test_sector_valuation_shows_accruing_progress_instead_of_a_verdict(built):
    """pePercentile 이 None 이면 판정 대신 '기록 중 (N일)' 진행 상태만 나온다."""
    html = (built.out_dir / "index.html").read_text(encoding="utf-8")
    assert "섹터 밸류에이션" in html                                 # L.sectorValuationTitle
    assert html.count("val-row--accruing") == 3
    assert "기록 중 (1일)" in html
    assert "PER" in html and "PBR" in html
    assert "<dd>33.15</dd>" in html
    # 백분위가 없으므로 백분위 바(판정 시각화)는 그리지 않는다
    assert 'class="val-fill"' not in html


@pytest.mark.parametrize("word", VALUATION_VERDICT_WORDS)
def test_no_valuation_verdict_wording_anywhere_in_the_output(built, word):
    """pePercentile 이 없는 레코드에서 '저평가/고평가'가 어떤 산출물에도 나오면 안 된다."""
    hits = [
        path.relative_to(built.out_dir).as_posix()
        for path in built.out_dir.rglob("*")
        if path.is_file() and path.suffix in {".html", ".json", ".xml", ".txt", ".js", ".css"}
        and word in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert hits == [], f"근거 없는 밸류에이션 판정 어휘({word})가 있는 파일: {hits}"


def test_record_without_sector_heat_leaves_no_empty_section_markup(
    tmp_path, preheat_record, candles, scorecard
):
    """v2.2 이전 레코드(sectorHeat={}, sectorValuation={})도 모든 페이지가 정상 생성된다."""
    result = builder.build(
        [preheat_record], preheat_record.market, scorecard, {"sp500_1d": candles},
        out_dir=tmp_path / "site", settings=make_settings(),
    )
    assert result.pages == 3          # index + archive/index + 레코드 1건
    for relative in (
        "index.html",
        "archive/index.html",
        f"archive/{preheat_record.id}.html",
        "data/index.json",
        "data/latest.json",
        "feed.xml",
    ):
        assert (result.out_dir / relative).exists(), f"누락된 산출물: {relative}"

    for path, html in read_rendered_pages(result.out_dir).items():
        for leftover in (
            "heat-list", "heat-row", "heat-note", "heat-chip", "heat-fill", "heat-metric",
            "val-list", "val-row", "val-note", "val-metric", "val-state",
            "섹터 과열도", "섹터 밸류에이션", "기록 중", 'id="h-heat"', 'id="h-val"',
        ):
            assert leftover not in html, f"빈 섹션 잔재: {path.name} → {leftover}"
        for attr in ('href="/', "href='/", 'src="/', "src='/"):
            assert attr not in html, f"루트 절대 경로 발견: {path.name} ({attr})"

    # 관련 섹션(섹터 ETF 칩)과 본문 파트는 그대로 남는다
    html = (result.out_dir / "index.html").read_text(encoding="utf-8")
    assert "sector-chip" in html
    for marker in ("PART 1", "PART 2", "PART 3", "PART 7"):
        assert marker in html, f"컴포넌트 누락: {marker}"


def test_sector_heat_view_puts_missing_percentiles_last(sample_record):
    """백분위가 없는 섹터는 정렬 맨 뒤로 간다 (판정축이 없으므로 앞에 둘 근거가 없다)."""
    from mbrief.site import i18n

    market = sample_record.market.model_copy(deep=True)
    market.sectorHeat["Technology"].gapPercentile = None
    rows = builder._sector_heat_view(market, i18n.get_labels("ko"))
    assert [r["name"] for r in rows][-1] == "Technology"
    assert rows[-1]["percentile_text"] == "—"
    assert rows[-1]["bar"] == 0


def test_sector_heat_view_is_empty_for_a_record_without_the_field(preheat_record):
    from mbrief.site import i18n

    labels = i18n.get_labels("ko")
    assert builder._sector_heat_view(preheat_record.market, labels) == []
    assert builder._sector_valuation_view(preheat_record.market, labels) == []
    assert builder._sector_heat_view(None, labels) == []
    assert builder._sector_valuation_view(None, labels) == []


# ----- 누적 밸류에이션 기록 게시 (오프머신 복구 경로) -----


def _build_with_valuation_file(tmp_path, monkeypatch, record, candles, source):
    # paths.VALUATION_FILE 은 상류 에이전트가 추가하는 상수다. 아직 없을 수 있으므로
    # raising=False 로 주입한다 (빌더도 getattr 로 방어적으로 읽는다).
    monkeypatch.setattr(builder._paths, "VALUATION_FILE", source, raising=False)
    return builder.build(
        [record], record.market, None, {"sp500_1d": candles},
        out_dir=tmp_path / "site", settings=make_settings(),
    )


def test_valuation_history_is_published_as_a_json_array(
    tmp_path, monkeypatch, sample_record, candles
):
    source = tmp_path / "valuation.json"
    source.write_text(
        json.dumps([
            {"date": "2026-09-06", "XLK": {"trailingPE": 32.9}},
            {"date": "2026-09-07", "XLK": {"trailingPE": 33.15}},
        ]),
        encoding="utf-8",
    )
    result = _build_with_valuation_file(tmp_path, monkeypatch, sample_record, candles, source)
    payload = json.loads(
        (result.out_dir / "data" / "sector_valuation.json").read_text(encoding="utf-8")
    )
    assert isinstance(payload, list)
    assert [row["date"] for row in payload] == ["2026-09-06", "2026-09-07"]


@pytest.mark.parametrize("body", [
    # 날짜를 키로 갖는 객체
    '{"2026-09-07": {"XLK": 33.15}, "2026-09-06": {"XLK": 32.9}}',
    # 한 줄에 한 레코드 (JSONL)
    '{"date": "2026-09-06", "XLK": 32.9}\n{"date": "2026-09-07", "XLK": 33.15}\n',
])
def test_valuation_history_normalizes_other_source_shapes(
    tmp_path, monkeypatch, sample_record, candles, body
):
    """상류 파일 형식이 아직 고정되지 않았다 — 배열/객체/JSONL 어느 쪽이어도 배열로 게시한다."""
    source = tmp_path / "valuation.json"
    source.write_text(body, encoding="utf-8")
    result = _build_with_valuation_file(tmp_path, monkeypatch, sample_record, candles, source)
    payload = json.loads(
        (result.out_dir / "data" / "sector_valuation.json").read_text(encoding="utf-8")
    )
    assert isinstance(payload, list)
    assert [row["date"] for row in payload] == ["2026-09-06", "2026-09-07"]


def test_valuation_history_is_skipped_when_the_source_is_missing(
    tmp_path, monkeypatch, sample_record, candles
):
    result = _build_with_valuation_file(
        tmp_path, monkeypatch, sample_record, candles, tmp_path / "nope.json"
    )
    assert not (result.out_dir / "data" / "sector_valuation.json").exists()


def test_valuation_history_is_skipped_when_paths_has_no_constant(
    tmp_path, monkeypatch, sample_record, candles
):
    """상류가 아직 VALUATION_FILE 을 추가하지 않았어도 빌드는 그대로 성공해야 한다."""
    monkeypatch.delattr(builder._paths, "VALUATION_FILE", raising=False)
    result = builder.build(
        [sample_record], sample_record.market, None, {"sp500_1d": candles},
        out_dir=tmp_path / "site", settings=make_settings(),
    )
    assert result.pages == 3
    assert not (result.out_dir / "data" / "sector_valuation.json").exists()


def test_valuation_history_is_skipped_when_the_source_is_empty_or_broken(
    tmp_path, monkeypatch, sample_record, candles
):
    source = tmp_path / "valuation.json"
    source.write_text("not json at all\n", encoding="utf-8")
    result = _build_with_valuation_file(tmp_path, monkeypatch, sample_record, candles, source)
    assert not (result.out_dir / "data" / "sector_valuation.json").exists()


def test_valuation_history_is_skipped_when_the_source_is_too_large(
    tmp_path, monkeypatch, sample_record, candles
):
    """비대한 기록이 정적 배포본을 망가뜨리지 않게 상한을 둔다."""
    source = tmp_path / "valuation.json"
    source.write_text(json.dumps([{"date": "2026-09-07"}]), encoding="utf-8")
    monkeypatch.setattr(builder, "VALUATION_HISTORY_MAX_BYTES", 4)
    result = _build_with_valuation_file(tmp_path, monkeypatch, sample_record, candles, source)
    assert not (result.out_dir / "data" / "sector_valuation.json").exists()


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Improper Authorization: 비공개 포트폴리오 메모가 전체 산출물에서 검색되지 않음을 강제.
#     구계약 표기(PART 7)와 v2.1 표기(PART 8)를 모두 검증하고, 제거 규칙의 단일 정의처가
#     mbrief.llm.prompts.strip_portfolio_section 임을 고정한다 (폴백 경로도 동일 결과인지 대조)
#   - XSS: 모든 HTML/피드에서 미이스케이프 스크립트 부재 및 인라인 스크립트 화이트리스트 검증.
#     v2.1 신규 필드(keyActors의 이름·티커)에 마크업을 넣어도 이스케이프되는지 별도 검증
#     CSP 메타(script-src에 'unsafe-inline' 없음)와 인라인 이벤트 핸들러 부재를 함께 강제
#   - Code Download Without Integrity Check: CDN 스크립트의 SRI(integrity/crossorigin/referrerpolicy)와
#     외부 스크립트 호스트가 CSP 허용 목록에 한정되는지 검증
#   - Input Validation: XML 비허용 제어문자가 스키마 단계에서 제거되어 feed/sitemap이 파싱되는지 검증
#   - Path Traversal: 차트 키 정규식과 out_dir 삭제 화이트리스트를 직접 테스트
#   - Open Redirect: 뉴스 외부 링크의 rel 속성과 javascript: href 부재 검증
#   - Improper Input for Security Decision: 섹터 과열도 바의 인라인 width 가 0~100 정수인지,
#     등급 클래스가 화이트리스트 다섯 값에 한정되는지 렌더 결과로 검증. 근거(pePercentile)가
#     없을 때 판정 어휘(저평가/고평가)가 산출물 전체에서 검색되지 않음을 강제한다
#   - Missing Error Handling: 누적 밸류에이션 기록의 부재·상수 미정의·파싱 실패·크기 초과가
#     각각 빌드를 중단시키지 않고 게시만 건너뛰는지 검증
# Not Applied:
#   - [WARN] 브라우저 실행 검증(E2E)은 포함하지 않는다. 차트 모달·통화 전환 등 JS 동작은
#            수동 프리뷰 또는 별도 E2E 러너로 확인해야 한다.
# --------------------------------------------------

def test_chart_timezone_label_is_passed_to_the_page(tmp_path, sample_record):
    """차트 시간축 표준시 표기를 위해 body에 라벨이 실려야 한다.

    회귀 배경: 라벨은 body의 data-l-* 속성으로만 JS에 전달된다. i18n에 키를 추가해도
    base.html에 속성을 넣지 않으면 JS는 영어 폴백을 쓴다.
    """
    out = tmp_path / "site"
    builder.build([sample_record], sample_record.market, None, {}, out_dir=out)
    html = (out / "index.html").read_text(encoding="utf-8")
    assert 'data-l-chart-tz="' in html
    assert 'id="chart-tz"' in html


# ===== 홈은 최신 시장, 아카이브는 그 시점 =====
def test_index_uses_live_market_while_archive_keeps_its_snapshot(tmp_path, sample_record):
    """회귀: 장중 tick이 차트만 갱신하고 카드·과열도는 아침에 멈춰 있던 문제.

    홈(index)은 '지금의 시장'이라 최신 번들을 쓰고, 아카이브는 그 브리핑이 근거로 삼은
    스냅샷을 그대로 보존해야 한다 (과거 문서를 사후 수정하지 않는다).
    """
    from mbrief.schemas import MarketBundle, Quote

    live = MarketBundle(
        updatedAt="2026-09-08T14:37:00+09:00",
        quotes={"S&P 500": Quote(price=9999.0, change=1.0, changePercent=0.1)},
    )
    older = sample_record.model_copy(deep=True)
    older.id = "2026-09-07-ko-aaaaaa"
    older.date = "2026-09-07"
    older.generatedAt = "2026-09-07T07:00:00+09:00"

    out = tmp_path / "site"
    builder.build([sample_record, older], live, None, {}, out_dir=out)

    index_html = (out / "index.html").read_text(encoding="utf-8")
    archive_html = (out / "archive" / f"{older.id}.html").read_text(encoding="utf-8")

    assert "9,999.00" in index_html            # 홈은 최신 시세
    assert "9,999.00" not in archive_html      # 아카이브는 자기 스냅샷 유지
    assert sample_record.market is not None
