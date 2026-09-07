"""뉴스 수집 — Finnhub(카테고리·기업) + 공식/통신사/와이어 RSS.

v1 services/news_collector.py를 이식했다. 변경점:
  - Finnhub API 키를 쿼리스트링(`?token=`)이 아니라 `X-Finnhub-Token` 헤더로 전송한다.
    v1에서는 httpx INFO 로그의 요청 URL에 키가 그대로 남았다. (Category 2: Unencrypted Sensitive Data)
  - XML 파싱을 defusedxml.ElementTree로 교체해 엔티티 확장 폭탄까지 방어한다. (Category 1: XXE)
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from defusedxml import ElementTree as DefusedET  # [SECURE] XXE·엔티티 폭탄 방어 파서 (Category 1)

from mbrief.config import settings
from mbrief.sources import httpguard

logger = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"
CATEGORIES = ["general", "forex", "crypto", "merger"]
MAX_AGE_SECONDS = 24 * 3600  # Finnhub 기본 신선도 기준

# ===== RSS 피드 정의 =====
# [SECURE] 고정 HTTPS URL만 사용 - SSRF 방지 (Category 1). 사용자 입력 URL은 절대 받지 않는다.
#
# category 의미:
#   official : 중앙은행·정부 통계 원문 (1차 출처). 발표 주기가 길어 max_age를 넓게 잡는다.
#   general  : 매크로·지정학 일반 뉴스
#   wire     : 실시간 속보 와이어 (속도 우선, 단편적)
#
# 옵션:
#   title_suffix_source : "제목 - 매체명" 형태에서 매체명을 분리 (Google News 형식)
#   drop_description    : 본문 대신 링크 HTML만 들어있는 피드에서 요약 제거
RSS_FEEDS: list[dict[str, Any]] = [
    # ── TIER 1: 1차 공식 출처 (중앙은행 · 정부 통계) ────────────────────────
    # 공식 발표는 매일 나오지 않는다. 창을 넓게 잡되, 48h 초과분은 news_filter에서 감점해
    # 오래된 정례 공지가 상단을 점유하지 않도록 균형을 맞춘다.
    {"name": "Federal Reserve", "category": "official", "max_age_h": 168, "limit": 15,
     "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    {"name": "Federal Reserve Monetary Policy", "category": "official", "max_age_h": 336, "limit": 10,
     "url": "https://www.federalreserve.gov/feeds/press_monetary.xml"},
    {"name": "Federal Reserve Speeches", "category": "official", "max_age_h": 168, "limit": 10,
     "url": "https://www.federalreserve.gov/feeds/speeches.xml"},
    {"name": "ECB", "category": "official", "max_age_h": 168, "limit": 10,
     "url": "https://www.ecb.europa.eu/rss/press.html"},
    {"name": "Bank of Japan", "category": "official", "max_age_h": 168, "limit": 10,
     "url": "https://www.boj.or.jp/en/rss/whatsnew.xml"},
    {"name": "Bank of England", "category": "official", "max_age_h": 168, "limit": 10,
     "url": "https://www.bankofengland.co.uk/rss/news"},
    {"name": "US Bureau of Labor Statistics", "category": "official", "max_age_h": 168, "limit": 5,
     "url": "https://www.bls.gov/feed/bls_latest.rss"},

    # ── TIER 2: 검증된 통신사 · 경제지 ────────────────────────────────────
    # Reuters는 2020년 자체 RSS를 폐지했으므로 Google News 경유로 수집한다.
    {"name": "Reuters", "category": "general", "max_age_h": 24, "limit": 30,
     "title_suffix_source": True, "drop_description": True,
     "url": "https://news.google.com/rss/search?q=when:1d+site:reuters.com+markets+OR+economy&hl=en-US&gl=US&ceid=US:en"},
    {"name": "Associated Press", "category": "general", "max_age_h": 24, "limit": 20,
     "title_suffix_source": True, "drop_description": True,
     "url": "https://news.google.com/rss/search?q=when:1d+site:apnews.com+business+OR+economy&hl=en-US&gl=US&ceid=US:en"},
    {"name": "Yahoo Finance", "category": "general", "max_age_h": 24, "limit": 30,
     "url": "https://finance.yahoo.com/news/rssindex"},
    {"name": "Yahoo Finance S&P 500", "category": "general", "max_age_h": 24, "limit": 15,
     "url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US"},
    {"name": "CNBC", "category": "general", "max_age_h": 24, "limit": 25,
     "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"},
    {"name": "CNBC Economy", "category": "general", "max_age_h": 24, "limit": 25,
     "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"},
    {"name": "MarketWatch", "category": "general", "max_age_h": 24, "limit": 15,
     "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},

    # ── TIER 3: 실시간 속보 와이어 (가장 빠르지만 단편적) ──────────────────
    {"name": "FinancialJuice", "category": "wire", "max_age_h": 12, "limit": 40,
     "strip_prefix": "FinancialJuice:",
     "url": "https://www.financialjuice.com/feed.ashx?xy=rss"},
]

RSS_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MarketBriefBot/1.0)"}

# [SECURE] 허용 호스트를 위 URL 상수에서만 파생 - 리다이렉트 경유 SSRF 방지 (Category 1)
ALLOWED_HOSTS = httpguard.hosts_from_urls(
    [FINNHUB_BASE] + [feed["url"] for feed in RSS_FEEDS]
)

# 수집 텍스트 길이 상한. LLM 프롬프트와 저장 레코드의 상한(NewsRef)과 같은 값을 쓴다.
_HEADLINE_LIMIT = 300
_SUMMARY_LIMIT = 300
_SOURCE_LIMIT = 80

# 수집 상한. Finnhub 기업 뉴스는 종목당 수십 건이 쏟아져 RSS 슬롯을 잠식하므로
# 카테고리별 상한을 먼저 적용한 뒤 전체 상한을 건다.
PER_CATEGORY_CAP: dict[str, int] = {"company": 80, "crypto": 30, "merger": 30}
TOTAL_COLLECT_CAP = 400

# [SECURE] 응답 크기 상한 - 악성/손상 피드에 의한 파서 메모리 고갈 방지 (Category 1)
_MAX_FEED_BYTES = 5 * 1024 * 1024

# 개별 기업 뉴스를 별도로 수집할 핵심 종목
COMPANY_SYMBOLS = [
    "NVDA", "AAPL", "MSFT", "META", "GOOGL",
    "AMZN", "TSLA", "AMD", "INTC", "AVGO",
]


def finnhub_headers() -> dict[str, str]:
    """[SECURE] API 키를 요청 헤더로만 전달 - URL/로그 노출 방지 (Category 2)."""
    key = settings.finnhub_api_key.strip()
    return {"X-Finnhub-Token": key} if key else {}


async def _fetch_category(client: httpx.AsyncClient, category: str) -> list[dict]:
    async with asyncio.timeout(15):
        resp = await client.get(
            f"{FINNHUB_BASE}/news",
            params={"category": category},
            headers=finnhub_headers(),
        )
        resp.raise_for_status()
        # [SECURE] 외부 JSON 텍스트를 평탄화 후에만 사용 - 프롬프트 인젝션 방지 (Category 1)
        items = _normalize_ingested_list(resp.json())
        for item in items:
            item["_category"] = category
        return items


async def _fetch_company_news(client: httpx.AsyncClient, symbol: str) -> list[dict]:
    """특정 종목의 최근 24h 뉴스 수집."""
    now_utc = datetime.now(timezone.utc)
    yesterday = now_utc - timedelta(days=1)
    try:
        async with asyncio.timeout(15):
            resp = await client.get(
                f"{FINNHUB_BASE}/company-news",
                params={
                    "symbol": symbol,
                    "from": yesterday.strftime("%Y-%m-%d"),
                    "to": now_utc.strftime("%Y-%m-%d"),
                },
                headers=finnhub_headers(),
            )
            resp.raise_for_status()
            # [SECURE] 외부 JSON 텍스트를 평탄화 후에만 사용 - 프롬프트 인젝션 방지 (Category 1)
            items = _normalize_ingested_list(resp.json())
            for item in items:
                item["_category"] = "company"
                item["_symbol"] = symbol
            return items
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as e:
        logger.warning("[NewsCollector] %s 기업 뉴스 실패: %s", symbol, e)
        return []


def _parse_rss_date(date_str: str | None) -> int | None:
    """RSS 날짜 문자열 → Unix timestamp.

    RFC 2822 (`Mon, 10 Aug 2026 00:25:00 GMT`)와
    ISO 8601 (`2026-08-09T03:07:00Z`, Yahoo Finance 형식)을 모두 처리한다.
    """
    if not date_str:
        return None

    try:
        return int(parsedate_to_datetime(date_str).timestamp())
    except (TypeError, ValueError):
        pass  # ISO 8601 형식일 수 있으므로 아래에서 재시도

    try:
        normalized = date_str.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:  # [SECURE] naive datetime을 UTC로 고정 - 시각 오판 방지
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        logger.debug("[RSS] 날짜 파싱 실패: %r", date_str[:40])
        return None


def _strip_html(text: str) -> str:
    """단순 HTML 태그 제거 (LLM 입력 정제 및 다운스트림 XSS 표면 축소)."""
    clean = re.sub(r"<[^>]+>", " ", text or "")
    return " ".join(clean.split())


# 개행·탭·NUL 등 모든 C0 제어문자와 DEL. 프롬프트의 줄 구조를 위조하는 데 쓰인다.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _flatten(text: object, limit: int) -> str:
    """수집 텍스트를 **한 줄**로 평탄화한다.

    [SECURE] 제어문자 제거 + 공백 단일화 + 길이 상한 - 프롬프트 인젝션(구조 위조) 방지 (Category 1).
    뉴스 헤드라인·요약·출처명은 전량 제3자 입력이다. 개행이 살아 있으면
    `헤드라인\\n*** CRITICAL INSTRUCTION ***\\n...` 처럼 파이프라인이 실제로 쓰는
    지시문 블록을 위조해 LLM에게 새 명령으로 보이게 만들 수 있다.
    한 줄로 고정하면 아이템 텍스트는 프롬프트에서 항상 한 줄에 갇힌다.
    HTML 태그도 함께 제거해 다운스트림 XSS 표면을 줄인다.
    """
    clean = _CONTROL_RE.sub(" ", str(text or ""))
    clean = _strip_html(clean)  # 태그 제거 + 공백 단일화
    return clean[:limit] if limit > 0 else clean


def _normalize_ingested(item: object) -> dict | None:
    """수집 원본 아이템 1건 → 평탄화된 사본. dict가 아니면 None(폐기).

    [SECURE] 신뢰할 수 없는 외부 JSON 요소의 타입을 검증한 뒤에만 사용 (Category 1).
    """
    if not isinstance(item, dict):
        return None
    clean = dict(item)
    clean["headline"] = _flatten(clean.get("headline"), _HEADLINE_LIMIT)
    clean["summary"] = _flatten(clean.get("summary"), _SUMMARY_LIMIT)
    clean["source"] = _flatten(clean.get("source"), _SOURCE_LIMIT)
    return clean


def _normalize_ingested_list(items: object) -> list[dict]:
    """수집 응답 목록 → 평탄화된 dict 목록. 비-dict 요소는 조용히가 아니라 집계 후 로깅한다."""
    if not isinstance(items, list):
        return []
    result: list[dict] = []
    dropped = 0
    for item in items:
        clean = _normalize_ingested(item)
        if clean is None:
            dropped += 1
            continue
        result.append(clean)
    if dropped:
        logger.warning("[NewsCollector] dict가 아닌 응답 요소 %d건 폐기", dropped)
    return result


def _normalize_headline(headline: str, source: str) -> str:
    """제목 끝에 붙은 자기 출처명(`... - Reuters`)을 제거."""
    if not source or not headline:
        return headline
    suffix = f" - {source}"
    if headline.endswith(suffix) and len(headline) > len(suffix):
        return headline[: -len(suffix)].rstrip()
    return headline


def _split_title_source(title: str, default_source: str) -> tuple[str, str]:
    """Google News 형식 `기사 제목 - Reuters`에서 매체명을 분리."""
    if " - " not in title:
        return title, default_source
    head, _, tail = title.rpartition(" - ")
    tail = tail.strip()
    # 매체명은 짧다. 길면 제목의 일부이므로 분리하지 않는다.
    if head and 0 < len(tail) <= 40:
        return head.strip(), tail
    return title, default_source


def parse_rss_text(content: str, feed: dict[str, Any]) -> list[dict]:
    """RSS 본문 문자열 → Finnhub 호환 아이템 목록 (네트워크 없음, 테스트 가능)."""
    source_name = feed["name"]
    category = feed.get("category", "general")
    max_age_s = int(feed.get("max_age_h", 24)) * 3600
    limit = int(feed.get("limit", 30))
    strip_prefix = feed.get("strip_prefix", "")

    # [SECURE] defusedxml로 파싱 - XXE 및 엔티티 확장 폭탄 방지 (Category 1)
    root = DefusedET.fromstring(content)
    items_el = root.findall(".//item")
    results: list[dict] = []
    unparsed_dates = 0

    for item in items_el[:limit]:
        def txt(tag: str) -> str:
            el = item.find(tag)
            return (el.text or "").strip() if el is not None else ""

        # [SECURE] RSS 텍스트도 같은 평탄화를 거친다 - 프롬프트 인젝션 방지 (Category 1)
        headline = _flatten(txt("title"), _HEADLINE_LIMIT)
        if strip_prefix and headline.startswith(strip_prefix):
            headline = headline[len(strip_prefix):].strip()
        if not headline:
            continue

        item_source = source_name
        if feed.get("title_suffix_source"):
            headline, item_source = _split_title_source(headline, source_name)

        summary = "" if feed.get("drop_description") else _flatten(txt("description"), _SUMMARY_LIMIT)
        link = txt("link")
        ts = _parse_rss_date(txt("pubDate") or txt("pubdate"))
        if ts is None:
            unparsed_dates += 1

        results.append({
            "id":         link or headline,
            "headline":   _flatten(headline, _HEADLINE_LIMIT),
            "summary":    _flatten(summary, _SUMMARY_LIMIT),
            "datetime":   ts,
            "source":     _flatten(item_source, _SOURCE_LIMIT),
            "url":        link,
            "_category":  category,
            "_rss":       True,
            "_feed":      source_name,
            "_max_age_s": max_age_s,
        })

    if unparsed_dates:
        # 날짜가 없으면 신선도 필터에서 탈락한다. 피드 형식이 바뀐 것을 조용히 넘기지 않는다.
        logger.warning(
            "[RSS] %s: 날짜 파싱 실패 %d/%d건 - 신선도 필터에서 제외될 수 있음",
            source_name, unparsed_dates, len(results),
        )
    return results


async def _fetch_rss(client: httpx.AsyncClient, feed: dict[str, Any]) -> list[dict]:
    """RSS 피드 수집 + 파싱. 개별 실패는 빈 목록으로 격리한다."""
    source_name = feed["name"]
    try:
        async with asyncio.timeout(15):
            resp = await client.get(feed["url"], headers=RSS_HEADERS, follow_redirects=True)
            resp.raise_for_status()
            if len(resp.content) > _MAX_FEED_BYTES:  # [SECURE] 과대 응답 차단 (Category 1)
                logger.warning("[RSS] %s: 응답 크기 초과 - 건너뜀", source_name)
                return []
            content = resp.text

        results = parse_rss_text(content, feed)
        logger.info("[RSS] %s: %d건 수집", source_name, len(results))
        return results

    except (httpx.HTTPError, asyncio.TimeoutError, DefusedET.ParseError, ValueError) as e:
        # [SECURE] 개별 피드 실패가 전체 수집을 중단시키지 않도록 격리 (Category 4)
        logger.warning("[RSS] %s 수집 실패: %s: %s", source_name, type(e).__name__, e)
        return []


def dedupe_and_cap(all_items: list[dict], now: float | None = None) -> list[dict]:
    """신선도 필터 → 중복 제거 → 카테고리 상한 → 전체 상한 (순수 함수)."""
    ts_now = time.time() if now is None else now

    # 신선도 필터 — 피드별 max_age를 우선 적용 (공식 발표는 주기가 길어 더 넓게 허용)
    fresh = [
        item for item in all_items
        if ts_now - (item.get("datetime") or 0) <= item.get("_max_age_s", MAX_AGE_SECONDS)
    ]

    # 중복 제거 (id 기준, 없으면 headline 기준) + 제목 정규화
    seen: set[str] = set()
    unique: list[dict] = []
    for item in fresh:
        key = str(item.get("id") or item.get("headline", ""))
        if key and key not in seen:
            seen.add(key)
            item["headline"] = _normalize_headline(item.get("headline", ""), item.get("source", ""))
            unique.append(item)

    # 카테고리 상한 — 기업 뉴스 폭주가 공식·와이어 항목을 밀어내지 않도록 방지
    per_cat_count: dict[str, int] = {}
    capped: list[dict] = []
    for item in unique:
        cat = item.get("_category", "general")
        cap = PER_CATEGORY_CAP.get(cat)
        if cap is not None:
            if per_cat_count.get(cat, 0) >= cap:
                continue
            per_cat_count[cat] = per_cat_count.get(cat, 0) + 1
        capped.append(item)

    return capped[:TOTAL_COLLECT_CAP]


async def collect_all() -> list[dict[str, Any]]:
    """Finnhub(카테고리+기업) + 공식·통신사·와이어 RSS 병렬 수집 → 신선도 필터 + 중복 제거."""
    # [SECURE] AsyncClient를 context manager로 사용 - 소켓 누수 방지 (Category 5)
    # [SECURE] 응답 훅으로 리다이렉트 종착지 호스트까지 검증 - SSRF 방지 (Category 1)
    async with httpx.AsyncClient(
        timeout=20, event_hooks=httpguard.async_event_hooks(ALLOWED_HOSTS)
    ) as client:
        category_tasks = [_fetch_category(client, cat) for cat in CATEGORIES]
        company_tasks = [_fetch_company_news(client, sym) for sym in COMPANY_SYMBOLS]
        rss_tasks = [_fetch_rss(client, feed) for feed in RSS_FEEDS]

        all_results = await asyncio.gather(
            *category_tasks, *company_tasks, *rss_tasks,
            return_exceptions=True,
        )

    all_items: list[dict] = []
    n_finnhub = len(CATEGORIES) + len(COMPANY_SYMBOLS)

    for i, result in enumerate(all_results):
        if isinstance(result, BaseException):
            if i < len(CATEGORIES):
                label = CATEGORIES[i]
            elif i < n_finnhub:
                label = COMPANY_SYMBOLS[i - len(CATEGORIES)]
            else:
                label = RSS_FEEDS[i - n_finnhub]["name"]
            # [SECURE] 실패 상세는 로컬 로그에만 (Category 4)
            logger.warning("[NewsCollector] %s 수집 실패: %s", label, result)
            continue
        all_items.extend(result)

    result = dedupe_and_cap(all_items)

    official = sum(1 for x in result if x.get("_category") == "official")
    wire = sum(1 for x in result if x.get("_category") == "wire")
    rss = sum(1 for x in result if x.get("_rss"))
    logger.info(
        "[NewsCollector] 수집 %d건 → 신선도·상한 적용 후 %d건 (Finnhub %d / RSS %d — 공식 %d, 와이어 %d)",
        len(all_items), len(result), len(result) - rss, rss, official, wire,
    )
    return result


# --------------------------------------------------
# Security Checklist
# Applied:
#   - SSRF: 모든 외부 요청 URL을 소스 상수로 고정, 사용자 입력 URL 미사용.
#     추가로 httpguard 응답 훅이 리다이렉트 종착지의 스킴(https)·호스트(ALLOWED_HOSTS)를 검증한다
#   - 프롬프트 인젝션(구조 위조): Finnhub·RSS 모든 수집 경로의 headline/summary/source를 _flatten으로
#     제어문자 제거 + 한 줄 평탄화 + 길이 상한 처리한다. dict가 아닌 응답 요소는 폐기
#   - XXE: defusedxml.ElementTree 사용 - 외부 엔티티 및 엔티티 확장 폭탄 차단
#   - Unencrypted Sensitive Data: Finnhub 키를 X-Finnhub-Token 헤더로만 전송 (v1의 쿼리스트링 노출 제거)
#   - Sensitive Info in Logs: 요청 URL에 자격증명이 포함되지 않으므로 httpx 로그로 키가 새지 않음
#   - XSS (입력 정제): 수집 텍스트의 HTML 태그를 _strip_html로 제거
#   - 자원 고갈 방지: 응답 5MB 상한, 피드별 item 개수 제한, 카테고리·전체 상한
#   - Infinite Loop: 모든 요청에 asyncio.timeout(15s) + 클라이언트 타임아웃(20s)
#   - Missing Error Handling: 피드/심볼별 예외를 개별 격리하여 전체 파이프라인 중단 방지
#   - Improper Exception Handling: 빈 except 없음, 모든 실패 경로에서 로깅
#   - Improper Resource Release: httpx.AsyncClient를 async context manager로 사용
# Not Applied:
#   - [WARN] Improper Certificate Validation: httpx 기본 TLS 검증에 의존 (핀닝 없음).
#   - [WARN] 수집 텍스트는 LLM 입력 및 사이트 렌더 원본이 된다. 브라우저 출력 시점에
#     반드시 HTML 인코딩할 것 (mbrief/site 렌더러 책임).
# --------------------------------------------------
