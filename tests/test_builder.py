"""mbrief.site.builder 정적 사이트 빌드 테스트.

회귀로 고정하는 것
  1. 산출물 구성 (index/archive/data/feed/.nojekyll/sw.js 플레이스홀더 치환)
  2. 비공개 필드(PART 7)가 공개 산출물 어디에도 남지 않는다
  3. LLM 유래 문자열이 이스케이프되지 않은 마크업으로 새어나가지 않는다
  4. 루트 절대 경로(href="/", src="/")가 없다 — GitHub Pages 하위 경로 배포 전제
  5. data/index.json 이 SiteIndex 계약을 만족한다
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mbrief.schemas import BriefingRecord, MarketBundle, Scorecard, SiteIndex
from mbrief.site import builder

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "SECRET-PART7"
XSS = "<script>alert(1)</script>"


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
])
def test_strip_part7_markdown_handles_marker_variants(marker):
    text = f"## PART 6 — 대시보드\n- 항목\n\n{marker}\n{SECRET}\n"
    stripped = builder.strip_part7_markdown(text)
    assert SECRET not in stripped
    assert "PART 6" in stripped


def test_strip_part7_markdown_keeps_text_without_marker():
    text = "## PART 1 — 어젯밤\n본문\n\n## PART 6 — 대시보드\n- 항목\n"
    assert builder.strip_part7_markdown(text) == text


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
    html = (built.out_dir / "index.html").read_text(encoding="utf-8")
    for marker in (
        "PART 1", "PART 2", "PART 3", "PART 4", "PART 5", "PART 6",
        "dirbar-seg", "fg-gauge", "trade-card", "risk-card", "dash-row",
    ):
        assert marker in html, f"컴포넌트 누락: {marker}"
    assert "PART 7" not in html


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


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Improper Authorization: PART 7 비공개 메모가 전체 산출물에서 검색되지 않음을 강제
#   - XSS: 모든 HTML/피드에서 미이스케이프 스크립트 부재 및 인라인 스크립트 화이트리스트 검증.
#     CSP 메타(script-src에 'unsafe-inline' 없음)와 인라인 이벤트 핸들러 부재를 함께 강제
#   - Code Download Without Integrity Check: CDN 스크립트의 SRI(integrity/crossorigin/referrerpolicy)와
#     외부 스크립트 호스트가 CSP 허용 목록에 한정되는지 검증
#   - Input Validation: XML 비허용 제어문자가 스키마 단계에서 제거되어 feed/sitemap이 파싱되는지 검증
#   - Path Traversal: 차트 키 정규식과 out_dir 삭제 화이트리스트를 직접 테스트
#   - Open Redirect: 뉴스 외부 링크의 rel 속성과 javascript: href 부재 검증
# Not Applied:
#   - [WARN] 브라우저 실행 검증(E2E)은 포함하지 않는다. 차트 모달·통화 전환 등 JS 동작은
#            수동 프리뷰 또는 별도 E2E 러너로 확인해야 한다.
# --------------------------------------------------
