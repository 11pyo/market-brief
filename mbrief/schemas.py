"""공유 데이터 모델 (Pydantic v2).

파일 형식 = 사이트 데이터 계약. 프론트엔드(web/js/app.js)와 빌더(mbrief/site)가 이 구조를 그대로 소비한다.
필드명은 JSON 친화적으로 camelCase를 유지한다.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# XML 1.0이 허용하지 않는 제어문자 (탭·LF·CR 제외). RSS/sitemap 직렬화 시 파서가 문서를 거부한다.
_XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_line(value: object, limit: int) -> str:
    """[SECURE] XML 비허용 제어문자 제거 + 공백 단일화 + 길이 상한 (Category 1).

    헤드라인·출처명은 전량 외부(뉴스 피드·LLM) 문자열이다. 제어문자가 그대로 남으면
    feed.xml/sitemap.xml이 XML 파서에서 깨지고(가용성), 한 줄 계약도 무너진다.
    """
    return " ".join(_XML_ILLEGAL_RE.sub("", str(value or "")).split())[:limit]


# ===== 시세 =====
class Quote(BaseModel):
    price: Optional[float] = None
    change: Optional[float] = None
    changePercent: Optional[float] = None


class SectorQuote(BaseModel):
    symbol: str
    price: Optional[float] = None
    changePercent: Optional[float] = None


class FearGreed(BaseModel):
    score: float
    rating: str
    rating_ko: str = ""
    color: str = "#eab308"
    previous_close: Optional[float] = None
    previous_1week: Optional[float] = None
    previous_1month: Optional[float] = None
    timestamp: str = ""


class Candle(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float


class MarketBundle(BaseModel):
    """data/cache/market.json 및 브리핑 레코드의 market 필드. 생성 시점 스냅샷."""
    updatedAt: str                                  # ISO 8601 (KST)
    quotes: dict[str, Quote] = Field(default_factory=dict)        # 표시명 → 시세 (예: "S&P 500")
    sectors: dict[str, SectorQuote] = Field(default_factory=dict) # 섹터명 → ETF 시세
    fearGreed: Optional[FearGreed] = None
    calendar: dict = Field(default_factory=dict)    # {"fomc": [...], "indicators": [...], "earnings": [...], "economic": [...]}
    chartKeys: list[str] = Field(default_factory=list)  # data/charts/<key>.json 목록 (예: "sp500_1d")


# ===== 구조화 브리핑 (LLM 출력) =====
Severity = Literal["high", "medium", "low"]
Side = Literal["long", "short"]
Call = Literal["up", "flat", "down"]


class Direction(BaseModel):
    up: int = Field(ge=0, le=100)
    flat: int = Field(ge=0, le=100)
    down: int = Field(ge=0, le=100)
    rationaleUp: str = ""
    rationaleFlat: str = ""
    rationaleDown: str = ""

    @model_validator(mode="after")
    def _normalize_total(self) -> "Direction":
        total = self.up + self.flat + self.down
        if total <= 0:
            self.up, self.flat, self.down = 34, 33, 33
        elif total != 100:
            # LLM 반올림 오차 보정: 가장 큰 값에 잔차를 흡수시켜 합계 100 유지
            diff = 100 - total
            key = max(("up", "flat", "down"), key=lambda k: getattr(self, k))
            setattr(self, key, getattr(self, key) + diff)
        return self

    @property
    def call(self) -> Call:
        return max(("up", "flat", "down"), key=lambda k: getattr(self, k))  # type: ignore[return-value]


class SectorCall(BaseModel):
    name: str
    etf: str = ""
    reason: str = ""


class Risk(BaseModel):
    title: str
    detail: str = ""
    severity: Severity = "medium"


class TradeIdea(BaseModel):
    asset: str
    side: Side = "long"
    entry: str = ""
    target: str = ""
    stop: str = ""
    rr: str = ""
    rationale: str = ""


class DashboardItem(BaseModel):
    label: str
    value: str
    comment: str = ""


class StructuredBriefing(BaseModel):
    """`claude -p --json-schema`로 받는 본문. 각 문자열 필드는 마크다운 허용(굵게·목록 정도)."""
    headline: str                                   # 한 줄 헤드라인 (구체적 사건·수치, <= 90자)
    overnight: str                                  # PART 1: 어젯밤 핵심 이벤트 & 시장 충격 분석 (마크다운)
    direction: Direction                            # PART 2: 방향성 확률 + 근거
    sectorLeaders: list[SectorCall] = Field(default_factory=list)   # PART 3: 주도 섹터 (3)
    sectorLaggards: list[SectorCall] = Field(default_factory=list)  # PART 3: 약세 섹터 (2)
    risks: list[Risk] = Field(default_factory=list)                 # PART 4: 핵심 리스크 (3~5)
    trades: list[TradeIdea] = Field(default_factory=list)           # PART 5: 트레이드 아이디어 (2~4)
    dashboard: list[DashboardItem] = Field(default_factory=list)    # PART 6: 10초 마켓 대시보드 (6~10)
    portfolioNote: Optional[str] = None             # PART 7: 소유자 포트폴리오 맞춤 조언 (선택, 기본 비공개)

    @field_validator("headline")
    @classmethod
    def _trim_headline(cls, v: str) -> str:
        return _clean_line(v, 160)


class NewsRef(BaseModel):
    headline: str
    source: str = ""
    tier: int = 3
    url: str = ""
    time: str = ""          # "YYYY-MM-DD HH:MM UTC"
    category: str = ""

    @field_validator("headline")
    @classmethod
    def _clean_headline(cls, v: str) -> str:
        return _clean_line(v, 300)

    @field_validator("source")
    @classmethod
    def _clean_source(cls, v: str) -> str:
        return _clean_line(v, 80)


class BriefingRecord(BaseModel):
    """data/briefings/<id>.json 및 site/data/briefings/<id>.json."""
    id: str                                         # "YYYY-MM-DD-<lang>-<6hex>"
    date: str                                       # "YYYY-MM-DD" (KST)
    lang: str = "ko"
    generatedAt: str                                # ISO 8601 (KST, 오프셋 포함)
    provider: str = "claude_cli"                    # claude_cli | gemini | openai | anthropic | mock
    model: str = ""
    generationTimeMs: int = 0
    newsCount: int = 0
    tierCounts: dict[str, int] = Field(default_factory=dict)       # {"t1": 8, "t2": 29, "t3": 13}
    headline: str = ""                              # structured.headline 복제 (인덱스·피드용)
    structured: Optional[StructuredBriefing] = None # 구조화 출력. 텍스트 폴백 시 None
    contentMarkdown: str = ""                       # 전체 마크다운 (구조화 → 렌더 or 모델 원문). 피드·텔레그램·폴백 렌더용
    market: Optional[MarketBundle] = None           # 생성 시점 스냅샷
    topNews: list[NewsRef] = Field(default_factory=list)          # 근거 뉴스 상위 (<= 12)
    translatedFrom: Optional[str] = None            # 번역 패스로 만든 레코드면 원본 id

    @field_validator("headline")
    @classmethod
    def _clean_headline(cls, v: str) -> str:
        # [SECURE] feed.xml/sitemap.xml의 제목으로 직렬화되는 값 - XML 비허용 제어문자 제거 (Category 1)
        return _clean_line(v, 300)

    @property
    def is_structured(self) -> bool:
        return self.structured is not None


# ===== 사이트 인덱스 =====
class IndexEntry(BaseModel):
    id: str
    date: str
    lang: str
    headline: str
    generatedAt: str
    model: str = ""
    provider: str = ""
    direction: Optional[Direction] = None
    fearGreedScore: Optional[float] = None
    spxChangePercent: Optional[float] = None


class SiteIndex(BaseModel):
    """site/data/index.json — 최신순."""
    generatedAt: str
    languages: list[str] = Field(default_factory=lambda: ["ko"])
    entries: list[IndexEntry] = Field(default_factory=list)


# ===== 방향성 적중 스코어카드 =====
class ScoreEntry(BaseModel):
    date: str                                       # 예측 브리핑 날짜
    briefingId: str
    predicted: Call
    probs: Direction
    actual: Call
    spxChangePercent: float                         # 예측 대상 세션의 S&P 500 등락률
    scoredAt: str
    hit: bool
    brier: float                                    # 0(완벽)~2(최악). 확률 예측 품질


class Scorecard(BaseModel):
    """data/scorecard.json — 이전 브리핑의 방향성 예측을 다음 스냅샷으로 채점한 누적 기록."""
    updatedAt: str
    entries: list[ScoreEntry] = Field(default_factory=list)      # 최신순
    hitRate: Optional[float] = None                 # 최근 sampleWindow 적중률 (0~1)
    avgBrier: Optional[float] = None
    sampleSize: int = 0
    sampleWindow: int = 30


# ===== 소유자 포트폴리오 (선택, 비공개) =====
class PortfolioAllocation(BaseModel):
    name: str
    percentage: int = 0
    details: str = ""


class Portfolio(BaseModel):
    allocations: list[PortfolioAllocation] = Field(default_factory=list)
    investmentStyle: str = "Growth"
    riskTolerance: str = "Medium-High"
    totalAssets: str = ""
    watchlist: list[str] = Field(default_factory=list)


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Unsafe Deserialization: 외부(LLM·파일) JSON은 전부 이 pydantic 모델로 검증한 뒤에만 사용된다
#   - Input Validation: 헤드라인·출처명(StructuredBriefing.headline, NewsRef.headline/source,
#     BriefingRecord.headline)에서 XML 비허용 제어문자를 제거하고 공백 단일화 + 길이 상한을 적용한다.
#     feed.xml/sitemap.xml이 제어문자로 깨지지 않도록 하는 가용성 방어이기도 하다
#   - Integer Overflow/범위: 확률 필드는 ge/le로 0~100 범위를 강제하고 합계 100을 모델 검증에서 보정
# Not Applied:
#   - [WARN] 본문 필드(overnight/contentMarkdown 등)에는 제어문자 필터를 적용하지 않는다.
#     마크다운 개행을 보존해야 하기 때문이며, XML 직렬화 경로(feed description)는
#     mbrief/site/builder.py의 _plain_summary가 별도로 정제한다.
#   - [WARN] 이 모델들은 표현 계약일 뿐 인가(authorization) 판단을 하지 않는다.
#     비공개 필드(portfolioNote) 노출 제어는 builder.public_record 책임이다.
# --------------------------------------------------
