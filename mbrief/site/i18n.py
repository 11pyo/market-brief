"""정적 사이트 UI 라벨 사전 + 로케일 날짜 포맷.

빌드 시점에 모든 문자열이 HTML로 구워지므로 클라이언트 i18n 런타임이 없다.
언어별로 페이지를 따로 렌더링하고, 상단 언어 전환기는 형제 페이지로 이동만 한다.

폴백 규칙: <요청 언어> → en → ko (키 단위로 병합하므로 누락 키가 있어도 빈칸이 되지 않는다).
"""
from __future__ import annotations

from datetime import datetime, date
from functools import lru_cache

__all__ = [
    "LABELS",
    "SUPPORTED_LANGS",
    "DEFAULT_LANG",
    "get_labels",
    "format_date",
    "format_short_date",
    "format_month",
    "format_time",
    "format_datetime",
    "html_lang",
    "og_locale",
]

DEFAULT_LANG = "ko"
SUPPORTED_LANGS = ("ko", "en", "zh", "ja")

# BCP-47 / OG 로케일 매핑
_HTML_LANG = {"ko": "ko", "en": "en", "zh": "zh-Hans", "ja": "ja"}
_OG_LOCALE = {"ko": "ko_KR", "en": "en_US", "zh": "zh_CN", "ja": "ja_JP"}


LABELS: dict[str, dict[str, str]] = {
    # ==========================================================
    "ko": {
        # 브랜드 · 내비게이션
        "brandTitle": "AI 모닝 브리핑",
        "brandSubtitle": "증시흐름",
        "navHome": "홈",
        "navArchive": "아카이브",
        "navRss": "RSS",
        "navGithub": "GitHub",
        "navLangLabel": "언어 선택",
        "navCurrencyLabel": "통화 단위 전환",
        "skipToContent": "본문으로 건너뛰기",
        # 섹션 제목
        "sectionMarket": "📊 시장 스냅샷",
        "sectionSectors": "🏭 섹터 ETF",
        "sectionBriefing": "🧠 AI 모닝 브리핑",
        "sectionCalendar": "🗓️ 경제 캘린더",
        "sectionNews": "📰 근거 뉴스",
        "sectionScorecard": "🎯 방향성 적중률",
        "sectionArchive": "🗂️ 아카이브",
        # PART 제목 (v2.1에서 8파트로 재번호: 리캡 신설 + 핵심 인물이 PART 2에 합류)
        "part1": "PART 1 · 전날 시장 리캡",
        "part2": "PART 2 · 밤사이 이벤트 & 핵심 인물",
        "part3": "PART 3 · 오늘의 방향성 확률",
        "part4": "PART 4 · 섹터 리더십",
        "part5": "PART 5 · 핵심 리스크",
        "part6": "PART 6 · 트레이드 아이디어",
        "part7": "PART 7 · 10초 마켓 대시보드",
        "part8": "PART 8 · 포트폴리오 맞춤 조언",
        # PART 1 전날 리캡
        "partRecap": "전날 시장 리캡",
        "recapDrivers": "시장을 움직인 요인",
        # PART 2 핵심 인물
        "partActors": "시장을 움직인 인물",
        "actorImpact": "영향",
        "actorAssets": "관련 자산",
        # 방향성
        "dirUp": "상승",
        "dirFlat": "횡보",
        "dirDown": "하락",
        "dirCall": "오늘의 콜",
        "dirRationale": "근거",
        # 섹터 리더십
        "leadersTitle": "주도 섹터",
        "laggardsTitle": "약세 섹터",
        # 섹터 과열도 · 밸류에이션 (v2.2)
        # 과열도는 가격만으로 계산하는 '스트레치' 지표다. 가치 판정 어휘(싸다/비싸다)는 쓰지 않는다.
        "sectorHeatTitle": "🌡️ 섹터 과열도",
        "sectorValuationTitle": "🧾 섹터 밸류에이션",
        "themeHeatTitle": "🧩 테마·산업 과열도",
        "themeValuationTitle": "🧩 테마·산업 밸류에이션",
        "themeNote": (
            "대분류를 더 잘게 쪼갠 산업·테마입니다. 테마는 대분류의 부분집합이라 서로 겹칩니다 "
            "(반도체는 Technology 안에 있습니다). 합산하거나 비중으로 읽지 마세요."
        ),
        "heatHot": "과열",
        "heatWarm": "다소 과열",
        "heatNeutral": "중립",
        "heatCool": "다소 침체",
        "heatCold": "침체",
        "heatGap": "200일선 이격",
        "heatPercentile": "분포 백분위",
        "heatRsi": "RSI(14)",
        "heatRel3m": "3개월 상대",
        "heatNote": (
            "가격만으로 계산한 '얼마나 달아올랐나' 지표입니다. 가치 판단이 아닙니다. "
            "등급은 오늘의 200일선 이격이 그 섹터 자체의 과거 이격 분포에서 어디에 있는지"
            "(백분위)만으로 정합니다. RSI와 3개월 상대수익률은 참고 수치입니다."
        ),
        "heatSampleDays": "관측 {n}일",
        "valuationPer": "PER",
        "valuationPbr": "PBR",
        "valuationYield": "배당수익률",
        "valuationAccruing": "기록 중",
        "valuationAccruingDays": "기록 중 ({n}일)",
        "valuationNote": (
            "현재 PER·PBR 수치만 표시합니다. 자체 기록이 충분히 쌓이기 전에는 백분위를 계산하지 않으며, "
            "어떤 가치 판정도 내리지 않습니다."
        ),
        # 리스크
        "riskHigh": "높음",
        "riskMedium": "보통",
        "riskLow": "낮음",
        # 트레이드
        "tradeLong": "롱",
        "tradeShort": "숏",
        "tradeEntry": "진입",
        "tradeTarget": "목표",
        "tradeStop": "손절",
        "tradeRr": "R:R",
        # 시장 스냅샷
        "marketBasis": "기준 시각",
        "marketEmpty": "시세 데이터가 없습니다.",
        "chartHint": "카드를 누르면 차트가 열립니다",
        # 공포·탐욕
        "fgTitle": "공포·탐욕 지수",
        "fgPrevClose": "전일 종가",
        "fgPrev1w": "1주 전",
        "fgPrev1m": "1개월 전",
        "fgSource": "출처: CNN Fear & Greed Index",
        "fgRatingExtremeFear": "극도의 공포",
        "fgRatingFear": "공포",
        "fgRatingNeutral": "중립",
        "fgRatingGreed": "탐욕",
        "fgRatingExtremeGreed": "극도의 탐욕",
        # 캘린더
        "calSource": "출처: Federal Reserve · U.S. BLS · Finnhub",
        "calEmpty": "표시할 일정이 없습니다.",
        "fomcTitle": "FOMC 정례회의",
        "fomcNext": "다음 회의",
        "fomcLast": "직전 회의",
        "fomcLive": "진행 중",
        "fomcDone": "종료",
        "fomcSep": "SEP",
        "fomcSepFull": "경제전망요약(점도표) 동시 발표 — 시장 영향 큼",
        "indicatorsTitle": "최근 발표 지표 (미국)",
        "earningsTitle": "주요 실적 발표",
        "earnBmo": "장전",
        "earnAmc": "장후",
        "earnDmh": "장중",
        "calToday": "오늘",
        "calTomorrow": "내일",
        "calTbd": "미정",
        # 뉴스
        "newsSource": "출처",
        "newsTierLegend": "T1 공식 · T2 검증 · T3 통신",
        "newsTier1": "T1 공식",
        "newsTier2": "T2 검증",
        "newsTier3": "T3 통신",
        "newsEmpty": "인용된 뉴스가 없습니다.",
        # 스코어카드
        "scoreSummary": "최근 {n}회 방향성 적중률 {rate}% · 평균 Brier {brier}",
        "scoreNote": "S&P 500 방향성 예측을 다음 세션 종가와 대조해 채점합니다.",
        "scoreHit": "적중",
        "scoreMiss": "빗나감",
        "scoreTooltip": "{date} · 예측 {call} {pct}% → 실제 {actual}",
        "scoreEmpty": "채점 이력이 아직 없습니다.",
        # 메타
        "metaModel": "생성 모델",
        "metaProvider": "생성 경로",
        "metaNews": "뉴스 {n}건",
        "metaGenTime": "생성 소요시간",
        "metaUpdated": "최종 갱신",
        "metaBuild": "빌드",
        # 아카이브 · 내비
        "archiveTitle": "아카이브",
        "archiveAll": "아카이브 전체 보기",
        "archiveEmpty": "저장된 브리핑이 없습니다.",
        "archiveCount": "총 {n}건",
        "prevBriefing": "이전 브리핑",
        "nextBriefing": "다음 브리핑",
        "filterAll": "전체",
        # 관심종목 렌즈
        "watchTitle": "관심종목 하이라이트",
        "watchToggle": "내 관심종목",
        "watchPlaceholder": "쉼표로 구분. 예: NVDA, XLU, 반도체",
        "watchApply": "적용",
        "watchClear": "지우기",
        "watchHint": "브라우저에만 저장되며 서버로 전송되지 않습니다.",
        "watchMatchTrades": "언급된 트레이드 아이디어",
        "watchMatchSectors": "언급된 섹터",
        "watchNone": "브리핑에서 일치하는 항목을 찾지 못했습니다.",
        "watchCount": "{n}곳 일치",
        # PWA · 공유 · 차트
        "pwaHint": "홈 화면에 추가하면 매일 아침 한 번에 열립니다.",
        "pwaDismiss": "닫기",
        "shareBtn": "공유",
        "shareCopied": "링크가 복사되었습니다.",
        "chartLoading": "차트 로딩 중...",
        "chartNoData": "차트 데이터 없음",
        "chartClose": "닫기",
        "chartTz": "시간 기준",
        "chartError": "차트를 불러오지 못했습니다.",
        "swUpdated": "새 버전이 준비되었습니다. 새로고침하세요.",
        # 면책
        "disclaimerTitle": "투자 유의사항",
        "disclaimerBody": (
            "본 브리핑은 AI가 공개된 뉴스와 시장 데이터를 자동 분석해 생성한 참고 자료이며, 투자자문이나 "
            "투자권유가 아닙니다. 제시된 상승·하락 확률, 목표가, 손절가를 포함한 모든 내용은 정확성이나 수익을 "
            "보장하지 않으며 사실과 다른 정보가 포함될 수 있습니다. 투자 판단과 그 결과에 대한 책임은 전적으로 "
            "이용자 본인에게 있습니다."
        ),
        "disclaimerShort": "AI 생성 참고 자료입니다. 투자자문이 아니며 투자 책임은 이용자 본인에게 있습니다.",
        # 푸터
        "footerSources": "데이터 출처: Yahoo Finance · Finnhub · CNN · Federal Reserve · U.S. BLS",
        "footerGenerated": "생성: 로컬 Claude Code CLI",
        "footerBuild": "사이트 빌드",
        # 빈 상태
        "emptyTitle": "아직 브리핑이 없습니다",
        "emptyDesc": "매일 아침 자동 생성됩니다. 첫 브리핑이 만들어지면 이 자리에 표시됩니다.",
        # 스마트 허브 & 인터랙티브 뷰
        "hubBadge": "✨ AI 모닝 네비게이터",
        "hubQuestion": "어떤 것을 확인하시겠어요?",
        "hubSubtitle": "카드를 선택해 관심 있는 섹션만 집중해서 열어보거나, 모드를 전환해 편리하게 탐색하세요.",
        "hubCardBriefing": "오늘의 핵심 브리핑",
        "hubCardBriefingDesc": "시장 리캡 · 인물 · 주도주 & 리스크",
        "hubCardMarket": "10초 시장 스냅샷",
        "hubCardMarketDesc": "나스닥 · S&P500 · 금리 · 환율 12종",
        "hubCardSectors": "섹터 맵 & 밸류 지표",
        "hubCardSectorsDesc": "주요 섹터 등락률 및 모멘텀 진단",
        "hubCardCalendar": "경제 캘린더 & FOMC",
        "hubCardCalendarDesc": "차기 FOMC D-Day · 물가/고용 · 실적",
        "hubCardNews": "검증된 근거 뉴스",
        "hubCardNewsDesc": "T1 공식 원문 & 주요 외신 50선",
        "hubCardScorecard": "AI 예측 적중률 성적표",
        "hubCardScorecardDesc": "Brier Score 사후 검증 통계",
        "hubModeCard": "🗂️ 팝업 모달 뷰 (Zero-Scroll)",
        "hubModeAll": "📑 리서처 뷰 (전체 펼침 · Ctrl+F 검색)",
        "collapseToggle": "접기/펼치기",
        "collapseFold": "접기",
        "collapseUnfold": "펼치기",
        "newsShowMore": "📰 근거 뉴스 전체 펼치기 (남은 {n}건 더보기)",
        "newsShowLess": "뉴스 접기 (상위 10건만 보기)",
        "quickHub": "✨ 네비게이터",
        "quickTop": "▲ 맨 위로",
        "modalClose": "닫기",
        "cardActionPopup": "팝업으로 열기 ➔",
    },
    # ==========================================================
    "en": {
        "brandTitle": "AI Morning Brief",
        "brandSubtitle": "Market Flow",
        "navHome": "Home",
        "navArchive": "Archive",
        "navRss": "RSS",
        "navGithub": "GitHub",
        "navLangLabel": "Select language",
        "navCurrencyLabel": "Toggle currency",
        "skipToContent": "Skip to content",
        "sectionMarket": "📊 Market Snapshot",
        "sectionSectors": "🏭 Sector ETFs",
        "sectionBriefing": "🧠 AI Morning Briefing",
        "sectionCalendar": "🗓️ Economic Calendar",
        "sectionNews": "📰 Source News",
        "sectionScorecard": "🎯 Directional Hit Rate",
        "sectionArchive": "🗂️ Archive",
        "part1": "PART 1 · Previous Session Recap",
        "part2": "PART 2 · Overnight Events & Key Actors",
        "part3": "PART 3 · Direction Probability",
        "part4": "PART 4 · Sector Leadership",
        "part5": "PART 5 · Key Risks",
        "part6": "PART 6 · Trade Ideas",
        "part7": "PART 7 · 10-Second Dashboard",
        "part8": "PART 8 · Portfolio Advice",
        "partRecap": "Previous session recap",
        "recapDrivers": "What moved the market",
        "partActors": "Who moved the market",
        "actorImpact": "Impact",
        "actorAssets": "Assets in play",
        "dirUp": "Up",
        "dirFlat": "Flat",
        "dirDown": "Down",
        "dirCall": "Today's call",
        "dirRationale": "Rationale",
        "leadersTitle": "Leaders",
        "laggardsTitle": "Laggards",
        "sectorHeatTitle": "🌡️ Sector Heat",
        "sectorValuationTitle": "🧾 Sector Valuation",
        "themeHeatTitle": "🧩 Theme & Industry Heat",
        "themeValuationTitle": "🧩 Theme & Industry Valuation",
        "themeNote": (
            "Finer industry and theme slices. Themes are subsets of the broad sectors and overlap "
            "each other (semiconductors sit inside Technology). Do not sum them or read them as weights."
        ),
        "heatHot": "Hot",
        "heatWarm": "Warm",
        "heatNeutral": "Neutral",
        "heatCool": "Cool",
        "heatCold": "Cold",
        "heatGap": "Gap to 200DMA",
        "heatPercentile": "Percentile",
        "heatRsi": "RSI(14)",
        "heatRel3m": "3M relative",
        "heatNote": (
            "How stretched each sector's price is right now, computed from price alone. "
            "This is not a valuation judgement. The label comes only from where today's gap to the "
            "200-day average sits in that sector's own historical gap distribution (percentile). "
            "RSI and 3-month relative return are supporting context."
        ),
        "heatSampleDays": "{n} observed days",
        "valuationPer": "P/E",
        "valuationPbr": "P/B",
        "valuationYield": "Dividend yield",
        "valuationAccruing": "Accruing",
        "valuationAccruingDays": "Accruing ({n}d)",
        "valuationNote": (
            "Current P/E and P/B only. Until enough of our own history has accumulated, no percentile "
            "is computed and no valuation verdict is given."
        ),
        "riskHigh": "High",
        "riskMedium": "Medium",
        "riskLow": "Low",
        "tradeLong": "LONG",
        "tradeShort": "SHORT",
        "tradeEntry": "Entry",
        "tradeTarget": "Target",
        "tradeStop": "Stop",
        "tradeRr": "R:R",
        "marketBasis": "As of",
        "marketEmpty": "No market data available.",
        "chartHint": "Tap a card to open its chart",
        "fgTitle": "Fear & Greed Index",
        "fgPrevClose": "Prev close",
        "fgPrev1w": "1 week ago",
        "fgPrev1m": "1 month ago",
        "fgSource": "Source: CNN Fear & Greed Index",
        "fgRatingExtremeFear": "Extreme Fear",
        "fgRatingFear": "Fear",
        "fgRatingNeutral": "Neutral",
        "fgRatingGreed": "Greed",
        "fgRatingExtremeGreed": "Extreme Greed",
        "calSource": "Source: Federal Reserve · U.S. BLS · Finnhub",
        "calEmpty": "No events to display.",
        "fomcTitle": "FOMC Meetings",
        "fomcNext": "Next meeting",
        "fomcLast": "Most recent",
        "fomcLive": "In progress",
        "fomcDone": "Concluded",
        "fomcSep": "SEP",
        "fomcSepFull": "Summary of Economic Projections (dot plot) released — higher market impact",
        "indicatorsTitle": "Latest U.S. Indicator Releases",
        "earningsTitle": "Major Earnings",
        "earnBmo": "BMO",
        "earnAmc": "AMC",
        "earnDmh": "Intraday",
        "calToday": "Today",
        "calTomorrow": "Tomorrow",
        "calTbd": "TBD",
        "newsSource": "Source",
        "newsTierLegend": "T1 official · T2 verified · T3 wire",
        "newsTier1": "T1 official",
        "newsTier2": "T2 verified",
        "newsTier3": "T3 wire",
        "newsEmpty": "No source news cited.",
        "scoreSummary": "Last {n} calls · hit rate {rate}% · avg Brier {brier}",
        "scoreNote": "Scores the S&P 500 direction call against the next session's close.",
        "scoreHit": "Hit",
        "scoreMiss": "Miss",
        "scoreTooltip": "{date} · called {call} {pct}% → actual {actual}",
        "scoreEmpty": "No scored calls yet.",
        "metaModel": "Model",
        "metaProvider": "Engine",
        "metaNews": "{n} news",
        "metaGenTime": "Generation time",
        "metaUpdated": "Last updated",
        "metaBuild": "Build",
        "archiveTitle": "Archive",
        "archiveAll": "Browse full archive",
        "archiveEmpty": "No briefings stored yet.",
        "archiveCount": "{n} total",
        "prevBriefing": "Older",
        "nextBriefing": "Newer",
        "filterAll": "All",
        "watchTitle": "Watchlist highlights",
        "watchToggle": "My watchlist",
        "watchPlaceholder": "Comma separated. e.g. NVDA, XLU, semis",
        "watchApply": "Apply",
        "watchClear": "Clear",
        "watchHint": "Stored in your browser only — never sent to a server.",
        "watchMatchTrades": "Trade ideas mentioning these",
        "watchMatchSectors": "Sectors mentioning these",
        "watchNone": "No matches found in this briefing.",
        "watchCount": "{n} matches",
        "pwaHint": "Add to your home screen to open tomorrow's brief in one tap.",
        "pwaDismiss": "Dismiss",
        "shareBtn": "Share",
        "shareCopied": "Link copied to clipboard.",
        "chartLoading": "Loading chart...",
        "chartNoData": "No chart data",
        "chartClose": "Close",
        "chartTz": "Times in",
        "chartError": "Failed to load the chart.",
        "swUpdated": "A new version is ready. Reload to update.",
        "disclaimerTitle": "Investment Disclaimer",
        "disclaimerBody": (
            "This briefing is reference material generated automatically by AI from publicly available news "
            "and market data. It is not investment advice or a solicitation to trade. All content — including "
            "directional probabilities, price targets, and stop levels — carries no guarantee of accuracy or "
            "profit and may contain factual errors. You are solely responsible for your investment decisions "
            "and their outcomes."
        ),
        "disclaimerShort": "AI-generated reference material. Not investment advice — you are responsible for your own decisions.",
        "footerSources": "Data: Yahoo Finance · Finnhub · CNN · Federal Reserve · U.S. BLS",
        "footerGenerated": "Generated by local Claude Code CLI",
        "footerBuild": "Site build",
        "emptyTitle": "No briefing yet",
        "emptyDesc": "A new briefing is generated every morning. The first one will appear right here.",
        # Smart Hub & Interactive View
        "hubBadge": "✨ AI Morning Navigator",
        "hubQuestion": "What would you like to check?",
        "hubSubtitle": "Select a card to focus on what matters most, or toggle view modes for effortless reading.",
        "hubCardBriefing": "Key Morning Briefing",
        "hubCardBriefingDesc": "Market Recap · Key Actors · Leaders & Risks",
        "hubCardMarket": "10s Market Snapshot",
        "hubCardMarketDesc": "Nasdaq · S&P 500 · 10Y Yield · Currencies",
        "hubCardSectors": "Sector Heat & PER",
        "hubCardSectorsDesc": "24 Sectors & Themes Heatmap & Valuations",
        "hubCardCalendar": "Economic Calendar & FOMC",
        "hubCardCalendarDesc": "Next FOMC D-Day · Inflation/Jobs · Earnings",
        "hubCardNews": "Verified Source News",
        "hubCardNewsDesc": "T1 Primary Sources & Wire Coverage (Top 50)",
        "hubCardScorecard": "AI Accuracy Scorecard",
        "hubModeCard": "🗂️ Popup Modal View (Zero Scroll)",
        "hubModeAll": "📑 Researcher View (Expand All · Ctrl+F Search)",
        "collapseToggle": "Fold/Unfold",
        "collapseFold": "Fold",
        "collapseUnfold": "Unfold",
        "newsShowMore": "📰 Show all source news (show {n} more)",
        "newsShowLess": "Fold news (top 10 only)",
        "quickHub": "✨ Navigator",
        "quickTop": "▲ Back to Top",
        "modalClose": "Close",
        "cardActionPopup": "Open in Popup ➔",
    },
    # ==========================================================
    "zh": {
        "brandTitle": "AI 晨间简报",
        "brandSubtitle": "市场动态",
        "navHome": "首页",
        "navArchive": "存档",
        "navRss": "RSS",
        "navGithub": "GitHub",
        "navLangLabel": "选择语言",
        "navCurrencyLabel": "切换货币",
        "skipToContent": "跳到正文",
        "sectionMarket": "📊 市场快照",
        "sectionSectors": "🏭 板块 ETF",
        "sectionBriefing": "🧠 AI 晨间简报",
        "sectionCalendar": "🗓️ 经济日历",
        "sectionNews": "📰 依据新闻",
        "sectionScorecard": "🎯 方向判断命中率",
        "sectionArchive": "🗂️ 存档",
        "part1": "PART 1 · 前一交易日回顾",
        "part2": "PART 2 · 隔夜事件与关键人物",
        "part3": "PART 3 · 今日方向概率",
        "part4": "PART 4 · 板块领导力",
        "part5": "PART 5 · 核心风险",
        "part6": "PART 6 · 交易思路",
        "part7": "PART 7 · 十秒仪表盘",
        "part8": "PART 8 · 投资组合专属建议",
        "partRecap": "前一交易日回顾",
        "recapDrivers": "推动行情的因素",
        "partActors": "推动市场的人物",
        "actorImpact": "影响",
        "actorAssets": "相关资产",
        "dirUp": "上涨",
        "dirFlat": "震荡",
        "dirDown": "下跌",
        "dirCall": "今日判断",
        "dirRationale": "依据",
        "leadersTitle": "领涨板块",
        "laggardsTitle": "落后板块",
        "sectorHeatTitle": "🌡️ 板块过热度",
        "sectorValuationTitle": "🧾 板块估值",
        "themeHeatTitle": "🧩 主题·行业过热度",
        "themeValuationTitle": "🧩 主题·行业估值",
        "themeNote": (
            "更细分的行业与主题。主题是大类板块的子集且相互重叠（半导体属于科技板块）。"
            "请勿相加或当作权重解读。"
        ),
        "heatHot": "过热",
        "heatWarm": "偏热",
        "heatNeutral": "中性",
        "heatCool": "偏冷",
        "heatCold": "低迷",
        "heatGap": "200日均线乖离",
        "heatPercentile": "分布百分位",
        "heatRsi": "RSI(14)",
        "heatRel3m": "3个月相对",
        "heatNote": (
            "仅用价格计算的“热度”指标，衡量板块当前被推得有多远，并非价值判断。"
            "等级只取决于今日相对200日均线的乖离在该板块自身历史乖离分布中的百分位。"
            "RSI 与 3 个月相对收益仅作参考。"
        ),
        "heatSampleDays": "观测 {n} 天",
        "valuationPer": "市盈率",
        "valuationPbr": "市净率",
        "valuationYield": "股息率",
        "valuationAccruing": "记录中",
        "valuationAccruingDays": "记录中（{n}天）",
        "valuationNote": (
            "仅显示当前市盈率与市净率。在自有记录积累足够之前不计算百分位，也不给出任何价值判断。"
        ),
        "riskHigh": "高",
        "riskMedium": "中",
        "riskLow": "低",
        "tradeLong": "做多",
        "tradeShort": "做空",
        "tradeEntry": "入场",
        "tradeTarget": "目标",
        "tradeStop": "止损",
        "tradeRr": "盈亏比",
        "marketBasis": "数据时间",
        "marketEmpty": "暂无行情数据。",
        "chartHint": "点击卡片查看图表",
        "fgTitle": "恐惧与贪婪指数",
        "fgPrevClose": "前收盘",
        "fgPrev1w": "一周前",
        "fgPrev1m": "一个月前",
        "fgSource": "来源：CNN Fear & Greed Index",
        "fgRatingExtremeFear": "极度恐惧",
        "fgRatingFear": "恐惧",
        "fgRatingNeutral": "中性",
        "fgRatingGreed": "贪婪",
        "fgRatingExtremeGreed": "极度贪婪",
        "calSource": "来源：美联储 · 美国劳工统计局 · Finnhub",
        "calEmpty": "暂无可显示的日程。",
        "fomcTitle": "FOMC 例会",
        "fomcNext": "下次会议",
        "fomcLast": "上次会议",
        "fomcLive": "进行中",
        "fomcDone": "已结束",
        "fomcSep": "SEP",
        "fomcSepFull": "同时发布经济预测摘要（点阵图）— 市场影响更大",
        "indicatorsTitle": "美国最新指标发布",
        "earningsTitle": "重点财报",
        "earnBmo": "盘前",
        "earnAmc": "盘后",
        "earnDmh": "盘中",
        "calToday": "今天",
        "calTomorrow": "明天",
        "calTbd": "待定",
        "newsSource": "来源",
        "newsTierLegend": "T1 官方 · T2 核实 · T3 通讯社",
        "newsTier1": "T1 官方",
        "newsTier2": "T2 核实",
        "newsTier3": "T3 通讯社",
        "newsEmpty": "没有引用的新闻。",
        "scoreSummary": "最近 {n} 次方向命中率 {rate}% · 平均 Brier {brier}",
        "scoreNote": "以次日标普 500 收盘价校验方向判断。",
        "scoreHit": "命中",
        "scoreMiss": "未中",
        "scoreTooltip": "{date} · 预测 {call} {pct}% → 实际 {actual}",
        "scoreEmpty": "尚无评分记录。",
        "metaModel": "生成模型",
        "metaProvider": "生成方式",
        "metaNews": "新闻 {n} 条",
        "metaGenTime": "生成耗时",
        "metaUpdated": "最后更新",
        "metaBuild": "构建",
        "archiveTitle": "存档",
        "archiveAll": "查看全部存档",
        "archiveEmpty": "暂无已保存的简报。",
        "archiveCount": "共 {n} 篇",
        "prevBriefing": "更早",
        "nextBriefing": "更新",
        "filterAll": "全部",
        "watchTitle": "关注列表高亮",
        "watchToggle": "我的关注列表",
        "watchPlaceholder": "逗号分隔，例：NVDA, XLU, 半导体",
        "watchApply": "应用",
        "watchClear": "清除",
        "watchHint": "仅保存在浏览器本地，不会发送到服务器。",
        "watchMatchTrades": "相关交易思路",
        "watchMatchSectors": "相关板块",
        "watchNone": "本篇简报中未找到匹配项。",
        "watchCount": "匹配 {n} 处",
        "pwaHint": "添加到主屏幕，明早一键打开。",
        "pwaDismiss": "关闭",
        "shareBtn": "分享",
        "shareCopied": "链接已复制。",
        "chartLoading": "图表加载中...",
        "chartNoData": "暂无图表数据",
        "chartClose": "关闭",
        "chartTz": "时间基准",
        "chartError": "图表加载失败。",
        "swUpdated": "新版本已就绪，请刷新页面。",
        "disclaimerTitle": "投资风险提示",
        "disclaimerBody": (
            "本简报由 AI 基于公开新闻与市场数据自动生成，仅供参考，不构成投资建议或买卖要约。"
            "所载涨跌概率、目标价、止损位等全部内容不保证准确性或收益，且可能包含与事实不符的信息。"
            "投资决策及其后果由用户本人自行承担。"
        ),
        "disclaimerShort": "AI 自动生成的参考资料，不构成投资建议，投资风险由用户自行承担。",
        "footerSources": "数据来源：Yahoo Finance · Finnhub · CNN · 美联储 · 美国劳工统计局",
        "footerGenerated": "由本地 Claude Code CLI 生成",
        "footerBuild": "站点构建",
        "emptyTitle": "尚无简报",
        "emptyDesc": "每天早上自动生成，第一篇简报将显示在这里。",
    },
    # ==========================================================
    "ja": {
        "brandTitle": "AI モーニングブリーフ",
        "brandSubtitle": "市場動向",
        "navHome": "ホーム",
        "navArchive": "アーカイブ",
        "navRss": "RSS",
        "navGithub": "GitHub",
        "navLangLabel": "言語を選択",
        "navCurrencyLabel": "通貨を切り替え",
        "skipToContent": "本文へスキップ",
        "sectionMarket": "📊 マーケットスナップショット",
        "sectionSectors": "🏭 セクター ETF",
        "sectionBriefing": "🧠 AI モーニングブリーフィング",
        "sectionCalendar": "🗓️ 経済カレンダー",
        "sectionNews": "📰 根拠ニュース",
        "sectionScorecard": "🎯 方向性の的中率",
        "sectionArchive": "🗂️ アーカイブ",
        "part1": "PART 1 · 前日セッションの振り返り",
        "part2": "PART 2 · 夜間のイベントと主要人物",
        "part3": "PART 3 · 本日の方向性確率",
        "part4": "PART 4 · セクターリーダーシップ",
        "part5": "PART 5 · 主要リスク",
        "part6": "PART 6 · トレードアイデア",
        "part7": "PART 7 · 10秒ダッシュボード",
        "part8": "PART 8 · ポートフォリオ向け助言",
        "partRecap": "前日セッションの振り返り",
        "recapDrivers": "相場を動かした要因",
        "partActors": "相場を動かした人物",
        "actorImpact": "影響",
        "actorAssets": "関連資産",
        "dirUp": "上昇",
        "dirFlat": "横ばい",
        "dirDown": "下落",
        "dirCall": "本日の判断",
        "dirRationale": "根拠",
        "leadersTitle": "主導セクター",
        "laggardsTitle": "劣後セクター",
        "sectorHeatTitle": "🌡️ セクター過熱度",
        "sectorValuationTitle": "🧾 セクターバリュエーション",
        "themeHeatTitle": "🧩 テーマ・業種の過熱度",
        "themeValuationTitle": "🧩 テーマ・業種のバリュエーション",
        "themeNote": (
            "大分類をさらに細分した業種・テーマです。テーマは大分類の部分集合で互いに重複します"
            "（半導体はTechnologyの中にあります）。合算したり比率として読まないでください。"
        ),
        "heatHot": "過熱",
        "heatWarm": "やや過熱",
        "heatNeutral": "中立",
        "heatCool": "やや低調",
        "heatCold": "低調",
        "heatGap": "200日線乖離",
        "heatPercentile": "分布パーセンタイル",
        "heatRsi": "RSI(14)",
        "heatRel3m": "3か月相対",
        "heatNote": (
            "価格だけで算出した「どれだけ過熱しているか」の指標です。価値の判断ではありません。"
            "ラベルは、本日の200日線乖離がそのセクター自身の過去の乖離分布のどこに位置するか"
            "（パーセンタイル）だけで決まります。RSIと3か月相対リターンは参考値です。"
        ),
        "heatSampleDays": "観測 {n} 日",
        "valuationPer": "PER",
        "valuationPbr": "PBR",
        "valuationYield": "配当利回り",
        "valuationAccruing": "記録中",
        "valuationAccruingDays": "記録中（{n}日）",
        "valuationNote": (
            "現在のPER・PBRのみを表示します。自前の記録が十分に蓄積されるまでパーセンタイルは算出せず、"
            "いかなる価値判定も行いません。"
        ),
        "riskHigh": "高",
        "riskMedium": "中",
        "riskLow": "低",
        "tradeLong": "ロング",
        "tradeShort": "ショート",
        "tradeEntry": "エントリー",
        "tradeTarget": "目標",
        "tradeStop": "損切り",
        "tradeRr": "R:R",
        "marketBasis": "基準時刻",
        "marketEmpty": "市場データがありません。",
        "chartHint": "カードをタップするとチャートが開きます",
        "fgTitle": "恐怖・強欲指数",
        "fgPrevClose": "前日終値",
        "fgPrev1w": "1週間前",
        "fgPrev1m": "1か月前",
        "fgSource": "出典: CNN Fear & Greed Index",
        "fgRatingExtremeFear": "極度の恐怖",
        "fgRatingFear": "恐怖",
        "fgRatingNeutral": "中立",
        "fgRatingGreed": "強欲",
        "fgRatingExtremeGreed": "極度の強欲",
        "calSource": "出典: 米連邦準備制度 · 米労働統計局 · Finnhub",
        "calEmpty": "表示する日程がありません。",
        "fomcTitle": "FOMC 定例会合",
        "fomcNext": "次回会合",
        "fomcLast": "直近の会合",
        "fomcLive": "開催中",
        "fomcDone": "終了",
        "fomcSep": "SEP",
        "fomcSepFull": "経済見通し要約（ドットプロット）同時公表 — 市場影響大",
        "indicatorsTitle": "米国 最新指標の発表",
        "earningsTitle": "主要決算",
        "earnBmo": "寄前",
        "earnAmc": "引後",
        "earnDmh": "場中",
        "calToday": "今日",
        "calTomorrow": "明日",
        "calTbd": "未定",
        "newsSource": "出典",
        "newsTierLegend": "T1 公式 · T2 検証済 · T3 通信社",
        "newsTier1": "T1 公式",
        "newsTier2": "T2 検証済",
        "newsTier3": "T3 通信社",
        "newsEmpty": "引用ニュースはありません。",
        "scoreSummary": "直近 {n} 回の的中率 {rate}% · 平均 Brier {brier}",
        "scoreNote": "S&P 500 の方向性予測を翌セッションの終値で採点します。",
        "scoreHit": "的中",
        "scoreMiss": "外れ",
        "scoreTooltip": "{date} · 予測 {call} {pct}% → 実際 {actual}",
        "scoreEmpty": "採点履歴はまだありません。",
        "metaModel": "生成モデル",
        "metaProvider": "生成経路",
        "metaNews": "ニュース {n} 件",
        "metaGenTime": "生成所要時間",
        "metaUpdated": "最終更新",
        "metaBuild": "ビルド",
        "archiveTitle": "アーカイブ",
        "archiveAll": "アーカイブをすべて見る",
        "archiveEmpty": "保存されたブリーフィングはありません。",
        "archiveCount": "全 {n} 件",
        "prevBriefing": "前の記事",
        "nextBriefing": "次の記事",
        "filterAll": "すべて",
        "watchTitle": "ウォッチリストのハイライト",
        "watchToggle": "マイウォッチリスト",
        "watchPlaceholder": "カンマ区切り。例: NVDA, XLU, 半導体",
        "watchApply": "適用",
        "watchClear": "クリア",
        "watchHint": "ブラウザ内にのみ保存され、サーバーには送信されません。",
        "watchMatchTrades": "該当するトレードアイデア",
        "watchMatchSectors": "該当するセクター",
        "watchNone": "このブリーフィングに一致する項目はありません。",
        "watchCount": "{n} 件一致",
        "pwaHint": "ホーム画面に追加すると、明朝ワンタップで開けます。",
        "pwaDismiss": "閉じる",
        "shareBtn": "共有",
        "shareCopied": "リンクをコピーしました。",
        "chartLoading": "チャート読み込み中...",
        "chartNoData": "チャートデータなし",
        "chartClose": "閉じる",
        "chartTz": "時間基準",
        "chartError": "チャートを読み込めませんでした。",
        "swUpdated": "新しいバージョンがあります。再読み込みしてください。",
        "disclaimerTitle": "投資に関する注意事項",
        "disclaimerBody": (
            "本ブリーフィングは、AIが公開されたニュースおよび市場データを自動分析して生成した参考資料であり、"
            "投資助言や投資勧誘ではありません。記載された上昇・下落確率、目標価格、損切り水準を含むすべての内容は、"
            "正確性や利益を保証するものではなく、事実と異なる情報を含む可能性があります。"
            "投資判断およびその結果については、利用者ご自身が全責任を負います。"
        ),
        "disclaimerShort": "AIが生成した参考資料です。投資助言ではなく、投資判断の責任は利用者ご自身にあります。",
        "footerSources": "データ出典: Yahoo Finance · Finnhub · CNN · 米連邦準備制度 · 米労働統計局",
        "footerGenerated": "生成: ローカル Claude Code CLI",
        "footerBuild": "サイトビルド",
        "emptyTitle": "ブリーフィングはまだありません",
        "emptyDesc": "毎朝自動生成されます。最初のブリーフィングはここに表示されます。",
    },
}


@lru_cache(maxsize=16)
def get_labels(lang: str | None = None) -> dict[str, str]:
    """언어별 라벨 사전. 요청 언어 → en → ko 순으로 키 단위 병합한다."""
    # [SECURE] 언어 코드 화이트리스트 검증 - 사전 조회를 통한 예상 밖 입력 차단 (Category 1)
    code = (lang or DEFAULT_LANG).strip().lower()[:5]
    merged = dict(LABELS["ko"])
    merged.update(LABELS["en"])
    if code in LABELS:
        merged.update(LABELS[code])
    return merged


def html_lang(lang: str | None) -> str:
    return _HTML_LANG.get((lang or DEFAULT_LANG).lower(), "en")


def og_locale(lang: str | None) -> str:
    return _OG_LOCALE.get((lang or DEFAULT_LANG).lower(), "en_US")


# ===== 날짜 포맷 =====
_WEEKDAY_SHORT = {
    "ko": ("월", "화", "수", "목", "금", "토", "일"),
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "zh": ("周一", "周二", "周三", "周四", "周五", "周六", "周日"),
    "ja": ("月", "火", "水", "木", "金", "土", "日"),
}
_MONTH_FULL_EN = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_MONTH_SHORT_EN = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _parse(iso: str | None) -> datetime | None:
    """ISO 8601 / YYYY-MM-DD 문자열을 datetime으로. 실패 시 None."""
    if not iso:
        return None
    text = str(iso).strip()
    try:
        # 'Z' 접미사는 fromisoformat(3.11+)가 처리하지만 하위 호환을 위해 치환한다
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())
    except ValueError:
        # [SECURE] 파싱 실패를 삼키지 않고 호출자에게 None으로 명시 전달 (Category 4)
        return None


def _lang_key(lang: str | None) -> str:
    code = (lang or DEFAULT_LANG).lower()
    return code if code in _WEEKDAY_SHORT else "en"


def format_date(iso: str | None, lang: str | None = DEFAULT_LANG) -> str:
    """전체 날짜 + 요일. 파싱 실패 시 원문을 그대로 돌려준다(템플릿에서 이스케이프됨)."""
    dt = _parse(iso)
    if dt is None:
        return str(iso or "")
    code = _lang_key(lang)
    wd = _WEEKDAY_SHORT[code][dt.weekday()]
    if code == "ko":
        return f"{dt.year}년 {dt.month}월 {dt.day}일 ({wd})"
    if code == "zh":
        return f"{dt.year}年{dt.month}月{dt.day}日 ({wd})"
    if code == "ja":
        return f"{dt.year}年{dt.month}月{dt.day}日 ({wd})"
    return f"{_MONTH_FULL_EN[dt.month - 1]} {dt.day}, {dt.year} ({wd})"


def format_short_date(iso: str | None, lang: str | None = DEFAULT_LANG) -> str:
    dt = _parse(iso)
    if dt is None:
        return str(iso or "")
    code = _lang_key(lang)
    wd = _WEEKDAY_SHORT[code][dt.weekday()]
    if code == "ko":
        return f"{dt.month}월 {dt.day}일 ({wd})"
    if code in ("zh", "ja"):
        return f"{dt.month}月{dt.day}日 ({wd})"
    return f"{_MONTH_SHORT_EN[dt.month - 1]} {dt.day} ({wd})"


def format_month(ym: str | None, lang: str | None = DEFAULT_LANG) -> str:
    """'YYYY-MM' → 로케일 월 표기."""
    text = str(ym or "")
    dt = _parse(text if len(text) > 7 else text + "-01")
    if dt is None:
        return text
    code = _lang_key(lang)
    if code == "ko":
        return f"{dt.year}년 {dt.month}월"
    if code in ("zh", "ja"):
        return f"{dt.year}年{dt.month}月"
    return f"{_MONTH_FULL_EN[dt.month - 1]} {dt.year}"


def format_time(iso: str | None, lang: str | None = DEFAULT_LANG) -> str:
    dt = _parse(iso)
    if dt is None:
        return ""
    return f"{dt.hour:02d}:{dt.minute:02d}"


def format_datetime(iso: str | None, lang: str | None = DEFAULT_LANG) -> str:
    dt = _parse(iso)
    if dt is None:
        return str(iso or "")
    stamp = format_date(iso, lang)
    return f"{stamp} {dt.hour:02d}:{dt.minute:02d}"


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Improper Input for Security Decision: 언어 코드는 길이 제한 + 사전 키 화이트리스트로만 사용
#   - Missing Error Handling: 날짜 파싱 실패를 명시적으로 처리해 원문 문자열을 반환 (예외 전파 없음)
#   - Improper Exception Handling: except 블록이 비어 있지 않으며 반환값으로 결과를 전달
#   - Error Message Information Exposure: 파싱 실패 시 내부 예외 메시지를 노출하지 않는다
# Not Applied:
#   - [WARN] 라벨 문자열은 HTML 이스케이프하지 않는다. Jinja2 autoescape가 출력 시점에 처리하므로
#            템플릿에서 이 값에 |safe 를 적용해서는 안 된다.
# --------------------------------------------------
