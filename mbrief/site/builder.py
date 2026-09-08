"""정적 사이트 빌더.

`data/`(브리핑 레코드 · 시세 · 차트 · 스코어카드) + `web/`(정적 자산) + `templates/`(Jinja2)
→ `site/` (GitHub Pages로 그대로 올릴 수 있는 산출물).

설계 원칙
  1. **상대 경로만 사용한다.** GitHub 프로젝트 페이지는 `/<repo>/` 하위에 서빙되므로
     `/css/...` 같은 루트 절대 경로는 전부 깨진다. 템플릿에 `base`("./" 또는 "../")를 넘긴다.
  2. **모든 동적 문자열은 Jinja2 autoescape를 통과한다.** 예외는 이 모듈이 직접 만든 마크업
     (스파크라인·게이지 SVG, markdown.render_markdown_safe 결과)뿐이다.
  3. **비공개 필드는 공개 산출물에 절대 쓰지 않는다.** `settings.publish_portfolio_note`가 꺼져 있으면
     포트폴리오 파트(portfolioNote)를 깊은 복사본에서 제거한 뒤에야 직렬화한다.
     제거 기준은 `mbrief.llm.prompts.strip_portfolio_section()`이 단일 정의처이며,
     빌더는 import 실패 시에만 동등 범위의 로컬 폴백을 쓴다. 입력 레코드는 변형하지 않는다.
  4. **신규 필드는 항상 부재를 허용한다.** v2.1의 sessionRecap / keyActors, v2.2의
     sectorHeat / sectorValuation은 구버전 레코드에 아예 없으므로 뷰모델이 None / 빈 리스트를
     돌려주고 템플릿은 섹션 자체를 그리지 않는다.
  5. **섹터 과열도는 밸류에이션 판정이 아니다.** gapPercentile(그 섹터 자체의 과거 이격 분포에서
     오늘의 위치) 하나로 등급이 정해지며, 빌더는 상류가 내린 등급을 표시만 한다. 밸류에이션 블록은
     pePercentile이 없으면 '기록 중'만 보여주고 어떤 평가 결론도 만들지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from mbrief import paths as _paths
from mbrief.config import settings as default_settings
from mbrief.paths import ROOT, SITE_DIR, TEMPLATES_DIR, WEB_DIR
from mbrief.schemas import (
    BriefingRecord,
    Direction,
    IndexEntry,
    MarketBundle,
    Scorecard,
    SiteIndex,
)
from mbrief.site import i18n
from mbrief.site.markdown import inline_markdown_safe, render_markdown_safe
# 식별자 화이트리스트는 저장소(store)와 반드시 같아야 한다. 별도 정의하면 느슨한 쪽이 취약점이 된다.
from mbrief.sources.market_data import SYMBOL_KEYS
from mbrief.store import CHART_KEY_RE, RECORD_ID_RE
from mbrief.timeutil import parse_iso

try:
    # 포트폴리오 파트 표식의 **단일 정의처**는 프롬프트 모듈이다 (파트 번호·다국어 제목을 소유).
    # 빌더가 별도 정규식을 유지하면 두 기준이 갈라져 느슨한 쪽이 유출 경로가 된다.
    from mbrief.llm import prompts as _prompts
except Exception:  # pragma: no cover - 순환/부분 설치 대비. 아래 로컬 정규식으로 폴백한다
    # [SECURE] import 실패를 조용히 무시하지 않고 폴백 상태를 명시 보관 (Category 4)
    _prompts = None

logger = logging.getLogger(__name__)

__all__ = [
    "BuildResult",
    "build",
    "build_from_data",
    "strip_portfolio_markdown",
    "strip_part7_markdown",
    "public_record",
    "SYMBOL_SLUGS",
    "CHART_KEY_RE",
    "RECORD_ID_RE",
]


# ===== 상수 =====

#: 시세 표시명 → 차트 파일 슬러그. `data/charts/<slug>_<period>.json`
#: market_data.SYMBOL_KEYS가 단일 정의처이며 여기서는 재노출만 한다 (슬러그 표기 이원화 방지).
SYMBOL_SLUGS: dict[str, str] = SYMBOL_KEYS

#: 카드 노출 순서 (지수 → 금리/달러 → 원자재/변동성 → 통화/코인 → 국내)
CARD_ORDER: tuple[str, ...] = tuple(SYMBOL_SLUGS.keys())

#: 원화 환산 대상 (지수 포인트는 통화 단위가 아니므로 제외)
KRW_CONVERTIBLE: frozenset[str] = frozenset({"Crude Oil (WTI)", "Gold", "BTC/USD"})

CHART_PERIODS: tuple[str, ...] = ("1d", "5d", "1mo")
# [SECURE] 스타일 속성에 들어가는 색상값 검증 - CSS/속성 인젝션 방지 (Category 1)
HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
# [SECURE] 링크 스킴 화이트리스트 - javascript:/data: 링크 및 Open Redirect 방지 (Category 1)
HTTP_URL_RE = re.compile(r"^https?://[^\s<>\"']{4,2000}$")

PROVIDER_LABELS: dict[str, str] = {
    "claude_cli": "Claude CLI",
    "claude": "Claude CLI",
    "gemini": "Gemini",
    "openai": "OpenAI",
    "anthropic": "Anthropic API",
    "mock": "Mock",
}

_FG_ZONES: tuple[tuple[float, str, str], ...] = (
    (25.0, "fgRatingExtremeFear", "#ef4444"),
    (45.0, "fgRatingFear", "#f97316"),
    (55.0, "fgRatingNeutral", "#eab308"),
    (75.0, "fgRatingGreed", "#84cc16"),
    (100.1, "fgRatingExtremeGreed", "#22c55e"),
)

#: 섹터 과열도 등급 → 라벨 키. 등급 판정은 상류(indicators)가 gapPercentile 하나로만 내리며
#: 빌더는 그 결과를 표시만 한다 (여기서 재판정하면 기준이 이원화된다).
HEAT_LABEL_KEYS: dict[str, str] = {
    "hot": "heatHot",
    "warm": "heatWarm",
    "neutral": "heatNeutral",
    "cool": "heatCool",
    "cold": "heatCold",
}

#: site/data/sector_valuation.json 로 옮길 수 있는 원본 파일의 최대 크기.
#: 누적 기록이라 무한정 커질 수 있고, 정적 사이트에 통째로 실리면 첫 로딩을 망친다(가용성).
VALUATION_HISTORY_MAX_BYTES = 8 * 1024 * 1024

_RFC822_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_RFC822_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

# PART 2 확률 바 (텍스트 폴백 렌더 전용) — 4개 언어 라벨 대응
_PROB_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(📈\s*(?:상승|Bullish|Up|上涨|上昇)\s*[:：]\s*)(\d{1,3})%", re.I), "up"),
    (re.compile(r"(➡️\s*(?:횡보|Neutral|Flat|震荡|横ばい)\s*[:：]\s*)(\d{1,3})%", re.I), "neutral"),
    (re.compile(r"(📉\s*(?:하락|Bearish|Down|下跌|下落)\s*[:：]\s*)(\d{1,3})%", re.I), "down"),
)

# 비공개 포트폴리오 파트 제거용 **폴백** 정규식.
# 정상 경로에서는 mbrief.llm.prompts.strip_portfolio_section()이 단일 정의처다. 아래 두 정규식은
# 그 import가 실패했을 때만 쓰이며, 동작이 갈라지지 않도록 prompts 쪽 규칙을 그대로 미러링한다
# (PORTFOLIO_SECTION_RE / _DASHBOARD_TITLE_RE와 동기화 대상이다).
# 모델이 `## PART 8`, `**PART 7 — 포트폴리오**`, `パート8`, `第8部` 등 어떤 표기를 쓰더라도 잡아야 하므로
# 제목 마크업을 요구하지 않고 본문 어디에서든 표식을 찾는다. 포트폴리오는 계약상 마지막 파트이므로
# 표식이 처음 등장한 줄부터 EOF까지 통째로 잘라낸다.
# 파트 번호가 7(구 계약)에서 8(v2.1 계약)로 바뀌었으므로 두 번호를 모두 잡되,
# v2.1의 공개 파트인 PART 7(10초 마켓 대시보드)은 제목으로 식별해 예외 처리한다.
_PORTFOLIO_MARKER_RE = re.compile(
    r"PART\s*[78]\b|포트폴리오\s*맞춤|파트\s*[78]|パート\s*[78]|第\s*[78]\s*部|Portfolio\s+Advice",
    re.I,
)
# [SECURE] 오탐 차단: 대시보드 제목이 확인되는 줄은 비공개 파트 시작으로 보지 않는다 (가용성 방어)
_DASHBOARD_TITLE_RE = re.compile(r"대시보드|ダッシュボード|仪表盘|儀表板|Dashboard", re.I)

# 인물 카드의 관련 자산 티커가 시장 스냅샷 12심볼과 겹치는지 판정하기 위한 키 집합.
# 표기 흔들림(`^TNX`, `BTC/USD`, `S&P 500`)을 흡수하려고 영숫자만 남겨 정규화한다.
_TICKER_KEY_RE = re.compile(r"[^A-Z0-9]")


def _ticker_key(text: object) -> str:
    return _TICKER_KEY_RE.sub("", str(text or "").upper())


#: SYMBOL_KEYS(표시명·슬러그)에 흔한 티커 별칭을 더한 집합. 매칭돼도 링크는 만들지 않고
#: 시각적 강조(pill 강조)만 준다 — 외부 이동 경로를 늘리지 않기 위함이다.
MARKET_TICKER_KEYS: frozenset[str] = frozenset(
    {_ticker_key(name) for name in SYMBOL_KEYS}
    | {_ticker_key(slug) for slug in SYMBOL_KEYS.values()}
    | {
        _ticker_key(alias)
        for alias in (
            # 같은 지수·상품을 가리키는 표기만 넣는다 (단순 연관 종목은 제외 — 강조가 거짓말이 된다)
            "SPX", "^GSPC", "SPY", "ES=F",          # S&P 500
            "^IXIC", "NDX", "QQQ",                  # NASDAQ
            "^DJI", "DIA",                          # Dow Jones
            "^TNX", "TNX", "US10Y",                 # US 10Y Yield
            "^DXY", "UUP",                          # Dollar Index
            "CL=F", "USO",                          # Crude Oil (WTI)
            "^VIX",                                 # VIX
            "GC=F", "GLD", "XAUUSD",                # Gold
            "BTC-USD", "XBTUSD",                    # BTC/USD
            "^KS11",                                # KOSPI
            "KRW=X",                                # USD/KRW
        )
    }
) - {""}


@dataclass(frozen=True)
class BuildResult:
    out_dir: Path
    pages: int
    records: int
    build_id: str


# ===== 유틸 =====


def _now() -> datetime:
    return datetime.now().astimezone()


def _rfc822(iso: str | None, fallback: datetime | None = None) -> str:
    """RSS pubDate. 로케일 의존을 피하려고 영문 약어를 직접 조립한다."""
    dt = None
    if iso:
        try:
            dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        except ValueError:
            dt = None
    if dt is None:
        dt = fallback or _now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    offset = dt.utcoffset() or timedelta(0)
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return (
        f"{_RFC822_DAYS[dt.weekday()]}, {dt.day:02d} {_RFC822_MONTHS[dt.month - 1]} {dt.year} "
        f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} {sign}{total // 3600:02d}{(total % 3600) // 60:02d}"
    )


def _safe_url(url: object) -> str:
    """http(s)만 통과시킨다. 그 외에는 빈 문자열(링크 미생성)."""
    text = str(url or "").strip()
    return text if HTTP_URL_RE.match(text) else ""


def _safe_color(value: object, fallback: str) -> str:
    text = str(value or "").strip()
    return text if HEX_COLOR_RE.match(text) else fallback


def _fmt_price(value: float | None, name: str = "") -> str:
    """v1 formatNumber와 동일 규칙 (클라이언트 통화 전환과 표기를 맞추기 위함)."""
    if value is None:
        return "N/A"
    lower = name.lower()
    if any(k in lower for k in ("yield", "vix", "dxy", "eur/usd")):
        return f"{value:.2f}"
    if value >= 1_000_000:
        return f"{round(value):,}"
    if value >= 100:
        return f"{value:,.2f}"
    return f"{value:.2f}"


def _fmt_signed(value: float | None, digits: int = 2) -> str:
    if value is None:
        return ""
    return f"{value:+,.{digits}f}"


def _fmt_pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return ""
    return f"{value:+.{digits}f}%"


def _iter_dicts(raw: object) -> list[dict]:
    """캘린더 등 느슨한 dict 필드에서 dict 목록만 안전하게 뽑는다."""
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


# ===== SVG (서버 사이드 사전 렌더) =====


def sparkline_svg(candles: list[dict] | None, *, width: int = 120, height: int = 30) -> Markup:
    """1D 캔들 종가로 만든 인라인 스파크라인. 데이터가 부족하면 빈 마크업."""
    if not candles or len(candles) < 3:
        return Markup("")
    prices: list[float] = []
    for c in candles:
        close = c.get("close") if isinstance(c, dict) else None
        if isinstance(close, (int, float)):
            prices.append(float(close))
    if len(prices) < 3:
        return Markup("")

    lo, hi = min(prices), max(prices)
    span = (hi - lo) or 1.0
    step = width / (len(prices) - 1)
    pts = " ".join(
        f"{i * step:.1f},{height - ((p - lo) / span) * (height - 3) - 1.5:.1f}"
        for i, p in enumerate(prices)
    )
    up = prices[-1] >= prices[0]
    color = "#10b981" if up else "#ef4444"
    # 좌표는 전부 우리가 계산한 숫자다. 외부 문자열이 섞이지 않으므로 Markup으로 신뢰 가능.
    return Markup(
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'preserveAspectRatio="none" aria-hidden="true" focusable="false">'
        f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/></svg>'
    )


def gauge_svg(score: float, active_color: str) -> Markup:
    """공포·탐욕 게이지 (v1 _buildGaugeSVG의 파이썬 포팅). viewBox 0 0 200 132."""
    import math

    score = max(0.0, min(100.0, float(score)))
    color = _safe_color(active_color, "#eab308")
    cx, cy, r, sw = 100.0, 100.0, 74.0, 14.0

    def pt(s: float, rad: float = r) -> tuple[float, float]:
        ang = math.radians(180 - s * 1.8)
        return cx + rad * math.cos(ang), cy - rad * math.sin(ang)

    zone_colors = ("#ef4444", "#f97316", "#eab308", "#84cc16", "#22c55e")
    parts: list[str] = []

    # 0점(왼쪽) → 100점(오른쪽)으로 원 위쪽을 지나는 호. SVG는 y축이 아래로 향하므로
    # 이 진행 방향은 "양의 각도 방향"이고 sweep-flag=1이다. (v1 JS는 0을 써서 반대쪽
    # 원으로 그려졌고 게이지가 꽃잎 모양으로 깨졌다.) 각 구간은 36도이므로 large-arc는 항상 0.
    for i in range(5):
        x1, y1 = pt(i * 20 + 1.2)
        x2, y2 = pt((i + 1) * 20 - 1.2)
        parts.append(
            f'<path d="M{x1:.2f},{y1:.2f} A{r:.0f},{r:.0f} 0 0,1 {x2:.2f},{y2:.2f}" '
            f'stroke="{zone_colors[i]}" stroke-width="{sw:.0f}" fill="none" '
            f'stroke-linecap="round" opacity="0.32"/>'
        )

    if score >= 2:
        x0, y0 = pt(0.5)
        xs, ys = pt(min(score, 99.5))
        # 0~100점이 180도에 대응하므로 호의 사잇각은 결코 180도를 넘지 않는다 → large-arc=0
        parts.append(
            f'<path d="M{x0:.2f},{y0:.2f} A{r:.0f},{r:.0f} 0 0,1 {xs:.2f},{ys:.2f}" '
            f'stroke="{color}" stroke-width="{sw - 6:.0f}" fill="none" '
            f'stroke-linecap="round" opacity="0.6"/>'
        )

    nx, ny = pt(score, r - sw / 2 - 1)
    parts.append(
        f'<line x1="{cx:.0f}" y1="{cy:.0f}" x2="{nx:.2f}" y2="{ny:.2f}" '
        f'stroke="#f8fafc" stroke-width="2.5" stroke-linecap="round"/>'
    )
    parts.append(f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="4" fill="#f8fafc"/>')
    lx, _ = pt(1)
    rx, _ = pt(99)
    parts.append(
        f'<text x="{lx - 2:.0f}" y="{cy + 15:.0f}" text-anchor="middle" fill="#ef4444" '
        f'font-size="8.5" font-family="Inter,sans-serif">FEAR</text>'
    )
    parts.append(
        f'<text x="{rx + 2:.0f}" y="{cy + 15:.0f}" text-anchor="middle" fill="#22c55e" '
        f'font-size="8.5" font-family="Inter,sans-serif">GREED</text>'
    )
    return Markup("".join(parts))


# ===== 뷰모델 =====


def _clean_inline(value: object, limit: int) -> str:
    """[SECURE] 표시 문자열 길이 상한 + 공백 단일화 (Category 1: Input Validation).

    인물명·직함·티커는 전량 LLM 출력이다. 이스케이프는 Jinja autoescape가 하지만,
    길이를 제한하지 않으면 한 필드가 레이아웃을 통째로 밀어내 다른 정보를 가린다(가용성).
    """
    return " ".join(str(value or "").split())[:limit]


def _recap_view(structured: Any, labels: dict[str, str]) -> dict | None:
    """PART 1 전날 세션 리캡 뷰모델.

    sessionRecap은 v2.1에서 추가된 선택 필드다. 구버전 레코드에는 아예 없으므로
    `None`/빈 값이면 None을 돌려주고 템플릿은 섹션 자체를 그리지 않는다.
    """
    if structured is None:
        return None
    recap = getattr(structured, "sessionRecap", None)
    if recap is None:
        return None

    summary = str(getattr(recap, "summary", "") or "").strip()
    drivers = [
        text
        for text in (_clean_inline(d, 300) for d in (getattr(recap, "drivers", None) or []))
        if text
    ][:6]
    if not summary and not drivers:
        return None
    return {
        # render_markdown_safe는 `md` 필터와 같은 구현이다 (이스케이프 후 제한 문법만 복원)
        "summary_html": Markup(render_markdown_safe(summary)) if summary else Markup(""),
        "drivers": drivers,
        "drivers_label": labels["recapDrivers"],
    }


def _actors_view(structured: Any, labels: dict[str, str]) -> list[dict]:
    """PART 2 핵심 인물 카드 뷰모델. 필드가 없거나 빈 목록이면 빈 리스트."""
    if structured is None:
        return []
    rows: list[dict] = []
    for actor in (getattr(structured, "keyActors", None) or [])[:6]:
        name = _clean_inline(getattr(actor, "name", ""), 60)
        if not name:
            continue  # 이름 없는 카드는 정보가 없다
        assets: list[dict] = []
        seen: set[str] = set()
        for raw in (getattr(actor, "assets", None) or []):
            label = _clean_inline(raw, 24)
            key = _ticker_key(label)
            if not label or not key or key in seen:
                continue
            seen.add(key)
            assets.append({"label": label, "known": key in MARKET_TICKER_KEYS})
            if len(assets) >= 8:
                break
        rows.append(
            {
                "name": name,
                "role": _clean_inline(getattr(actor, "role", ""), 80),
                "action": str(getattr(actor, "action", "") or "").strip(),
                "impact": str(getattr(actor, "impact", "") or "").strip(),
                "impact_label": labels["actorImpact"],
                "assets": assets,
                "assets_label": labels["actorAssets"],
                # 관심종목 렌즈가 매칭 결과를 그룹으로 묶을 때 쓰는 표시명.
                # 티커는 카드 본문(textContent)에 이미 있으므로 여기 넣지 않는다.
                "lens": name,
            }
        )
    return rows


def _direction_view(direction: Direction | None, labels: dict[str, str]) -> dict | None:
    if direction is None:
        return None
    call = direction.call
    return {
        "up": direction.up,
        "flat": direction.flat,
        "down": direction.down,
        "call": call,
        "call_label": labels.get({"up": "dirUp", "flat": "dirFlat", "down": "dirDown"}[call], call),
        "rows": [
            {"key": "up", "label": labels["dirUp"], "pct": direction.up,
             "rationale": direction.rationaleUp, "active": call == "up"},
            {"key": "flat", "label": labels["dirFlat"], "pct": direction.flat,
             "rationale": direction.rationaleFlat, "active": call == "flat"},
            {"key": "down", "label": labels["dirDown"], "pct": direction.down,
             "rationale": direction.rationaleDown, "active": call == "down"},
        ],
    }


def _market_cards(
    market: MarketBundle | None,
    charts: dict[str, list[dict]],
) -> list[dict]:
    if market is None or not market.quotes:
        return []
    names = [n for n in CARD_ORDER if n in market.quotes]
    names += [n for n in market.quotes if n not in SYMBOL_SLUGS]

    cards: list[dict] = []
    for name in names:
        quote = market.quotes[name]
        slug = SYMBOL_SLUGS.get(name, "")
        # 실제로 데이터가 있는 기간만 노출한다 (1d,5d,1mo 순). 없는 기간 탭은 클라이언트가 비활성화한다.
        periods = [p for p in CHART_PERIODS if slug and f"{slug}_{p}" in charts]
        change = quote.change
        pct = quote.changePercent
        up = (change if change is not None else (pct or 0.0)) >= 0
        cards.append(
            {
                "name": name,
                "slug": slug,
                "price": quote.price,
                "price_text": _fmt_price(quote.price, name),
                "change": change,
                "change_text": _fmt_signed(change),
                "pct_text": _fmt_pct(pct),
                "dir": "up" if up else "down",
                "convertible": name in KRW_CONVERTIBLE,
                "has_chart": bool(periods),
                "chart_periods": ",".join(periods),
                "spark": sparkline_svg(charts.get(f"{slug}_1d")) if slug else Markup(""),
            }
        )
    return cards


def _sector_chips(market: MarketBundle | None) -> list[dict]:
    if market is None or not market.sectors:
        return []
    rows = [
        {
            "name": name,
            "symbol": sector.symbol,
            "pct": sector.changePercent,
            "pct_text": _fmt_pct(sector.changePercent),
            "dir": "up" if (sector.changePercent or 0.0) >= 0 else "down",
        }
        for name, sector in market.sectors.items()
    ]
    rows.sort(key=lambda r: (r["pct"] is None, -(r["pct"] or 0.0)))
    return rows


def _fmt_points(value: float | None, digits: int = 1) -> str:
    """%p(퍼센트 포인트) 표기. 초과수익률은 비율이 아니라 차이라서 단위가 다르다."""
    if value is None:
        return ""
    return f"{value:+.{digits}f}%p"


def _fmt_plain(value: float | None, digits: int = 2) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def _sector_heat_view(market: MarketBundle | None, labels: dict[str, str]) -> list[dict]:
    """섹터 과열도(기술적 스트레치) 뷰모델. v2.2 신규 필드라 구버전 레코드에는 아예 없다.

    밸류에이션이 아니다 — 가격만으로 "얼마나 달아올랐나"를 본다. 판정축은 gapPercentile 하나이며
    rsi14/rel3m은 참고 수치로만 나란히 표시한다. 정렬은 백분위 내림차순(없으면 뒤)이라
    가장 달아오른 섹터가 먼저 읽힌다.
    """
    heat = getattr(market, "sectorHeat", None) if market is not None else None
    if not heat:
        return []

    rows: list[dict] = []
    for name, item in heat.items():
        display = _clean_inline(getattr(item, "name", "") or name, 48)
        if not display:
            continue
        pctile = getattr(item, "gapPercentile", None)
        label = str(getattr(item, "label", "") or "neutral")
        if label not in HEAT_LABEL_KEYS:
            # [SECURE] 등급 문자열 화이트리스트 - 클래스명·라벨 조회에 예상 밖 값이 섞이지 않게 한다
            label = "neutral"
        sample_days = getattr(item, "sampleDays", 0)
        rows.append(
            {
                "name": display,
                "symbol": _clean_inline(getattr(item, "symbol", ""), 12),
                "label": label,
                "label_text": labels[HEAT_LABEL_KEYS[label]],
                "percentile": pctile,
                # 인라인 style 폭으로 들어가는 값 — 0~100 정수로 고정한다
                "bar": 0 if pctile is None else max(0, min(100, int(round(float(pctile))))),
                "percentile_text": "—" if pctile is None else f"{float(pctile):.0f}",
                "gap_text": _fmt_pct(getattr(item, "gapPct", None), 1) or "—",
                "rsi_text": _fmt_plain(getattr(item, "rsi14", None), 1) or "—",
                "rel3m_text": _fmt_points(getattr(item, "rel3m", None)) or "—",
                "sample_text": (
                    labels["heatSampleDays"].format(n=int(sample_days))
                    if isinstance(sample_days, (int, float)) and sample_days > 0
                    else ""
                ),
            }
        )
    rows.sort(key=lambda r: (r["percentile"] is None, -(r["percentile"] or 0.0), r["name"]))
    return rows


def _sector_valuation_view(market: MarketBundle | None, labels: dict[str, str]) -> list[dict]:
    """섹터 밸류에이션 스냅샷 뷰모델. v2.2 신규 필드라 구버전 레코드에는 아예 없다.

    현재 PER/PBR은 매일 받을 수 있지만 그 수치가 높은지 낮은지는 자체 기록이 쌓여야 말할 수 있다.
    pePercentile이 None이면 백분위 대신 '기록 중 (N일)' 진행 상태만 보여준다 —
    근거 없는 평가 판정을 만들지 않기 위한 계약이다.
    """
    valuation = getattr(market, "sectorValuation", None) if market is not None else None
    if not valuation:
        return []

    rows: list[dict] = []
    for name, item in valuation.items():
        display = _clean_inline(getattr(item, "name", "") or name, 48)
        if not display:
            continue
        pctile = getattr(item, "pePercentile", None)
        recorded = getattr(item, "recordedDays", 0)
        recorded = int(recorded) if isinstance(recorded, (int, float)) and recorded > 0 else 0
        dividend = getattr(item, "dividendYield", None)
        rows.append(
            {
                "name": display,
                "symbol": _clean_inline(getattr(item, "symbol", ""), 12),
                "per_text": _fmt_plain(getattr(item, "trailingPE", None)) or "—",
                "pbr_text": _fmt_plain(getattr(item, "priceToBook", None)) or "—",
                "yield_text": "—" if dividend is None else f"{float(dividend) * 100:.2f}%",
                # 백분위가 없으면 판정을 만들지 않는다. 진행 상태만 노출한다.
                "accruing": pctile is None,
                "percentile_text": "" if pctile is None else f"{float(pctile):.0f}",
                "bar": 0 if pctile is None else max(0, min(100, int(round(float(pctile))))),
                "accruing_text": (
                    labels["valuationAccruingDays"].format(n=recorded)
                    if recorded > 0
                    else labels["valuationAccruing"]
                ),
                "recorded_days": recorded,
            }
        )
    return rows


def _fear_greed_view(market: MarketBundle | None, labels: dict[str, str], lang: str) -> dict | None:
    if market is None or market.fearGreed is None:
        return None
    fg = market.fearGreed
    score = max(0.0, min(100.0, float(fg.score)))

    key, zone_color = "fgRatingNeutral", "#eab308"
    for upper, label_key, col in _FG_ZONES:
        if score < upper:
            key, zone_color = label_key, col
            break
    raw_rating = (fg.rating or "").strip().lower()
    rating_map = {
        "extreme fear": "fgRatingExtremeFear",
        "fear": "fgRatingFear",
        "neutral": "fgRatingNeutral",
        "greed": "fgRatingGreed",
        "extreme greed": "fgRatingExtremeGreed",
    }
    key = rating_map.get(raw_rating, key)
    color = _safe_color(fg.color, zone_color)

    def delta(ref: float | None) -> dict:
        val = None if ref is None else round(float(ref), 1)
        if val is None:
            return {"value": None, "arrow": "—", "color": "#94a3b8", "width": 0}
        if score > val:
            arrow, col = "▲", "#22c55e"
        elif score < val:
            arrow, col = "▼", "#ef4444"
        else:
            arrow, col = "—", "#94a3b8"
        return {"value": val, "arrow": arrow, "color": col, "width": max(0, min(100, int(val)))}

    return {
        "score": round(score),
        "rating": labels.get(key, fg.rating),
        "color": color,
        "gauge": gauge_svg(score, color),
        "rows": [
            {"label": labels["fgPrevClose"], **delta(fg.previous_close)},
            {"label": labels["fgPrev1w"], **delta(fg.previous_1week)},
            {"label": labels["fgPrev1m"], **delta(fg.previous_1month)},
        ],
        "timestamp": i18n.format_datetime(fg.timestamp, lang) if fg.timestamp else "",
    }


def _fomc_range(item: dict) -> str:
    start, end = str(item.get("start") or ""), str(item.get("end") or "")
    if not start:
        return ""
    if len(end) >= 10 and start[5:7] != end[5:7]:
        return f"{start[5:7]}/{start[8:10]}-{end[5:7]}/{end[8:10]}"
    if len(end) >= 10:
        return f"{start[5:7]}/{start[8:10]}-{end[8:10]}"
    return f"{start[5:7]}/{start[8:10]}"


def _calendar_view(
    market: MarketBundle | None, labels: dict[str, str], lang: str, ref_date: str
) -> dict:
    empty = {"fomc": None, "indicators": [], "days": []}
    if market is None or not isinstance(market.calendar, dict):
        return empty
    cal = market.calendar

    # --- FOMC ---
    fomc_raw = _iter_dicts(cal.get("fomc"))
    fomc_view = None
    if fomc_raw:
        upcoming = [m for m in fomc_raw if not m.get("isPast")]
        past = [m for m in fomc_raw if m.get("isPast")]
        nxt = upcoming[0] if upcoming else None
        chips = []
        for m in fomc_raw[:6]:
            if m.get("isLive"):
                badge, cls = labels["fomcLive"], "live"
            elif m.get("isPast"):
                badge, cls = labels["fomcDone"], "past"
            else:
                days = m.get("daysUntil")
                badge = f"D-{int(days)}" if isinstance(days, (int, float)) else labels["fomcNext"]
                cls = "next" if nxt is not None and m.get("start") == nxt.get("start") else ""
            chips.append(
                {
                    "range": _fomc_range(m),
                    "badge": badge,
                    "cls": cls,
                    "sep": bool(m.get("hasSEP")),
                }
            )
        lead = None
        if nxt is not None:
            days = nxt.get("daysUntil")
            lead = {
                "label": labels["fomcNext"],
                "dday": f"D-{int(days)}" if isinstance(days, (int, float)) else "",
                "note": str(nxt.get("note") or ""),
            }
        elif past:
            lead = {"label": labels["fomcLast"], "dday": "", "note": _fomc_range(past[-1])}
        fomc_view = {"chips": chips, "lead": lead}

    # --- BLS 지표 ---
    indicators = []
    for ind in _iter_dicts(cal.get("indicators")):
        display = ind.get("display")
        if isinstance(display, (int, float)):
            sign = "+" if ind.get("isDelta") and display > 0 else ""
            value_text = f"{sign}{display}{ind.get('displayUnit') or ''}"
        else:
            value_text = "—"
        direction = str(ind.get("direction") or "flat")
        indicators.append(
            {
                "label": str(ind.get("label") or ""),
                "value_text": value_text,
                "dir": direction if direction in ("up", "down") else "flat",
                "period": str(ind.get("period") or ""),
                "change_note": str(ind.get("changeNote") or ""),
                "high": ind.get("impact") == "high",
            }
        )

    # --- 경제 이벤트 + 어닝을 날짜별로 묶는다 ---
    impact_icon = {"high": "🔴", "medium": "🟡", "low": "⚪"}
    hour_label = {"bmo": labels["earnBmo"], "amc": labels["earnAmc"], "dmh": labels["earnDmh"]}
    by_date: dict[str, dict[str, list]] = {}

    def bucket(key: str) -> dict[str, list]:
        return by_date.setdefault(key, {"economic": [], "earnings": []})

    for ev in _iter_dicts(cal.get("economic")):
        key = str(ev.get("time") or "")[:10] or labels["calTbd"]
        bucket(key)["economic"].append(
            {
                "icon": impact_icon.get(str(ev.get("impact")), "⚪"),
                "name": f"{ev.get('country') or ''} {ev.get('event') or ''}".strip(),
                "time": str(ev.get("time") or "")[11:16],
            }
        )
    for ev in _iter_dicts(cal.get("earnings")):
        key = str(ev.get("date") or "") or labels["calTbd"]
        bucket(key)["earnings"].append(
            {
                "symbol": str(ev.get("symbol") or ev.get("company") or "?"),
                "when": hour_label.get(str(ev.get("hour") or "").lower(), ""),
                "major": bool(ev.get("isMajor")),
            }
        )

    try:
        ref = date.fromisoformat(str(ref_date)[:10])
    except ValueError:
        ref = _now().date()

    days = []
    for key in sorted(by_date):
        if key == labels["calTbd"]:
            label, is_today = labels["calTbd"], False
        else:
            try:
                d = date.fromisoformat(key)
            except ValueError:
                label, is_today = key, False
            else:
                diff = (d - ref).days
                short = i18n.format_short_date(key, lang)
                if diff == 0:
                    label, is_today = f"{labels['calToday']} · {short}", True
                elif diff == 1:
                    label, is_today = f"{labels['calTomorrow']} · {short}", False
                else:
                    label, is_today = short, False
        days.append({"label": label, "today": is_today, **by_date[key]})

    return {"fomc": fomc_view, "indicators": indicators, "days": days}


def _news_view(record: BriefingRecord, labels: dict[str, str]) -> list[dict]:
    tier_labels = {1: labels["newsTier1"], 2: labels["newsTier2"], 3: labels["newsTier3"]}
    rows = []
    for item in record.topNews[:12]:
        tier = item.tier if item.tier in (1, 2, 3) else 3
        rows.append(
            {
                "headline": item.headline,
                "source": item.source,
                "time": item.time,
                "tier": tier,
                "tier_label": tier_labels[tier],
                # [SECURE] http(s) 스킴만 링크 - javascript: 링크 및 Open Redirect 방지 (Category 1)
                "url": _safe_url(item.url),
            }
        )
    return rows


def _scorecard_view(
    scorecard: Scorecard | None, labels: dict[str, str], lang: str
) -> dict | None:
    if scorecard is None or scorecard.sampleSize <= 0 or not scorecard.entries:
        return None
    hit_rate = round((scorecard.hitRate or 0.0) * 100)
    brier = scorecard.avgBrier
    summary = labels["scoreSummary"].format(
        n=scorecard.sampleSize,
        rate=hit_rate,
        brier=f"{brier:.2f}" if isinstance(brier, (int, float)) else "—",
    )
    call_labels = {"up": labels["dirUp"], "flat": labels["dirFlat"], "down": labels["dirDown"]}
    dots = []
    for entry in list(scorecard.entries[:12])[::-1]:  # 최신순 → 시간순
        pct = getattr(entry.probs, entry.predicted, 0)
        dots.append(
            {
                "hit": entry.hit,
                "state": labels["scoreHit"] if entry.hit else labels["scoreMiss"],
                "tooltip": labels["scoreTooltip"].format(
                    date=i18n.format_short_date(entry.date, lang),
                    call=call_labels.get(entry.predicted, entry.predicted),
                    pct=pct,
                    actual=_fmt_pct(entry.spxChangePercent),
                ),
            }
        )
    return {
        "summary": summary,
        "note": labels["scoreNote"],
        "dots": dots,
        "hit_rate": hit_rate,
        "updated": i18n.format_datetime(scorecard.updatedAt, lang),
    }


def _fallback_body_html(markdown_text: str) -> Markup:
    """구조화 출력이 없을 때의 본문. 안전 렌더 후 PART 2 확률 바만 덧입힌다."""
    html = render_markdown_safe(markdown_text)
    if not html:
        return Markup("")
    for pattern, kind in _PROB_PATTERNS:
        html = pattern.sub(
            lambda m, k=kind: (
                f"{m.group(1)}"
                f'<span class="prob-bar-wrap {k}">'
                f'<span class="prob-bar-fill" style="width:{min(100, int(m.group(2)))}%"></span>'
                f"</span>"
                f'<strong class="prob-pct {k}">{min(100, int(m.group(2)))}%</strong>'
            ),
            html,
        )
    return Markup(html)


# XML 1.0이 허용하지 않는 제어문자 (탭·LF·CR 제외)
_XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _xml_safe(text: object) -> str:
    """[SECURE] XML 비허용 제어문자 제거 - feed.xml/sitemap.xml 파싱 실패(가용성) 방지 (Category 1).

    스키마(mbrief.schemas)에서 이미 제거하지만, 빌더는 XML 직렬화의 마지막 관문이므로
    레코드가 어떤 경로로 만들어졌든 깨진 피드가 배포되지 않도록 한 번 더 방어한다.
    """
    return _XML_ILLEGAL_RE.sub("", str(text or ""))


def _plain_summary(record: BriefingRecord, limit: int = 280) -> str:
    """피드 description용 평문 요약 (마크다운 기호 제거)."""
    source = ""
    if record.structured is not None:
        source = record.structured.overnight or ""
    if not source:
        source = record.contentMarkdown or ""
    text = re.sub(r"[#*`>_\[\]]", "", source)
    text = _xml_safe(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


# ===== 개인정보 · 비공개 필드 제거 =====


def _strip_portfolio_fallback(text: str) -> str:
    """`mbrief.llm.prompts`를 못 불러왔을 때만 쓰는 폴백 구현."""
    out: list[str] = []
    for line in text.splitlines():
        if _PORTFOLIO_MARKER_RE.search(line) and not _DASHBOARD_TITLE_RE.search(line):
            break
        out.append(line)
    stripped = "\n".join(out).rstrip()
    if not stripped:
        return ""
    return stripped + ("\n" if text.endswith("\n") else "")


def strip_portfolio_markdown(text: str) -> str:
    """마크다운 본문에서 비공개 포트폴리오 파트(PART 7 구계약 / PART 8 v2.1) 이후를 잘라낸다.

    [SECURE] 표식 정의는 `mbrief.llm.prompts.strip_portfolio_section()`이 단일 정의처다.
    프롬프트가 파트 번호·다국어 제목을 소유하므로 제거 기준도 같은 곳에서 와야 한다
    (기준 이원화 시 느슨한 쪽이 유출 경로가 된다). import 실패 시에만 동등 범위의
    로컬 폴백을 쓴다 - 비공개 데이터의 공개 산출물 유출 방지
    (Category 2: Improper Authorization).

    포트폴리오는 계약상 마지막 파트이므로 표식이 처음 등장한 줄부터 EOF까지 통째로 제거한다.
    """
    if not text:
        return ""
    helper = getattr(_prompts, "strip_portfolio_section", None) if _prompts else None
    if callable(helper):
        try:
            return str(helper(text))
        except Exception:
            # [SECURE] 상류 헬퍼가 실패해도 원문을 그대로 흘리지 않고 폴백으로 반드시 제거한다
            # (예외를 삼키지 않고 보수적 경로로 강등 - Category 4)
            return _strip_portfolio_fallback(text)
    return _strip_portfolio_fallback(text)


#: 하위 호환 별칭 (기존 호출부·테스트가 쓰는 이름).
strip_part7_markdown = strip_portfolio_markdown


def public_record(record: BriefingRecord, *, publish_portfolio_note: bool) -> BriefingRecord:
    """공개 산출물용 깊은 복사본. 입력 레코드는 절대 변형하지 않는다."""
    # model_copy(deep=True)는 중첩 모델까지 새 객체를 만든다 → 원본 공유 참조 없음
    clone = record.model_copy(deep=True)
    if publish_portfolio_note:
        return clone
    if clone.structured is not None and clone.structured.portfolioNote:
        clone.structured.portfolioNote = None
    if clone.contentMarkdown:
        clone.contentMarkdown = strip_portfolio_markdown(clone.contentMarkdown)
    return clone


# ===== 파일 IO =====


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # [SECURE] 컨텍스트 매니저로 핸들 해제 보장 - Improper Resource Release 방지 (Category 5)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _write_json(path: Path, payload: Any, *, indent: int | None = None) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=indent, default=str))


def _valuation_rows(text: str) -> list:
    """누적 밸류에이션 기록 원문을 JSON 배열로 정규화한다.

    상류(paths.VALUATION_FILE)의 파일 형식이 아직 고정되지 않았으므로 흔한 세 형태를 모두 받는다:
    JSON 배열 / 날짜를 키로 갖는 JSON 객체 / 한 줄에 한 레코드(JSONL).
    """
    stripped = text.strip()
    if not stripped:
        return []
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        payload = None
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("records", "entries", "rows", "history", "days"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return nested
        if payload and all(isinstance(v, dict) for v in payload.values()):
            # {"2026-09-08": {...}} 형태 → 날짜 필드를 살린 배열로 편다
            return [{"date": k, **v} for k, v in sorted(payload.items())]
        return [payload]
    if payload is not None:
        return []  # 스칼라 JSON은 기록이 아니다

    # JSONL 폴백 — 깨진 줄은 버리고 읽을 수 있는 레코드만 살린다
    rows: list = []
    bad = 0
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            bad += 1
    if bad:
        # [SECURE] 파싱 실패를 조용히 삼키지 않고 서버 로그에만 남긴다 (Category 4)
        logger.warning("[Builder] 밸류에이션 기록 %d줄을 해석하지 못해 제외했습니다", bad)
    return rows


def _publish_valuation_history(out_dir: Path) -> bool:
    """누적 밸류에이션 기록을 site/data/sector_valuation.json 으로 공개한다.

    이 기록은 로컬 data/ 에만 쌓이므로 PC가 죽으면 백분위 산출 근거가 통째로 사라진다.
    정적 산출물에 배열로 함께 올려 두면 배포본에서 복구할 수 있다.
    원본이 없으면(=상류가 아직 기록을 시작하지 않았으면) 조용히 건너뛴다.
    """
    source = getattr(_paths, "VALUATION_FILE", None)
    if not source:
        return False
    path = Path(source)
    if not path.is_file():
        return False
    try:
        size = path.stat().st_size
        if size > VALUATION_HISTORY_MAX_BYTES:
            # [SECURE] 크기 상한 - 비대한 기록이 정적 배포본을 망가뜨리지 않게 한다 (가용성)
            logger.warning(
                "[Builder] 밸류에이션 기록이 상한(%d바이트)을 넘어 게시하지 않습니다: %d바이트",
                VALUATION_HISTORY_MAX_BYTES, size,
            )
            return False
        # [SECURE] 컨텍스트 매니저로 핸들 해제 보장 (Category 5)
        with path.open("r", encoding="utf-8") as fh:
            rows = _valuation_rows(fh.read())
    except OSError as exc:
        # [SECURE] 예외를 삼키지 않고 서버 로그로만 남긴다 - 사이트 빌드는 계속 (Category 4)
        logger.warning("[Builder] 밸류에이션 기록을 읽지 못했습니다: %s", exc)
        return False
    if not rows:
        return False
    _write_json(out_dir / "data" / "sector_valuation.json", rows, indent=1)
    return True


def _assert_safe_out_dir(out_dir: Path) -> Path:
    """산출물 디렉터리 삭제 전 안전성 검증.

    허용: 저장소 루트 바로 아래 `site/`, 또는 OS 임시 디렉터리 하위(테스트/프리뷰).
    """
    resolved = out_dir.resolve()
    root = ROOT.resolve()
    tmp = Path(tempfile.gettempdir()).resolve()

    if len(resolved.parts) <= 2:  # 드라이브 루트/최상위는 무조건 거부
        raise ValueError(f"거부: 최상위 경로에는 빌드할 수 없습니다 ({resolved})")
    if resolved == root:
        raise ValueError("거부: 저장소 루트를 산출물 디렉터리로 쓸 수 없습니다")

    allowed = (resolved.parent == root and resolved.name == "site") or resolved.is_relative_to(tmp)
    if not allowed:
        # [SECURE] 화이트리스트 밖 경로 삭제 거부 - 실수/경로조작에 의한 데이터 파괴 방지 (Category 1)
        raise ValueError(
            f"거부: 산출물 디렉터리는 '{root / 'site'}' 또는 임시 디렉터리 하위여야 합니다 ({resolved})"
        )
    return resolved


def _clean_out_dir(out_dir: Path) -> Path:
    resolved = _assert_safe_out_dir(out_dir)
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _compute_build_id(templates_dir: Path, web_dir: Path, latest_record_id: str = "") -> str:
    """빌드 ID = 템플릿·정적 자산 내용 + 최신 레코드 id의 해시.

    벽시계 시각을 섞지 않는다. 시각을 섞으면 내용이 그대로여도 매 빌드마다 값이 바뀌어
    서비스워커 캐시와 `?v=` 쿼리가 무의미하게 전부 무효화된다. 새 브리핑이 나오면
    latest_record_id가 바뀌므로 데이터 캐시는 여전히 갱신된다.
    """
    digest = hashlib.sha256()
    sources: list[Path] = []
    for base, patterns in ((templates_dir, ("*.html", "*.xml")), (web_dir, ("*.css", "*.js"))):
        if base.exists():
            for pattern in patterns:
                sources.extend(sorted(base.rglob(pattern)))
    for path in sources:
        try:
            digest.update(path.read_bytes())
        except OSError:
            # [SECURE] 읽기 실패를 조용히 무시하지 않고 경로를 해시에 남긴다 (Category 4)
            digest.update(f"<unreadable:{path.name}>".encode("utf-8"))
    digest.update(str(latest_record_id or "").encode("utf-8"))
    return digest.hexdigest()[:10]


def _copy_web_assets(web_dir: Path, out_dir: Path, build_id: str, site_url: str) -> None:
    if not web_dir.exists():
        raise FileNotFoundError(f"web/ 디렉터리를 찾을 수 없습니다: {web_dir}")
    shutil.copytree(web_dir, out_dir, dirs_exist_ok=True)

    for name in ("sw.js", "robots.txt", "llms.txt", "manifest.json"):
        target = out_dir / name
        if not target.exists():
            continue
        text = target.read_text(encoding="utf-8")
        if "__SITE_URL__" in text and not site_url:
            # site_url이 없으면 절대 URL이 들어간 줄 자체를 없앤다 (깨진 링크 방지)
            text = "\n".join(line for line in text.splitlines() if "__SITE_URL__" not in line) + "\n"
        text = text.replace("__SITE_URL__", site_url.rstrip("/")).replace("__BUILD_ID__", build_id)
        _write_text(target, text)


# ===== 메인 빌드 =====


def _jinja_env(templates_dir: Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        # [SECURE] autoescape 활성화 - 템플릿 출력 XSS 방지 (Category 1)
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["md"] = lambda v: Markup(render_markdown_safe(v))
    env.filters["mdi"] = lambda v: Markup(inline_markdown_safe(v))
    env.filters["price"] = _fmt_price
    env.filters["signed"] = _fmt_signed
    env.filters["pct"] = _fmt_pct
    return env


def _index_entry(record: BriefingRecord) -> IndexEntry:
    spx = None
    fg_score = None
    if record.market is not None:
        quote = record.market.quotes.get("S&P 500")
        if quote is not None:
            spx = quote.changePercent
        if record.market.fearGreed is not None:
            fg_score = record.market.fearGreed.score
    return IndexEntry(
        id=record.id,
        date=record.date,
        lang=record.lang,
        headline=record.headline or (record.structured.headline if record.structured else ""),
        generatedAt=record.generatedAt,
        model=record.model,
        provider=record.provider,
        direction=record.structured.direction if record.structured else None,
        fearGreedScore=fg_score,
        spxChangePercent=spx,
    )


def _page_path(record: BriefingRecord) -> str:
    return f"archive/{record.id}.html"


def _lang_links(
    record: BriefingRecord, records: Iterable[BriefingRecord], base: str, index_id: str | None
) -> list[dict]:
    """같은 날짜의 다른 언어 레코드로 가는 링크 (존재하는 언어만).

    href는 현재 페이지 기준 상대 경로, canonical_path는 사이트 루트 기준 경로다.
    hreflang처럼 절대 URL이 필요한 곳은 반드시 canonical_path를 쓴다 — 템플릿에서
    상대 경로를 문자열 치환으로 다듬으면 `../archive/x.html` → `.archive/x.html`처럼 깨진다.
    """
    same_day = [r for r in records if r.date == record.date]
    seen: dict[str, BriefingRecord] = {}
    for r in same_day:
        seen.setdefault(r.lang, r)
    links = []
    for lang in sorted(seen, key=lambda code: (code != record.lang, code)):
        target = seen[lang]
        is_index = target.id == index_id
        canonical_path = "" if is_index else _page_path(target)
        href = f"{base}index.html" if is_index else f"{base}{canonical_path}"
        links.append(
            {
                "lang": lang,
                "label": {"ko": "한글", "en": "ENG", "zh": "中文", "ja": "日本語"}.get(lang, lang.upper()),
                "href": href,
                "canonical_path": canonical_path,
                "active": lang == record.lang,
                "id": target.id,
            }
        )
    return links


def build(
    records: list[BriefingRecord],
    market: MarketBundle | None,
    scorecard: Scorecard | None,
    charts: dict[str, list[dict]],
    *,
    out_dir: Path = SITE_DIR,
    settings=default_settings,
) -> BuildResult:
    """레코드/시세/차트를 받아 정적 사이트를 생성한다."""
    out_dir = _clean_out_dir(Path(out_dir))
    site_url = (getattr(settings, "site_url", "") or "").rstrip("/")

    env = _jinja_env(TEMPLATES_DIR)
    now = _now()
    build_time_iso = now.isoformat(timespec="seconds")

    # --- 비공개 필드 제거 (깊은 복사본에만 적용) ---
    publish_note = bool(getattr(settings, "publish_portfolio_note", False))
    safe_records = [
        public_record(r, publish_portfolio_note=publish_note)
        for r in records
        if RECORD_ID_RE.match(r.id or "")
    ]
    # generatedAt은 오프셋이 섞일 수 있어 문자열 비교로는 순서가 뒤집힌다. 반드시 파싱 후 비교한다.
    safe_records.sort(key=lambda r: (r.date, parse_iso(r.generatedAt)), reverse=True)

    primary_lang = getattr(settings, "primary_lang", i18n.DEFAULT_LANG)
    latest = next((r for r in safe_records if r.lang == primary_lang), None) or (
        safe_records[0] if safe_records else None
    )
    index_id = latest.id if latest is not None else None

    # build_id는 자산 내용 + 최신 레코드 id의 해시다. 자산을 복사하기 전에 확정해야
    # sw.js / manifest.json의 __BUILD_ID__ 치환이 실제 값과 일치한다.
    build_id = _compute_build_id(TEMPLATES_DIR, WEB_DIR, index_id or "")
    _copy_web_assets(WEB_DIR, out_dir, build_id, site_url)

    site_ctx = {
        "site_name": getattr(settings, "site_name", "증시흐름"),
        "site_title": getattr(settings, "site_title", ""),
        "site_description": getattr(settings, "site_description", ""),
        "site_url": site_url,
        "github_repo_url": _safe_url(getattr(settings, "github_repo_url", "")),
        "build_id": build_id,
        "build_time_iso": build_time_iso,
    }

    pages = 0

    def render_briefing(record: BriefingRecord, *, base: str, is_index: bool) -> str:
        labels = i18n.get_labels(record.lang)
        record_market = record.market or market
        ordered = [r for r in safe_records if r.lang == record.lang]
        pos = next((i for i, r in enumerate(ordered) if r.id == record.id), None)
        newer = ordered[pos - 1] if pos is not None and pos > 0 else None
        older = ordered[pos + 1] if pos is not None and pos + 1 < len(ordered) else None
        krw_quote = record_market.quotes.get("USD/KRW") if record_market else None

        ctx = {
            **site_ctx,
            "base": base,
            "page": "index" if is_index else "archive",
            "lang": record.lang,
            "html_lang": i18n.html_lang(record.lang),
            "og_locale": i18n.og_locale(record.lang),
            "L": labels,
            "record": record,
            "s": record.structured,
            "headline": record.headline or (record.structured.headline if record.structured else ""),
            "date_text": i18n.format_date(record.date, record.lang),
            "generated_text": i18n.format_datetime(record.generatedAt, record.lang),
            "build_time_text": i18n.format_datetime(build_time_iso, record.lang),
            "provider_label": PROVIDER_LABELS.get(record.provider, record.provider),
            "gen_seconds": f"{record.generationTimeMs / 1000:.1f}s" if record.generationTimeMs else "",
            # v2.1 신규 파트. 구버전 레코드에는 필드가 없으므로 None / 빈 리스트가 정상값이다.
            "recap": _recap_view(record.structured, labels),
            "actors": _actors_view(record.structured, labels),
            "direction": _direction_view(
                record.structured.direction if record.structured else None, labels
            ),
            "cards": _market_cards(record_market, charts),
            "sectors": _sector_chips(record_market),
            # v2.2 신규 필드. 기능 도입 이전 레코드에는 없으므로 빈 리스트가 정상값이며
            # 템플릿은 두 경우 모두 섹션 마크업 자체를 만들지 않는다.
            "sector_heat": _sector_heat_view(record_market, labels),
            "sector_valuation": _sector_valuation_view(record_market, labels),
            "fg": _fear_greed_view(record_market, labels, record.lang),
            "calendar": _calendar_view(record_market, labels, record.lang, record.date),
            "market_updated": (
                i18n.format_datetime(record_market.updatedAt, record.lang) if record_market else ""
            ),
            # 차트 JSON은 레코드 스냅샷이 아니라 '마지막 refresh/tick' 결과다.
            # 장중 tick이 돌면 카드(브리핑이 인용한 시점)와 차트(최신)의 기준 시각이 갈라지므로
            # 각각 자기 데이터의 시각을 따로 표기한다.
            "charts_updated": (
                i18n.format_datetime(market.updatedAt, record.lang) if market else ""
            ),
            "news": _news_view(record, labels),
            "scorecard": _scorecard_view(scorecard, labels, record.lang),
            "body_html": (
                Markup("") if record.structured is not None
                else _fallback_body_html(record.contentMarkdown)
            ),
            "lang_links": _lang_links(record, safe_records, base, index_id),
            "prev_record": older,
            "next_record": newer,
            "prev_href": f"{base}{_page_path(older)}" if older else "",
            "next_href": (
                f"{base}index.html" if newer is not None and newer.id == index_id
                else (f"{base}{_page_path(newer)}" if newer else "")
            ),
            "canonical_path": "" if is_index else _page_path(record),
            "krw_rate": krw_quote.price if krw_quote else None,
            "summary": _plain_summary(record),
        }
        return env.get_template("briefing.html").render(**ctx)

    # --- index.html ---
    if latest is not None:
        _write_text(out_dir / "index.html", render_briefing(latest, base="./", is_index=True))
    else:
        labels = i18n.get_labels(primary_lang)
        _write_text(
            out_dir / "index.html",
            env.get_template("empty.html").render(
                **site_ctx,
                base="./",
                page="index",
                lang=primary_lang,
                html_lang=i18n.html_lang(primary_lang),
                og_locale=i18n.og_locale(primary_lang),
                L=labels,
                build_time_text=i18n.format_datetime(build_time_iso, primary_lang),
                lang_links=[],
                canonical_path="",
            ),
        )
    pages += 1

    # --- archive/<id>.html ---
    for record in safe_records:
        _write_text(
            out_dir / "archive" / f"{record.id}.html",
            render_briefing(record, base="../", is_index=False),
        )
        pages += 1

    # --- archive/index.html ---
    archive_lang = latest.lang if latest is not None else primary_lang
    labels = i18n.get_labels(archive_lang)
    months: list[dict] = []
    for record in safe_records:
        ym = (record.date or "")[:7]
        if not months or months[-1]["key"] != ym:
            months.append({"key": ym, "label": i18n.format_month(ym, archive_lang), "rows": []})
        entry = _index_entry(record)
        months[-1]["rows"].append(
            {
                "record": record,
                "href": f"./{record.id}.html" if record.id != index_id else "../index.html",
                "date_text": i18n.format_short_date(record.date, archive_lang),
                "direction": _direction_view(entry.direction, labels),
                "fg": None if entry.fearGreedScore is None else round(entry.fearGreedScore),
                "spx_text": _fmt_pct(entry.spxChangePercent),
                "spx_dir": (
                    "" if entry.spxChangePercent is None
                    else ("up" if entry.spxChangePercent >= 0 else "down")
                ),
            }
        )
    _write_text(
        out_dir / "archive" / "index.html",
        env.get_template("archive.html").render(
            **site_ctx,
            base="../",
            page="archive",
            lang=archive_lang,
            html_lang=i18n.html_lang(archive_lang),
            og_locale=i18n.og_locale(archive_lang),
            L=labels,
            months=months,
            total=len(safe_records),
            # 표시 언어를 맨 앞에 두고 나머지는 알파벳 순
            languages=sorted(
                {r.lang for r in safe_records} or {primary_lang},
                key=lambda code: (code != archive_lang, code),
            ),
            build_time_text=i18n.format_datetime(build_time_iso, archive_lang),
            canonical_path="archive/index.html",
            lang_links=[],
        ),
    )
    pages += 1

    # --- data/*.json ---
    data_dir = out_dir / "data"
    index_model = SiteIndex(
        generatedAt=build_time_iso,
        languages=sorted({r.lang for r in safe_records}) or [primary_lang],
        entries=[_index_entry(r) for r in safe_records],
    )
    _write_json(data_dir / "index.json", index_model.model_dump(mode="json"), indent=1)
    if latest is not None:
        _write_json(data_dir / "latest.json", latest.model_dump(mode="json"))
    for record in safe_records:
        _write_json(data_dir / "briefings" / f"{record.id}.json", record.model_dump(mode="json"))
    if market is not None:
        _write_json(data_dir / "market.json", market.model_dump(mode="json"), indent=1)
    if scorecard is not None:
        _write_json(data_dir / "scorecard.json", scorecard.model_dump(mode="json"), indent=1)
    # 누적 밸류에이션 기록(로컬 전용)을 배포본에도 실어 오프머신 복구 경로를 만든다. 없으면 건너뛴다.
    _publish_valuation_history(out_dir)
    for key, candles in (charts or {}).items():
        # [SECURE] 차트 키 화이트리스트 검증 - Path Traversal 방지 (Category 1)
        if not CHART_KEY_RE.match(str(key)):
            continue
        _write_json(data_dir / "charts" / f"{key}.json", candles)

    # --- feed.xml / sitemap.xml / .nojekyll ---
    feed_items = []
    for record in safe_records[:20]:
        path = _page_path(record)
        feed_items.append(
            {
                # [SECURE] 제목도 XML 직렬화 직전 제어문자를 제거 - 피드 파싱 실패 방지 (Category 1)
                "title": _xml_safe(
                    record.headline
                    or (record.structured.headline if record.structured else record.date)
                ),
                "link": f"{site_url}/{path}" if site_url else path,
                "guid": f"{site_url}/{path}" if site_url else record.id,
                "pub_date": _rfc822(record.generatedAt, now),
                "description": _plain_summary(record),
                "lang": record.lang,
            }
        )
    _write_text(
        out_dir / "feed.xml",
        env.get_template("feed.xml").render(
            **site_ctx,
            items=feed_items,
            build_rfc822=_rfc822(build_time_iso, now),
            self_link=f"{site_url}/feed.xml" if site_url else "feed.xml",
            home_link=f"{site_url}/" if site_url else "index.html",
            lang=primary_lang,
        ),
    )

    if site_url:
        urls = [{"loc": f"{site_url}/", "lastmod": build_time_iso[:10], "priority": "1.0"}]
        urls.append(
            {"loc": f"{site_url}/archive/", "lastmod": build_time_iso[:10], "priority": "0.7"}
        )
        urls += [
            {"loc": f"{site_url}/{_page_path(r)}", "lastmod": (r.date or "")[:10], "priority": "0.6"}
            for r in safe_records
        ]
        _write_text(
            out_dir / "sitemap.xml", env.get_template("sitemap.xml").render(urls=urls)
        )

    # GitHub Pages가 Jekyll 처리를 건너뛰게 한다 (밑줄로 시작하는 자산 보호)
    _write_text(out_dir / ".nojekyll", "")

    return BuildResult(out_dir=out_dir, pages=pages, records=len(safe_records), build_id=build_id)


def build_from_data(out_dir: Path = SITE_DIR) -> BuildResult:
    """`mbrief.store`에서 실데이터를 읽어 빌드한다."""
    # 지연 import: 이 모듈은 store/pipeline에 의존하지 않아야 단위 테스트가 가볍다.
    from mbrief import store  # noqa: PLC0415

    limit = int(getattr(default_settings, "max_briefings_on_site", 60) or 60)
    records = list(store.list_records(limit=limit))
    market = store.load_market()
    charts = store.load_charts() or {}
    scorecard = store.load_scorecard()
    return build(records, market, scorecard, charts, out_dir=out_dir, settings=default_settings)


# --------------------------------------------------
# Security Checklist
# Applied:
#   - XSS: Jinja2 autoescape(html/xml) 고정. |safe 대상은 이 모듈이 생성한 SVG/마크다운 렌더 결과뿐
#   - Path Traversal: 레코드 id·차트 키를 store와 **동일한** 정규식 화이트리스트로 검증한 뒤에만 파일명으로 사용
#     (builder가 별도 정의하던 느슨한 RECORD_ID_RE 제거 — 검증 기준 이원화 차단)
#   - Open Redirect / 위험 스킴: 뉴스 URL·GitHub URL은 http(s) 정규식 통과분만 링크로 렌더.
#     hreflang 절대 URL은 문자열 치환이 아니라 빌더가 계산한 canonical_path로 조립
#   - Improper Authorization(비공개 데이터 노출): publish_portfolio_note=False면 포트폴리오 파트를
#     깊은 복사본에서 제거한 뒤 직렬화. 제거 기준은 mbrief.llm.prompts.strip_portfolio_section()
#     단일 정의처를 사용하고(import 실패 시에만 동등 범위 폴백), 표기 변형(굵게/평문/다국어)과
#     구계약 PART 7 · v2.1 PART 8 번호를 모두 잡아 표식 이후를 EOF까지 잘라낸다.
#     HTML·records JSON·feed 어디에도 남지 않으며 입력 레코드는 변형하지 않는다
#   - Input Validation: 인물 카드의 이름·직함·티커는 공백 단일화 + 길이 상한을 적용한다
#     (LLM이 만든 초장문 필드가 레이아웃을 무너뜨리는 가용성 문제 방지). 티커는 영숫자만 남긴
#     정규화 키로만 시장 심볼 집합과 비교하며, 매칭돼도 링크는 만들지 않는다(외부 이동 경로 미증가)
#   - Incorrect Permission on Critical Resource: 산출물 디렉터리 삭제는 ROOT/site 또는 임시 디렉터리로 제한
#   - Improper Resource Release: 모든 파일 IO는 컨텍스트 매니저 사용
#   - Improper Input for Security Decision: 색상값은 hex 정규식 검증 후에만 style 속성에 삽입.
#     섹터 과열도/밸류에이션 바의 인라인 width는 0~100 정수로 클램프한 뒤에만 삽입하고,
#     과열도 등급 문자열은 화이트리스트(HEAT_LABEL_KEYS)를 통과한 값만 클래스명·라벨 조회에 쓴다
#   - Missing Error Handling: 안전하지 않은 out_dir·web/ 부재는 예외로 즉시 중단.
#     밸류에이션 기록 게시는 파일 부재·크기 초과·파싱 실패를 각각 구분해 처리하고
#     빈 except 없이 서버 로그에만 남긴 뒤 빌드를 계속한다
#   - Unsafe Deserialization: 밸류에이션 기록은 로컬 data/ 파일이지만 크기 상한을 두고
#     json.loads로만 해석한다 (pickle/eval 경로 없음). 내용은 사이트에 그대로 실린다
# Not Applied:
#   - [WARN] 차트 JSON 내용(캔들 값)은 스키마 검증 없이 그대로 복사한다. store가 신뢰 경계이며,
#            브라우저에서는 숫자로만 사용되지만 상류에서 Candle 모델 검증을 유지해야 한다.
#   - [WARN] 산출물에는 무결성 해시(SRI)를 넣지 않는다. 외부 CDN(lightweight-charts) 스크립트에
#            SRI를 적용하려면 버전 고정 해시를 템플릿에 함께 관리해야 한다.
#   - [WARN] 포트폴리오 제거는 표식 문자열 매칭이다. 앞 파트가 "Portfolio Advice"나 "PART 7"
#            (v2.1에서는 공개 파트인 대시보드) 같은 표현을 포함하면 그 지점부터 잘릴 수 있다.
#            오탐(공개 파트 일부 손실)보다 미탐(비공개 유출)이 위험하므로 의도한 선택이다.
#   - [WARN] keyActors의 인물명·발언은 LLM 출력이며 사실 검증을 하지 않는다. 실명이 잘못된
#            행위와 결합될 수 있으므로 면책 문구(disclaimer)가 같은 블록 안에 유지돼야 한다.
#   - [WARN] site/data/sector_valuation.json 은 스키마 검증 없이 원본 배열을 그대로 옮긴다.
#            상류(data/valuation)가 신뢰 경계이며, 이 파일은 백업·복구 목적의 원본 사본이다.
#            브라우저에서 이 파일을 읽어 화면에 그리게 되면 그때는 필드 검증이 필요하다.
# --------------------------------------------------
