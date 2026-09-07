"""방향성 적중 스코어카드.

어제(또는 최근) 브리핑의 PART 2 방향성 확률을, 그 다음 세션의 S&P 500 등락으로 채점한다.
  - 실제 방향 : changePercent >= +0.3% → up, <= -0.3% → down, 그 사이는 flat
  - 적중      : 최대 확률 시나리오 == 실제 방향
  - Brier     : sum over (up, flat, down) of (p/100 - y)^2  — 0이 완벽, 2가 최악

compute_scorecard()는 순수 함수(입출력 없음)이고,
update_scorecard()가 store에서 현재 스코어카드를 읽어 채점 후 저장하는 얇은 래퍼다.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from mbrief import store
from mbrief.config import settings
from mbrief.schemas import (
    BriefingRecord,
    Call,
    Direction,
    MarketBundle,
    ScoreEntry,
    Scorecard,
)
from mbrief.timeutil import now_iso, parse_iso_date, today_date

logger = logging.getLogger(__name__)

SPX_KEY = "S&P 500"
UP_THRESHOLD = 0.3      # % — 이상이면 상승
DOWN_THRESHOLD = -0.3   # % — 이하이면 하락
MAX_LOOKBACK_DAYS = 4   # 이보다 오래된 브리핑은 채점하지 않는다 (연휴·장기 중단 대비)
DEFAULT_SAMPLE_WINDOW = 30
MAX_ENTRIES = 400       # 스코어카드 파일 크기 상한 (오래된 항목부터 버린다)
SATURDAY = 5            # date.weekday(): 0=월 ... 5=토, 6=일


def classify(change_percent: float) -> Call:
    """등락률 → 실제 방향."""
    if change_percent >= UP_THRESHOLD:
        return "up"
    if change_percent <= DOWN_THRESHOLD:
        return "down"
    return "flat"


def brier_score(probs: Direction, actual: Call) -> float:
    """확률 예측 품질. 낮을수록 좋다 (0~2)."""
    total = 0.0
    for key in ("up", "flat", "down"):
        p = getattr(probs, key) / 100.0
        y = 1.0 if key == actual else 0.0
        total += (p - y) ** 2
    return round(total, 4)


def _spx_change(market: MarketBundle | None) -> float | None:
    """채점에 쓸 S&P 500 등락률. 시세가 없거나 거래가 없었으면 None."""
    if market is None:
        return None
    quote = market.quotes.get(SPX_KEY)
    if quote is None or quote.price is None or quote.changePercent is None:
        logger.info("[Scorecard] S&P 500 시세 없음 - 채점 건너뜀")
        return None
    # price == previous close(= change 0 / 등락률 0)이면 휴장·미체결로 보고 채점하지 않는다.
    if quote.change is not None and quote.change == 0:
        logger.info("[Scorecard] S&P 500 변동 없음(휴장 추정) - 채점 건너뜀")
        return None
    if quote.changePercent == 0:
        logger.info("[Scorecard] S&P 500 등락률 0%%(휴장 추정) - 채점 건너뜀")
        return None
    return float(quote.changePercent)


def _market_date(market: MarketBundle | None) -> date | None:
    """시세 스냅샷 기준일(KST). 없거나 형식이 틀리면 None."""
    if market is None:
        return None
    # [SECURE] 외부 타임스탬프를 ISO 8601로 파싱한 값만 비교에 사용 - Improper Input for Security Decision (Category 1)
    day = parse_iso_date(market.updatedAt)
    if day is None:
        logger.warning("[Scorecard] 시세 스냅샷 시각 형식 오류 - 채점 건너뜀")
    return day


def _pick_target(
    records: list[BriefingRecord],
    today: date,
    scored_ids: set[str],
    market_date: date,
) -> BriefingRecord | None:
    """채점 대상 레코드를 고른다.

    조건 (모두 만족해야 한다):
      - 기본 언어 + 구조화 레코드
      - 브리핑 날짜가 평일(월~금). 토·일 브리핑은 예측 대상 세션이 없으므로 채점하지 않는다
      - 최근 MAX_LOOKBACK_DAYS 이내이며 오늘보다 이전
      - 시세 스냅샷 기준일이 브리핑 날짜보다 **엄격히 이후**이고 MAX_LOOKBACK_DAYS 이내
        (토요일 스냅샷은 금요일 종가를 담으므로 금요일 브리핑 채점에 쓸 수 있다)

    조건을 만족하는 후보가 여럿이면 **가장 최신 1건만** 고른다. 더 오래된 후보는 버린다.
    이미 채점된 레코드가 최신 후보이면 None을 반환한다 — 뒤로 물러나 오래된 브리핑을
    엉뚱한(더 나중의) 세션으로 채점하는 look-ahead 오류를 원천 차단하기 위함이다.
    """
    lang = settings.primary_lang
    oldest = today - timedelta(days=MAX_LOOKBACK_DAYS)

    candidates: list[tuple[date, str, BriefingRecord]] = []
    for record in records:
        if record.lang != lang or record.structured is None:
            continue
        try:
            record_date = date.fromisoformat(record.date)
        except ValueError:
            logger.warning("[Scorecard] 날짜 형식 오류 - 건너뜀: %s", record.id)
            continue
        if record_date.weekday() >= SATURDAY:
            logger.info("[Scorecard] 주말 브리핑은 채점 대상이 아님 - 건너뜀: %s", record.id)
            continue
        if not (oldest <= record_date < today):
            continue
        # 스냅샷이 브리핑 다음 세션을 담고 있어야 한다 (같은 날/이전 날 스냅샷은 look-ahead 없음이 아니라 미래 미확정).
        if not (record_date < market_date <= record_date + timedelta(days=MAX_LOOKBACK_DAYS)):
            continue
        candidates.append((record_date, record.generatedAt, record))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    newest = candidates[0][2]
    if len(candidates) > 1:
        dropped = ", ".join(r.id for _, _, r in candidates[1:])
        logger.info("[Scorecard] 최신 1건만 채점하고 나머지는 버림: %s", dropped)
    if newest.id in scored_ids:
        return None
    return newest


def _recompute_stats(sc: Scorecard) -> Scorecard:
    """최근 sampleWindow 항목으로 적중률·평균 Brier 재계산."""
    window = sc.sampleWindow if sc.sampleWindow > 0 else DEFAULT_SAMPLE_WINDOW
    sample = sc.entries[:window]
    sc.sampleSize = len(sample)
    if sample:
        sc.hitRate = round(sum(1 for e in sample if e.hit) / len(sample), 4)
        sc.avgBrier = round(sum(e.brier for e in sample) / len(sample), 4)
    else:
        sc.hitRate = None
        sc.avgBrier = None
    sc.updatedAt = now_iso()
    return sc


def compute_scorecard(
    records: list[BriefingRecord],
    market: MarketBundle | None,
    today: date,
    current: Scorecard | None = None,
) -> Scorecard:
    """순수 채점 함수 — 파일 입출력 없음.

    채점 대상이 없거나 시세가 없으면 통계만 재계산한 스코어카드를 반환한다.
    """
    sc = (current.model_copy(deep=True) if current is not None
          else Scorecard(updatedAt=now_iso(), sampleWindow=DEFAULT_SAMPLE_WINDOW))

    market_date = _market_date(market)
    if market_date is None:
        logger.info("[Scorecard] 시세 스냅샷 기준일 없음 - 채점 건너뜀")
        return _recompute_stats(sc)

    scored_ids = {e.briefingId for e in sc.entries}
    target = _pick_target(records, today, scored_ids, market_date)
    if target is None or target.structured is None:
        logger.info("[Scorecard] 채점 대상 없음")
        return _recompute_stats(sc)

    change_pct = _spx_change(market)
    if change_pct is None:
        return _recompute_stats(sc)

    probs = target.structured.direction
    actual = classify(change_pct)
    predicted: Call = probs.call

    entry = ScoreEntry(
        date=target.date,
        briefingId=target.id,
        predicted=predicted,
        probs=probs,
        actual=actual,
        spxChangePercent=round(change_pct, 4),
        scoredAt=now_iso(),
        hit=(predicted == actual),
        brier=brier_score(probs, actual),
    )
    sc.entries.insert(0, entry)  # 최신순 유지
    if len(sc.entries) > MAX_ENTRIES:
        # [SECURE] 스코어카드 파일 무한 증가 방지 - 자원 고갈/파서 부하 차단 (Category 1)
        removed = len(sc.entries) - MAX_ENTRIES
        del sc.entries[MAX_ENTRIES:]
        logger.info("[Scorecard] 항목 상한(%d) 초과 - 오래된 %d건 제거", MAX_ENTRIES, removed)
    logger.info(
        "[Scorecard] 채점: %s 예측=%s 실제=%s (%+.2f%%) hit=%s brier=%.4f",
        target.id, predicted, actual, change_pct, entry.hit, entry.brier,
    )
    return _recompute_stats(sc)


def update_scorecard(
    records: list[BriefingRecord],
    market: MarketBundle | None,
    today: date,
) -> Scorecard:
    """현재 스코어카드를 읽어 채점하고 저장한다."""
    current = store.load_scorecard()
    updated = compute_scorecard(records, market, today, current)
    store.save_scorecard(updated)
    return updated


def run() -> Scorecard:
    """CLI `score` 명령용 얇은 래퍼 — 레코드·시세를 store에서 읽어 채점한다."""
    records = store.list_records()
    market = store.load_market()
    return update_scorecard(records, market, today_date())


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Improper Input for Security Decision: 레코드 날짜·시세 스냅샷 시각을 ISO 8601로 파싱 검증한 뒤에만
#     비교에 사용. 주말 브리핑과 스냅샷 기준일이 브리핑 날짜 이하인 경우는 채점에서 제외 (look-ahead 차단)
#   - Missing Error Handling: 시세 결측·휴장·날짜 오류를 각각 명시적으로 처리하고 채점을 건너뜀
#   - Null Pointer Dereference: market/quote/structured의 None을 사용 전에 모두 검사
#   - Infinite Loop: 유한 레코드 목록 순회, 통계는 sampleWindow로 상한
#   - Improper Exception Handling: 빈 except 없음, 형식 오류는 경고 로깅 후 건너뜀
#   - Private Array Returned: current.model_copy(deep=True)로 입력 스코어카드를 변형하지 않음
#   - 자원 고갈 방지: entries를 MAX_ENTRIES(400)로 상한 처리해 스코어카드 파일 무한 증가를 막음
# Not Applied:
#   - [WARN] 채점 기준(±0.3%)은 고정 상수다. 시장 변동성 레짐이 바뀌면 재조정이 필요하다.
#   - [WARN] 휴장 판정은 change == 0 / changePercent == 0 휴리스틱에 의존한다.
#     거래소 캘린더를 쓰면 더 정확하다.
#   - [WARN] '스냅샷 기준일이 브리핑 다음 날 이후'는 근사치다. 정확한 세션 매칭은 거래소 캘린더가 필요하다.
# --------------------------------------------------
