"""브리핑 생성 파이프라인.

refresh  : 시세·섹터·공포탐욕·캘린더 + 차트(12심볼x3기간) → data/cache/
generate : 뉴스 수집 → 필터 → LLM(구조화) → data/briefings/<id>.json (+ 추가 언어 번역)
daily    : 요일 게이트 + 단일 실행 lock → refresh → score → generate → build → publish → notify

--offline은 네트워크를 전혀 쓰지 않고 tests/fixtures의 실측 데이터를 사용한다.
--mock은 LLM 호출 없이 tests/fixtures/briefing_sample.json의 구조화 본문을 사용한다.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from pydantic import ValidationError

from mbrief import marketclock, paths, scorecard, store
from mbrief.config import settings
from mbrief.llm import engine, prompts, translate
from mbrief.schemas import BriefingRecord, MarketBundle, StructuredBriefing
from mbrief.sources import calendar_service, market_data, news_collector, news_filter
from mbrief.timeutil import now_iso, today_date, today_str

logger = logging.getLogger(__name__)

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
LOCK_STALE_SEC = 2 * 3600  # 2시간 넘은 lock은 죽은 프로세스로 보고 제거
# [SECURE] lock 파일 내용은 신뢰 경계 밖 - 숫자 형식만 PID로 인정 (Category 1)
_PID_RE = re.compile(r"^[0-9]{1,10}$")

EXIT_OK = 0
EXIT_SKIPPED = 0     # 요일 게이트로 건너뜀 — 정상 종료로 취급 (작업 스케줄러 오류 알림 방지)
EXIT_LOCKED = 2
EXIT_FAILED = 1

# ===== 사용자 안내 큐 =====
# 실행을 중단시키지는 않지만 사용자가 반드시 알아야 하는 사실(예: 로컬 CLI 미로그인으로
# API 폴백 사용)을 모아 두었다가 CLI 출력·텔레그램 알림으로 표면화한다.
_notices: list[str] = []


def add_notice(message: str) -> None:
    """안내 추가 (중복 제거). 로그에도 경고로 남긴다."""
    text = (message or "").strip()
    if text and text not in _notices:
        _notices.append(text)
        logger.warning("[Pipeline] 안내: %s", text)


def peek_notices() -> list[str]:
    """쌓인 안내를 비우지 않고 조회한다."""
    return list(_notices)


def take_notices() -> list[str]:
    """쌓인 안내를 반환하고 큐를 비운다 (CLI가 출력 직전에 호출)."""
    items = list(_notices)
    _notices.clear()
    return items


# ===== 픽스처 (offline / mock) =====
def _fixture_path(name: str) -> Path:
    """[SECURE] 픽스처 파일명을 상수 호출부에서만 받아 고정 디렉터리와 조합 - Path Traversal 방지."""
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError("허용되지 않은 픽스처 이름입니다.")
    return paths.ROOT / "tests" / "fixtures" / name


def load_fixture(name: str) -> object:
    """픽스처 JSON 로드. 없으면 FileNotFoundError."""
    path = _fixture_path(name)
    if not path.is_file():
        raise FileNotFoundError(f"픽스처가 없습니다: tests/fixtures/{name}")
    with path.open("r", encoding="utf-8") as f:  # [SECURE] context manager로 파일 핸들 해제 (Category 5)
        return json.load(f)


def _offline_bundle() -> tuple[MarketBundle, dict[str, list[dict]]]:
    """픽스처로 MarketBundle + 차트를 구성한다 (네트워크 없음)."""
    snapshot = load_fixture("snapshot.json")
    sectors = load_fixture("sectors.json")
    fear_greed = load_fixture("fear_greed.json")
    calendar = load_fixture("calendar.json")
    charts: dict[str, list[dict]] = {}
    try:
        candles = load_fixture("chart_spx_1d.json")
        if isinstance(candles, list):
            charts["sp500_1d"] = candles
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        logger.warning("[Pipeline] 오프라인 차트 픽스처 없음: %s", e)

    bundle = market_data.to_bundle(
        snapshot if isinstance(snapshot, dict) else {},
        sectors if isinstance(sectors, dict) else {},
        fear_greed if isinstance(fear_greed, dict) else None,
        calendar if isinstance(calendar, dict) else {},
        chart_keys=list(charts),
    )
    return bundle, charts


# ===== refresh =====
async def record_sector_valuation(bundle: MarketBundle) -> int:
    """오늘의 섹터 PER/PBR을 append-only 기록에 남기고 bundle.sectorValuation을 채운다.

    '저평가/고평가'는 과거 분포 없이 말할 수 없다. 오늘은 관측치 한 줄을 더 쌓는 날이고,
    백분위(pePercentile)는 MIN_VALUATION_DAYS일이 모인 뒤에야 채워진다.
    이 단계가 실패해도 refresh 전체를 중단시키지 않는다 (시세·차트는 이미 유효하다).

    Returns: 이번 실행에서 새로 기록된 줄 수 (중복 실행이면 0).
    """
    try:
        fundamentals = await market_data.get_sector_fundamentals()
    except Exception as e:
        # [SECURE] 외부 라이브러리 예외 계층을 알 수 없어 광범위하게 잡되 반드시 로깅 (Category 4)
        logger.exception("[Pipeline] 섹터 밸류에이션 수집 실패(계속 진행): %s", e)
        return 0

    written = 0
    try:
        rows = market_data.valuation_rows(fundamentals, today_str())
        written = store.append_valuation(rows)
    except (store.StoreError, ValueError, TypeError) as e:
        # 기록에 실패해도 오늘 수치는 화면에 보여 준다 (분포만 하루치 비게 된다).
        logger.warning("[Pipeline] 밸류에이션 기록 실패(계속 진행): %s", e)

    try:
        history_rows = store.load_valuation()
        bundle.sectorValuation = market_data.build_sector_valuation(fundamentals, history_rows)
    except (store.StoreError, ValueError, TypeError) as e:
        logger.warning("[Pipeline] 밸류에이션 구성 실패(계속 진행): %s", e)
    return written


async def refresh(*, offline: bool = False) -> MarketBundle:
    """시세 스냅샷 + 차트를 갱신해 data/cache/에 저장하고 번들을 반환한다.

    offline 모드는 네트워크를 전혀 쓰지 않으므로 밸류에이션 기록도 하지 않는다
    (픽스처 값을 실제 기록에 섞으면 분포가 오염된다).
    """
    started = time.time()
    recorded = 0

    if offline:
        bundle, charts = _offline_bundle()
        logger.info("[Pipeline] refresh(offline) - 픽스처 사용")
    else:
        bundle = await market_data.build_market_bundle()
        charts = await market_data.get_all_charts()
        recorded = await record_sector_valuation(bundle)

    saved_keys: list[str] = []
    for key, candles in charts.items():
        try:
            store.save_chart(key, candles)
            saved_keys.append(key)
        except (ValueError, store.StoreError) as e:
            # [SECURE] 개별 차트 저장 실패를 격리 - 전체 갱신 중단 방지 (Category 4)
            logger.warning("[Pipeline] 차트 저장 실패 %s: %s", key, e)

    bundle.chartKeys = sorted(saved_keys)
    store.save_market(bundle)
    logger.info(
        "[Pipeline] refresh 완료 %.1fs (시세 %d, 섹터 %d, 차트 %d, 과열도 %d, 밸류에이션 %d/신규기록 %d)",
        time.time() - started, len(bundle.quotes), len(bundle.sectors), len(saved_keys),
        len(bundle.sectorHeat), len(bundle.sectorValuation), recorded,
    )
    return bundle


# ===== generate =====
def _market_clock() -> marketclock.MarketClock:
    """캐시된 S&P 500 차트로 현재 시장 상태를 판정한다.

    5일 차트를 우선 쓴다 (연휴로 여러 날 쉬어도 직전 세션을 찾을 수 있다).
    차트가 없으면 시각만으로 판정한 시계를 돌려준다.
    """
    charts = store.load_charts()
    candles = charts.get("sp500_5d") or charts.get("sp500_1d") or []
    return marketclock.build_clock(candles)


def _market_text(bundle: MarketBundle) -> str:
    """캘린더(T1) + 시세 + 계산된 섹터 지표(T1) 텍스트.

    섹터 과열도/밸류에이션은 파이썬이 이미 계산한 값이다. 모델에는 캘린더와 같은 T1 프레이밍으로
    '인용할 사실'로만 넘기며, 다시 판정하거나 지어내지 말라고 명시한다 (prompts.format_sector_context).
    """
    snapshot = {name: quote.model_dump() for name, quote in bundle.quotes.items()}
    fear_greed = bundle.fearGreed.model_dump() if bundle.fearGreed else None
    text = (
        calendar_service.format_for_llm(bundle.calendar)
        + market_data.format_for_llm(snapshot, fear_greed)
    )
    sector_context = prompts.format_sector_context(
        bundle.sectorHeat, bundle.sectorValuation, market_data.MIN_VALUATION_DAYS,
    )
    return f"{text}\n{sector_context}" if sector_context else text


async def _collect_news(offline: bool) -> list[dict]:
    """뉴스 수집. offline이면 픽스처를 그대로 사용한다."""
    if offline:
        raw = load_fixture("news_raw.json")
        items = raw if isinstance(raw, list) else []
        logger.info("[Pipeline] 뉴스(offline) 픽스처 %d건", len(items))
        return items
    return await news_collector.collect_all()


def _mock_structured() -> StructuredBriefing:
    """mock 모드용 구조화 본문 (실측 픽스처)."""
    data = load_fixture("briefing_sample.json")
    if not isinstance(data, dict) or not isinstance(data.get("structured"), dict):
        raise RuntimeError("briefing_sample.json에 structured 본문이 없습니다.")
    # [SECURE] 픽스처도 pydantic으로 검증 후 사용 - Unsafe Deserialization 방지 (Category 5)
    return StructuredBriefing.model_validate(data["structured"])


def _build_record(
    *,
    lang: str,
    provider: str,
    model: str,
    elapsed_ms: int,
    filtered: list[dict],
    structured: StructuredBriefing | None,
    content_markdown: str,
    bundle: MarketBundle,
    translated_from: str | None = None,
) -> BriefingRecord:
    """레코드 조립. contentMarkdown 렌더는 **이 함수 한 곳에서만** 수행한다.

    [SECURE] PART 8 공개 여부를 settings.publish_portfolio_note 한 군데에서만 판단해
    렌더 경로 분기로 인한 비공개 데이터 노출을 막는다 (Category 2: Improper Authorization).
    """
    include_portfolio = settings.publish_portfolio_note
    if structured is not None:
        markdown = prompts.render_markdown(structured, lang, include_portfolio)
        headline = structured.headline
    else:
        markdown = content_markdown
        headline = " ".join(content_markdown.split())[:120]

    date_str = today_str()
    return BriefingRecord(
        id=store.new_record_id(date_str, lang),
        date=date_str,
        lang=lang,
        generatedAt=now_iso(),
        provider=provider,
        model=model,
        generationTimeMs=max(0, int(elapsed_ms)),
        newsCount=len(filtered),
        tierCounts=news_filter.tier_counts(filtered),
        headline=headline,
        structured=structured,
        contentMarkdown=markdown,
        market=bundle,
        topNews=news_filter.to_news_refs(filtered, limit=12),
        translatedFrom=translated_from,
    )


ALREADY_GENERATED_MSG = "오늘 브리핑이 이미 있어 생성을 건너뜀 (--force로 재생성)"


def todays_records(lang: str) -> list[BriefingRecord]:
    """해당 언어의 '오늘(KST) 날짜' 레코드 목록. 재생성 여부 판단에 쓴다."""
    today = today_str()
    return [r for r in store.list_records(lang=lang) if r.date == today]


def _resolve_langs(langs: list[str] | None) -> list[str]:
    """[SECURE] 언어 코드 화이트리스트 검증 - 임의 파일명 생성 방지 (Category 1)."""
    requested = langs if langs else settings.langs
    result: list[str] = []
    for raw in requested:
        code = str(raw).strip().lower()
        if code in prompts.PART_TITLES and code not in result:
            result.append(code)
        elif code not in prompts.PART_TITLES:
            logger.warning("[Pipeline] 지원하지 않는 언어 무시: %r", code[:10])
    return result or [prompts.DEFAULT_LANG]


async def generate(
    langs: list[str] | None = None,
    *,
    mock: bool = False,
    offline: bool = False,
    model: str | None = None,
    market: MarketBundle | None = None,
    force: bool = False,
) -> list[BriefingRecord]:
    """브리핑 생성. 첫 언어로 분석하고 나머지 언어는 번역 패스로 만든다.

    market을 넘기면 refresh를 다시 호출하지 않는다 (daily에서 재수집 방지).
    오늘 날짜의 기본 언어 레코드가 이미 있으면 재생성하지 않고 그대로 반환한다 (force=True면 무시).
    """
    lang_list = _resolve_langs(langs)
    primary = lang_list[0]
    _notices.clear()

    existing = todays_records(primary)
    if existing and not force:
        logger.info("[Pipeline] %s: %s", ALREADY_GENERATED_MSG, existing[0].id)
        return existing

    step_started = time.time()

    # 1) 시세
    bundle = market if market is not None else await refresh(offline=offline)
    logger.info("[Pipeline] 1/5 시세 준비 %.1fs", time.time() - step_started)

    # 2) 뉴스 수집 + 필터
    step_started = time.time()
    raw_news = await _collect_news(offline)
    filtered = news_filter.filter_news(raw_news)
    news_text = news_filter.format_for_llm(filtered)
    logger.info("[Pipeline] 2/5 뉴스 %d건 → %d건 %.1fs", len(raw_news), len(filtered), time.time() - step_started)

    # 3) 프롬프트 입력 구성
    step_started = time.time()
    market_text = _market_text(bundle)
    clock = _market_clock()
    clock_text = marketclock.format_for_llm(clock)
    logger.info("[Pipeline] 시장 시계: %s | 마지막 세션 %s | 시세=%s",
                clock.state, clock.last_session or "미상",
                "장중 실시간" if clock.quotes_are_live else "직전 종가")
    portfolio_text = store.format_portfolio_for_llm(store.load_portfolio())
    if portfolio_text:
        logger.info("[Pipeline] 포트폴리오 로드됨 - PART 8 생성 요청 (공개 여부: %s)",
                    settings.publish_portfolio_note)
    logger.info("[Pipeline] 3/5 프롬프트 준비 %.1fs", time.time() - step_started)

    # 4) 생성
    step_started = time.time()
    if mock:
        structured = _mock_structured()
        result_provider, result_model = "mock", "mock-fixture"
        content_markdown = ""
        elapsed_ms = int((time.time() - step_started) * 1000)
        logger.info("[Pipeline] 4/5 생성(mock) - LLM 호출 없음")
    else:
        llm_result = await engine.generate_briefing(
            news_text, market_text, portfolio_text, primary, model=model,
            clock_text=clock_text,
        )
        structured = llm_result.structured
        result_provider, result_model = llm_result.provider, llm_result.model
        # 엔진은 렌더하지 않는다. 구조화 실패 시의 원문만 받아 _build_record가 최종 마크다운을 만든다.
        content_markdown = llm_result.text
        elapsed_ms = llm_result.elapsed_ms
        # 로컬 CLI를 쓰지 못하고 API 폴백으로 생성했다면 그 사실을 사용자에게 표면화한다.
        add_notice(llm_result.notice)
        logger.info("[Pipeline] 4/5 생성 완료 %.1fs provider=%s model=%s",
                    time.time() - step_started, result_provider, result_model)

    primary_record = _build_record(
        lang=primary,
        provider=result_provider,
        model=result_model,
        elapsed_ms=elapsed_ms,
        filtered=filtered,
        structured=structured,
        content_markdown=content_markdown,
        bundle=bundle,
    )
    store.save_record(primary_record)
    records = [primary_record]

    # 5) 추가 언어
    step_started = time.time()
    for lang in lang_list[1:]:
        record = await _make_translated(primary_record, lang, mock=mock, bundle=bundle, filtered=filtered)
        if record is not None:
            store.save_record(record)
            records.append(record)
    if len(lang_list) > 1:
        logger.info("[Pipeline] 5/5 번역 %d/%d개 %.1fs",
                    len(records) - 1, len(lang_list) - 1, time.time() - step_started)

    logger.info("[Pipeline] generate 완료: %s", ", ".join(f"{r.id}({r.lang})" for r in records))
    return records


async def _make_translated(
    source: BriefingRecord,
    lang: str,
    *,
    mock: bool,
    bundle: MarketBundle,
    filtered: list[dict],
) -> BriefingRecord | None:
    """번역 레코드 생성. 실패하면 None."""
    if source.structured is None:
        logger.warning("[Pipeline] 원본이 텍스트 폴백이라 %s 번역을 건너뜁니다.", lang)
        return None

    if mock:
        translated = source.structured  # mock은 번역하지 않고 언어만 바꿔 복제한다
    else:
        translated = await translate.translate_structured(source.structured, source.lang, lang)
        if translated is None:
            return None

    try:
        record = _build_record(
            lang=lang,
            provider=source.provider,
            model=source.model if mock else settings.claude_cli_translate_model,
            elapsed_ms=0,
            filtered=filtered,
            structured=translated,
            content_markdown="",
            bundle=bundle,
            translated_from=source.id,
        )
    except (ValueError, ValidationError) as e:
        logger.warning("[Pipeline] %s 번역 레코드 생성 실패: %s", lang, e)
        return None
    return record


# ===== daily =====
def _is_active_today() -> bool:
    """settings.active_days 요일 게이트."""
    weekday = WEEKDAYS[today_date().weekday()]
    active = settings.active_days
    if not active:
        return True
    return weekday in active


def _pid_alive(pid: int) -> bool | None:
    """PID 생존 여부. 판별할 수 없으면 None (호출부는 시간 기준으로 대체 판단한다).

    psutil 없이 표준 라이브러리만 쓴다. Windows는 ctypes OpenProcess, 그 외는 os.kill(pid, 0).
    """
    if pid <= 0:
        return None

    if sys.platform == "win32":
        try:
            import ctypes  # noqa: PLC0415 - Windows 전용 경로에서만 필요

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            # PROCESS_QUERY_LIMITED_INFORMATION(0x1000): 권한 상승 없이 조회 가능
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if handle:
                exit_code = ctypes.c_ulong(0)
                alive = True
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    alive = exit_code.value == 259  # STILL_ACTIVE
                kernel32.CloseHandle(handle)
                return alive
            error = ctypes.get_last_error()
            if error == 87:   # ERROR_INVALID_PARAMETER - 해당 PID의 프로세스가 없다
                return False
            if error == 5:    # ERROR_ACCESS_DENIED - 존재하지만 조회 권한이 없다
                return True
            logger.debug("[Pipeline] OpenProcess 실패(pid=%s, err=%s)", pid, error)
            return None
        except (OSError, ValueError, AttributeError) as e:
            # [SECURE] 빈 except 없음 - 판별 실패는 로깅 후 '알 수 없음'으로 처리 (Category 4)
            logger.debug("[Pipeline] PID 생존 확인 실패(pid=%s): %s", pid, e)
            return None

    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError) as e:
        logger.debug("[Pipeline] PID 생존 확인 실패(pid=%s): %s", pid, e)
        return None
    return True


class _DailyLock:
    """단일 실행 lock 파일.

    [SECURE] os.open(O_CREAT|O_EXCL)로 원자적 생성 - Race Condition(TOCTOU) 방지 (Category 3)

    stale 판정은 두 가지다:
      1. lock에 기록된 PID가 살아있지 않으면 즉시 stale (정전·강제 종료 후 빠른 복구)
      2. PID를 판별할 수 없으면 기존대로 stale_sec(기본 2시간) 경과 여부로 판단
    """

    def __init__(self, path: Path, stale_sec: int = LOCK_STALE_SEC) -> None:
        self.path = path
        self.stale_sec = stale_sec
        self.acquired = False

    def _read_pid(self) -> int | None:
        """lock 파일 첫 토큰을 PID로 읽는다. 형식이 아니면 None."""
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("[Pipeline] lock 파일 읽기 실패: %s", e)
            return None
        tokens = text.split()
        if not tokens or not _PID_RE.match(tokens[0]):
            return None
        return int(tokens[0])

    def _clear_stale(self) -> None:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return

        pid = self._read_pid()
        alive = _pid_alive(pid) if pid is not None else None
        if alive is False:
            reason = f"기록된 PID {pid} 없음"
        elif age > self.stale_sec:
            reason = f"{age / 60:.0f}분 경과"
        else:
            return

        logger.warning("[Pipeline] 오래된 lock 제거 (%s)", reason)
        try:
            self.path.unlink()
        except OSError as e:
            logger.warning("[Pipeline] lock 제거 실패: %s", e)

    def __enter__(self) -> "_DailyLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):  # [SECURE] 재시도 횟수 고정 - 무한 루프 방지 (Category 3)
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(f"{os.getpid()} {now_iso()}\n")
                self.acquired = True
                return self
            except FileExistsError:
                if attempt == 0:
                    self._clear_stale()
                    continue
                logger.error("[Pipeline] 이미 실행 중입니다 (lock: %s)", self.path.name)
                return self
            except OSError as e:
                logger.error("[Pipeline] lock 생성 실패: %s", e)
                return self
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()  # [SECURE] finally 경로에서 확실히 해제 (Category 5)
        except OSError as e:
            logger.warning("[Pipeline] lock 해제 실패: %s", e)


def build_site() -> bool:
    """정적 사이트 빌드 (site 패키지는 지연 임포트)."""
    try:
        from mbrief.site.builder import build_from_data
    except ImportError as e:
        logger.error("[Pipeline] 사이트 빌더를 불러올 수 없습니다 (mbrief/site/builder.py 확인): %s", e)
        return False
    try:
        build_from_data()
    except Exception as e:  # 빌더 예외 타입을 알 수 없으므로 광범위하게 잡되 반드시 로깅한다
        logger.exception("[Pipeline] 사이트 빌드 실패: %s", e)
        return False
    logger.info("[Pipeline] 사이트 빌드 완료: %s", paths.SITE_DIR)
    return True


async def daily(
    *,
    no_publish: bool = False,
    mock: bool = False,
    offline: bool = False,
    force: bool = False,
) -> int:
    """일일 전체 파이프라인. 종료 코드를 반환한다.

    멱등: 오늘 날짜의 기본 언어 브리핑이 이미 있으면 생성 단계를 건너뛰고
    build → publish → notify만 다시 수행한다 (배포 실패 후 재시도해도 하루에 두 건이 생기지 않는다).
    --force로 재생성할 수 있다.
    """
    if not _is_active_today():
        logger.info("[Pipeline] 오늘(%s)은 실행 요일이 아닙니다 - 건너뜀",
                    WEEKDAYS[today_date().weekday()])
        return EXIT_SKIPPED

    with _DailyLock(paths.LOCK_FILE) as lock:
        if not lock.acquired:
            return EXIT_LOCKED

        # 0) 오늘 레코드 존재 여부 (lock 획득 후에 확인해야 경합이 없다)
        existing = todays_records(settings.primary_lang)
        skip_generate = bool(existing) and not force

        # 1) refresh — 시세는 재생성 여부와 무관하게 항상 갱신한다 (채점·사이트 표시용)
        try:
            bundle = await refresh(offline=offline)
        except Exception as e:
            logger.exception("[Pipeline] refresh 실패: %s", e)
            return EXIT_FAILED

        # 2) score — 실패해도 생성은 계속한다
        try:
            scorecard.update_scorecard(store.list_records(), bundle, today_date())
        except Exception as e:
            logger.exception("[Pipeline] score 실패(계속 진행): %s", e)

        # 3) generate — 여기서 실패하면 중단
        if skip_generate:
            logger.info("[Pipeline] %s: %s", ALREADY_GENERATED_MSG, existing[0].id)
            records = existing
        else:
            try:
                records = await generate(mock=mock, offline=offline, market=bundle, force=force)
            except Exception as e:
                logger.exception("[Pipeline] generate 실패: %s", e)
                return EXIT_FAILED
        if not records:
            logger.error("[Pipeline] 생성된 레코드가 없습니다.")
            return EXIT_FAILED

        primary = records[0]
        failures: list[str] = []

        # 4) build — 실패해도 레코드는 이미 저장되어 있다
        if not build_site():
            failures.append("build")

        # 5) publish
        if no_publish or not settings.publish_enabled:
            logger.info("[Pipeline] publish 건너뜀 (no_publish=%s, enabled=%s)",
                        no_publish, settings.publish_enabled)
        elif "build" in failures:
            # publish는 '시도하지 않은' 것이므로 실패로 집계하지 않는다 (원인은 build 하나뿐).
            logger.warning("[Pipeline] 빌드 실패로 publish 건너뜀")
        else:
            from mbrief import publish as publish_mod
            try:
                if not publish_mod.publish_site():
                    failures.append("publish")
                else:
                    try:
                        from mbrief import indexing
                        indexing.push_indexnow(settings.site_url)
                    except Exception as idx_err:
                        logger.warning("[IndexNow] 알림 실패: %s", idx_err)
            except Exception as e:
                logger.exception("[Pipeline] publish 실패: %s", e)
                failures.append("publish")

        # 6) notify
        try:
            from mbrief import notify as notify_mod
            await notify_mod.send_telegram(primary, settings.site_url, notices=peek_notices())
        except Exception as e:
            logger.exception("[Pipeline] notify 실패: %s", e)
            failures.append("notify")

        if failures:
            logger.warning("[Pipeline] 생성은 성공했으나 후속 단계 실패: %s", ", ".join(failures))
            return EXIT_FAILED
        logger.info("[Pipeline] daily 완료: %s", primary.id)
        return EXIT_OK


def run_daily(
    *,
    no_publish: bool = False,
    mock: bool = False,
    offline: bool = False,
    force: bool = False,
) -> int:
    """동기 진입점 (CLI용)."""
    return asyncio.run(daily(no_publish=no_publish, mock=mock, offline=offline, force=force))


# ===== tick (장중 시세 갱신) =====
# 이 사이트는 정적 파일이라 차트가 실시간이 아니라 '파이프라인이 돌 때의 스냅샷'이다.
# 미국 지수는 07시 브리핑 시점에 이미 장이 끝나 완결된 데이터지만, 한국 낮에 열려 있는
# 코스피·원달러·비트코인은 그 스냅샷이 몇 시간씩 낡는다. tick이 그 간극을 메운다.
#
# 브리핑 본문은 건드리지 않는다 (LLM 호출 없음). 시세·차트만 다시 받고,
# 값이 실제로 바뀐 경우에만 빌드·배포해 불필요한 배포를 만들지 않는다.
def market_fingerprint(bundle: MarketBundle, charts: dict[str, list[dict]]) -> str:
    """시세 스냅샷의 내용 지문. updatedAt처럼 매번 바뀌는 값은 제외한다."""
    parts: list[str] = []
    for name in sorted(bundle.quotes):
        q = bundle.quotes[name]
        parts.append(f"q|{name}|{q.price}|{q.change}|{q.changePercent}")
    for name in sorted(bundle.sectors):
        s = bundle.sectors[name]
        parts.append(f"s|{name}|{s.price}|{s.changePercent}")
    for key in sorted(charts):
        candles = charts[key] or []
        last = candles[-1] if candles else {}
        parts.append(f"c|{key}|{len(candles)}|{last.get('time')}|{last.get('close')}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _current_fingerprint() -> str | None:
    """직전 스냅샷의 지문. 캐시가 없으면 None."""
    bundle = store.load_market()
    if bundle is None:
        return None
    return market_fingerprint(bundle, store.load_charts())


def check_volatility_trigger(bundle: MarketBundle | None) -> tuple[bool, str]:
    """변동성 트리거 검사: VIX |changePercent| >= 10.0% 또는 S&P 500 |changePercent| >= 2.0%."""
    if not bundle or not bundle.quotes:
        return False, ""
    vix = bundle.quotes.get("VIX")
    if vix and vix.changePercent is not None and abs(vix.changePercent) >= 10.0:
        return True, f"VIX 변동성 급변({vix.changePercent:+.1f}%)"
    spx = bundle.quotes.get("S&P 500")
    if spx and spx.changePercent is not None and abs(spx.changePercent) >= 2.0:
        return True, f"S&P 500 지수 급등락({spx.changePercent:+.1f}%)"
    return False, ""


async def tick(*, force: bool = False, no_publish: bool = False) -> int:
    """장중 시세·차트만 갱신하고, 값이 바뀐 경우 또는 변동성 트리거 발동 시 빌드·배포한다."""
    window = marketclock.parse_window(settings.intraday_window)
    if not force and not marketclock.in_intraday_window(window=window):
        logger.info("[Tick] 장중 시간대가 아님 (%s) - 건너뜀", settings.intraday_window)
        return EXIT_SKIPPED

    # daily와 같은 lock을 쓴다. 브리핑 생성 중에 시세를 갈아끼우지 않기 위함이다.
    with _DailyLock(paths.LOCK_FILE) as lock:
        if not lock.acquired:
            logger.info("[Tick] 다른 실행이 진행 중 - 건너뜀")
            return EXIT_SKIPPED

        before = _current_fingerprint()
        try:
            bundle = await refresh()
        except Exception as e:
            logger.exception("[Tick] 시세 갱신 실패: %s", e)
            return EXIT_FAILED

        is_volatile, trigger_reason = check_volatility_trigger(bundle)
        if is_volatile:
            logger.warning("[Micro-Batch] 변동성 트리거 감지 (%s) - 강제 빌드 및 배포 가동", trigger_reason)

        after = market_fingerprint(bundle, store.load_charts())
        if not is_volatile and before is not None and before == after:
            logger.info("[Tick] 시세 변동 없음 - 빌드·배포 건너뜀")
            return EXIT_OK

        if not build_site():
            return EXIT_FAILED
        if no_publish or not settings.publish_enabled:
            logger.info("[Tick] publish 건너뜀 (no_publish=%s)", no_publish)
            return EXIT_OK

        from mbrief import publish as publish_mod
        commit_msg = (
            f"microbatch: {now_iso()[:16]} ({trigger_reason})"
            if is_volatile
            else f"tick: {now_iso()[:16]} 시세 갱신"
        )
        if not publish_mod.publish_site(message=commit_msg):
            return EXIT_FAILED
        logger.info("[Tick] 완료 - 시세 갱신 배포됨")

        try:
            from mbrief import indexing
            indexing.push_indexnow(settings.site_url)
        except Exception as idx_err:
            logger.warning("[IndexNow] 알림 실패: %s", idx_err)

        return EXIT_OK


def run_tick(*, force: bool = False, no_publish: bool = False) -> int:
    """동기 진입점 (CLI용)."""
    return asyncio.run(tick(force=force, no_publish=no_publish))


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Race Condition (TOCTOU): daily lock을 os.open(O_CREAT|O_EXCL, 0o600)으로 원자적 생성.
#     stale 판정은 기록된 PID 생존 확인 우선, 판별 불가 시 2시간 경과 기준으로 대체.
#     '오늘 레코드 존재' 확인도 lock 획득 이후에만 수행해 경합을 없앤다
#   - Improper Input for Security Decision: lock 파일의 PID는 숫자 정규식 검증 후에만 사용
#   - Path Traversal: 픽스처 이름에 경로 구분자·상위 경로 표기 금지, 레코드/차트 경로는 store가 재검증
#   - Improper Input for Security Decision: 언어 코드를 지원 목록 화이트리스트로 검증 후 사용
#   - Unsafe Deserialization: 픽스처·LLM 출력 모두 pydantic model_validate로 검증한 뒤 사용
#   - Improper Resource Release: lock 파일·픽스처 파일 핸들을 context manager로 해제
#   - Missing Error Handling: refresh/score/generate/build/publish/notify 단계별 예외를 개별 처리,
#     build 이후 실패는 이미 저장된 레코드를 잃지 않는다
#   - Improper Exception Handling: 빈 except 없음, 모든 실패 경로에서 로깅
#   - Improper Authorization: PART 8 렌더 여부는 settings.publish_portfolio_note로만 결정 (기본 비공개)
#   - Infinite Loop: lock 획득 재시도 2회 고정, 모든 반복이 유한 목록 기반
# Not Applied:
#   - [WARN] 광범위 except: 외부 모듈(사이트 빌더·배포·알림)의 예외 계층을 알 수 없어 Exception을 잡는다.
#     모두 logger.exception으로 스택을 남기며 조용히 삼키지 않는다.
#   - [WARN] Missing Rate Limit: LLM 호출 빈도 제한은 lock 파일 단일 실행 보장에만 의존한다.
# --------------------------------------------------
