"""파일 기반 상태 저장소 — data/ 아래 JSON이 유일한 영구 저장소다.

외부 DB(Upstash Redis 등)는 사용하지 않는다. 모든 쓰기는 같은 디렉터리의 임시 파일에
기록한 뒤 os.replace로 교체하는 원자적 쓰기다 (TOCTOU·부분 기록 방지).

경로는 `mbrief.paths` 모듈 속성을 호출 시점에 조회한다. 테스트(conftest)가
paths 상수를 tmp 디렉터리로 바꿔치기하면 실제 data/를 건드리지 않는다.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
from pathlib import Path

from pydantic import BaseModel, ValidationError

from mbrief import paths
from mbrief.schemas import BriefingRecord, MarketBundle, Portfolio, Scorecard
from mbrief.timeutil import now_iso, parse_iso

logger = logging.getLogger(__name__)

# [SECURE] 레코드 ID 화이트리스트 정규식 - Path Traversal 방지 (Category 1)
RECORD_ID_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z]{2}-[0-9a-f]{6}$")
# [SECURE] 차트 키 화이트리스트 정규식 - Path Traversal 방지 (Category 1)
CHART_KEY_RE = re.compile(r"^[a-z0-9]+_(1d|5d|1mo)$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_LANG_RE = re.compile(r"^[a-z]{2}$")

# [SECURE] 단일 레코드 파일 크기 상한 - 파서 메모리 고갈 방지 (Category 1)
_MAX_RECORD_BYTES = 8 * 1024 * 1024
LEGACY_SUFFIX = ".v1.bak"


class StoreError(RuntimeError):
    """저장소 입출력·검증 실패."""


# ===== 내부 헬퍼 =====
def _ensure_dir(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _atomic_write_text(path: Path, text: str) -> Path:
    """같은 디렉터리 임시 파일에 기록 후 os.replace로 교체.

    [SECURE] Race Condition (TOCTOU): 예측 불가 임시 파일명 + 원자적 교체 (Category 3)
    [SECURE] Improper Resource Release: context manager로 FD 확실히 해제 (Category 5)
    """
    _ensure_dir(path.parent)
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
        return path
    except OSError as e:
        logger.error("[Store] 저장 실패 %s: %s", path.name, e)
        raise StoreError(f"파일 저장에 실패했습니다: {path.name}") from e
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)  # [SECURE] 실패 시 임시 파일 잔존 방지 (Category 5)
            except OSError as e:
                logger.warning("[Store] 임시 파일 삭제 실패: %s", e)


def _write_model(path: Path, model: BaseModel) -> Path:
    return _atomic_write_text(path, model.model_dump_json(indent=2))


def _read_json(path: Path) -> object | None:
    """JSON 파일 읽기. 없으면 None, 손상되면 경고 후 None."""
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > _MAX_RECORD_BYTES:
            logger.warning("[Store] 파일 크기 상한 초과 - 건너뜀: %s", path.name)
            return None
        with path.open("r", encoding="utf-8") as f:  # [SECURE] context manager로 파일 핸들 해제 (Category 5)
            return json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        # [SECURE] 상세 오류는 로컬 로그에만 (Category 4)
        logger.warning("[Store] 읽기 실패 %s: %s", path.name, e)
        return None


def _record_path(record_id: str) -> Path:
    """[SECURE] ID 정규식 검증 후에만 경로를 조합 - Path Traversal 방지 (Category 1)."""
    if not RECORD_ID_RE.match(record_id or ""):
        raise ValueError("허용되지 않은 브리핑 ID 형식입니다.")
    return paths.BRIEFINGS_DIR / f"{record_id}.json"


def _chart_path(key: str) -> Path:
    """[SECURE] 차트 키 정규식 검증 후에만 경로를 조합 - Path Traversal 방지 (Category 1)."""
    if not CHART_KEY_RE.match(key or ""):
        raise ValueError("허용되지 않은 차트 키 형식입니다.")
    return paths.CHARTS_DIR / f"{key}.json"


# ===== 브리핑 레코드 =====
def new_record_id(date_str: str, lang: str) -> str:
    """`YYYY-MM-DD-<lang>-<6hex>` 형식의 새 레코드 ID.

    [SECURE] Improper Random Value: secrets(CSPRNG)로 예측 불가한 접미사 생성 (Category 2)
    """
    if not _DATE_RE.match(date_str or ""):
        raise ValueError("날짜 형식이 올바르지 않습니다 (YYYY-MM-DD).")
    code = (lang or "").strip().lower()
    if not _LANG_RE.match(code):
        raise ValueError("언어 코드 형식이 올바르지 않습니다 (2자 소문자).")
    return f"{date_str}-{code}-{secrets.token_hex(3)}"


def save_record(record: BriefingRecord) -> Path:
    """브리핑 레코드 저장 → data/briefings/<id>.json 경로 반환."""
    path = _record_path(record.id)
    _write_model(path, record)
    logger.info("[Store] 브리핑 저장: %s", path.name)
    return path


def load_record(record_id: str) -> BriefingRecord | None:
    """ID로 레코드 로드. 형식 위반·미존재·검증 실패 시 None."""
    try:
        path = _record_path(record_id)
    except ValueError as e:
        logger.warning("[Store] 잘못된 브리핑 ID 요청: %s", e)
        return None

    data = _read_json(path)
    if data is None:
        return None
    try:
        return BriefingRecord.model_validate(data)
    except ValidationError as e:
        logger.warning("[Store] 레코드 검증 실패 %s: %d건", path.name, e.error_count())
        return None


def list_records(limit: int | None = None, lang: str | None = None) -> list[BriefingRecord]:
    """generatedAt(파싱된 시각) 기준 최신순 레코드 목록. 검증 실패 파일은 경고 후 건너뛴다."""
    directory = paths.BRIEFINGS_DIR
    if not directory.is_dir():
        return []

    records: list[BriefingRecord] = []
    for path in sorted(directory.glob("*.json")):
        data = _read_json(path)
        if data is None:
            continue
        try:
            record = BriefingRecord.model_validate(data)
        except ValidationError as e:
            # [SECURE] 손상·구버전 파일이 전체 조회를 깨뜨리지 않도록 격리 (Category 4)
            logger.warning("[Store] 레코드 건너뜀 %s: 검증 실패 %d건", path.name, e.error_count())
            continue
        if lang and record.lang != lang:
            continue
        records.append(record)

    # generatedAt은 오프셋이 섞일 수 있어 문자열 비교로는 순서가 뒤집힌다. 반드시 파싱 후 비교한다.
    records.sort(key=lambda r: (parse_iso(r.generatedAt), r.id), reverse=True)
    if limit is not None and limit >= 0:
        records = records[:limit]
    return records


def latest_record(lang: str = "ko") -> BriefingRecord | None:
    """해당 언어의 최신 레코드."""
    found = list_records(limit=1, lang=lang)
    return found[0] if found else None


# ===== 시세 스냅샷 =====
def save_market(bundle: MarketBundle) -> Path:
    path = paths.MARKET_JSON
    _write_model(path, bundle)
    logger.info("[Store] 시세 스냅샷 저장: %s", path.name)
    return path


def load_market() -> MarketBundle | None:
    data = _read_json(paths.MARKET_JSON)
    if data is None:
        return None
    try:
        return MarketBundle.model_validate(data)
    except ValidationError as e:
        logger.warning("[Store] 시세 스냅샷 검증 실패: %d건", e.error_count())
        return None


# ===== 차트 =====
def save_chart(key: str, candles: list[dict]) -> Path:
    """캔들 목록 저장 → data/cache/charts/<key>.json."""
    path = _chart_path(key)
    _atomic_write_text(path, json.dumps(candles, ensure_ascii=False, separators=(",", ":")))
    return path


def load_charts() -> dict[str, list[dict]]:
    """저장된 모든 차트 로드. 키 형식이 맞지 않는 파일은 건너뛴다."""
    directory = paths.CHARTS_DIR
    if not directory.is_dir():
        return {}

    charts: dict[str, list[dict]] = {}
    for path in sorted(directory.glob("*.json")):
        key = path.stem
        if not CHART_KEY_RE.match(key):
            logger.warning("[Store] 차트 키 형식 불일치 - 건너뜀: %s", path.name)
            continue
        data = _read_json(path)
        if isinstance(data, list):
            charts[key] = data
        elif data is not None:
            logger.warning("[Store] 차트 형식 오류 - 건너뜀: %s", path.name)
    return charts


# ===== 스코어카드 =====
def save_scorecard(sc: Scorecard) -> Path:
    path = paths.SCORECARD_JSON
    _write_model(path, sc)
    logger.info("[Store] 스코어카드 저장: %d건", len(sc.entries))
    return path


def load_scorecard() -> Scorecard | None:
    data = _read_json(paths.SCORECARD_JSON)
    if data is None:
        return None
    try:
        return Scorecard.model_validate(data)
    except ValidationError as e:
        logger.warning("[Store] 스코어카드 검증 실패: %d건", e.error_count())
        return None


# ===== 포트폴리오 (비공개, 선택) =====
def load_portfolio() -> Portfolio | None:
    """data/portfolio.json. 없으면 None (PART 8을 생성하지 않는다).

    예시 템플릿: data/portfolio.example.json (복사해서 값을 채운다).
    """
    data = _read_json(paths.PORTFOLIO_JSON)
    if data is None:
        return None
    try:
        return Portfolio.model_validate(data)
    except ValidationError as e:
        logger.warning("[Store] 포트폴리오 검증 실패: %d건", e.error_count())
        return None


def format_portfolio_for_llm(portfolio: Portfolio | None) -> str:
    """포트폴리오 → 프롬프트 텍스트. 없으면 빈 문자열."""
    if portfolio is None or not portfolio.allocations:
        return ""
    lines = [
        f"Investment Style: {portfolio.investmentStyle}",
        f"Risk Tolerance: {portfolio.riskTolerance}",
        f"Total Assets: {portfolio.totalAssets or 'Not specified'}",
        "",
        "Asset Allocation:",
    ]
    for alloc in portfolio.allocations:
        line = f"- {alloc.name}: {alloc.percentage}%"
        if alloc.details:
            line += f" ({alloc.details})"
        lines.append(line)
    if portfolio.watchlist:
        lines.append("")
        lines.append(f"Watchlist: {', '.join(portfolio.watchlist)}")
    return "\n".join(lines)


# ===== v1 레코드 마이그레이션 =====
_LEGACY_PROVIDER_PREFIXES = (
    ("gemini", "gemini"),
    ("gpt-", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("chatgpt", "openai"),
    ("claude", "claude_cli"),
)


def _legacy_provider(model: str) -> str:
    """v1 model 문자열 → v2 provider 이름."""
    low = (model or "").strip().lower()
    for prefix, provider in _LEGACY_PROVIDER_PREFIXES:
        if low.startswith(prefix):
            return provider
    return "legacy"


def _looks_legacy(data: object) -> bool:
    """v1 레코드 판별: content가 있고 contentMarkdown이 없다."""
    return isinstance(data, dict) and "content" in data and "contentMarkdown" not in data


def convert_legacy(data: dict) -> BriefingRecord:
    """v1 레코드 dict → v2 BriefingRecord (structured=None)."""
    date_str = str(data.get("date") or "")[:10]
    if not _DATE_RE.match(date_str):
        raise ValueError("v1 레코드의 date 형식이 올바르지 않습니다.")

    content = str(data.get("content") or "")
    model = str(data.get("model") or "")
    headline = str(data.get("preview") or "").strip()
    if not headline:
        headline = " ".join(content.split())[:120]

    return BriefingRecord(
        id=new_record_id(date_str, "ko"),
        date=date_str,
        lang="ko",
        generatedAt=str(data.get("generatedAt") or now_iso()),
        provider=_legacy_provider(model),
        model=model,
        generationTimeMs=int(data.get("generationTimeMs") or 0),
        newsCount=int(data.get("newsCount") or 0),
        headline=headline[:160],
        structured=None,
        contentMarkdown=content,
    )


def legacy_import() -> list[BriefingRecord]:
    """data/briefings/*.json의 v1 레코드를 v2로 변환하고 원본을 *.v1.bak으로 이름 변경.

    이미 v2인 파일은 건드리지 않는다. 변환된 레코드 목록을 반환한다.
    """
    directory = paths.BRIEFINGS_DIR
    if not directory.is_dir():
        logger.info("[Store] 브리핑 디렉터리 없음 - 마이그레이션 대상 없음")
        return []

    converted: list[BriefingRecord] = []
    for path in sorted(directory.glob("*.json")):
        data = _read_json(path)
        if not _looks_legacy(data):
            continue
        try:
            record = convert_legacy(data)  # type: ignore[arg-type]
        except (ValueError, ValidationError, TypeError) as e:
            # [SECURE] 변환 실패를 격리하고 원본은 보존 (Category 4)
            logger.warning("[Store] v1 변환 실패 %s: %s", path.name, e)
            continue

        save_record(record)
        backup = path.with_name(path.name + LEGACY_SUFFIX)
        try:
            os.replace(path, backup)  # [SECURE] 원자적 이름 변경 - 중복 마이그레이션 방지 (Category 3)
        except OSError as e:
            logger.warning("[Store] 원본 이름 변경 실패 %s: %s", path.name, e)
        converted.append(record)
        logger.info("[Store] v1 → v2 변환: %s → %s.json", path.name, record.id)

    logger.info("[Store] 마이그레이션 완료: %d건", len(converted))
    return converted


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Path Traversal: 레코드 ID·차트 키를 정규식 화이트리스트로 검증한 뒤에만 경로를 조합
#   - Race Condition (TOCTOU): mkstemp 임시 파일 + os.replace 원자적 교체, 마이그레이션도 os.replace 사용
#   - Improper Random Value: 레코드 ID 접미사를 secrets.token_hex(CSPRNG)로 생성
#   - Improper Resource Release: 모든 파일 입출력을 context manager로 처리, 실패 시 임시 파일 정리
#   - Unsafe Deserialization: 읽어온 JSON을 pydantic model_validate로 검증한 뒤에만 사용
#   - Missing Error Handling: 손상·구버전 파일은 경고 후 건너뛰고 전체 조회를 중단하지 않음
#   - Improper Exception Handling: 빈 except 없음, 모든 실패 경로에서 로깅
#   - Error Message Information Exposure: 상세 예외는 로컬 로그에만, 호출자에는 요약 메시지
#   - 자원 고갈 방지: 단일 파일 8MB 상한 검사
# Not Applied:
#   - [WARN] Incorrect Permission on Critical Resource: data/portfolio.json은 OS 기본 권한으로 저장된다.
#     비공개 정보이므로 다중 사용자 PC에서는 파일 ACL을 사용자 전용으로 좁힐 것.
#   - [WARN] Unencrypted Sensitive Data: 포트폴리오는 평문 JSON이다. 공용 PC 사용 금지.
#   - [WARN] 동시 실행 방어는 pipeline.daily의 lock 파일에만 의존한다. store 자체는 파일 잠금을 걸지 않는다.
# --------------------------------------------------
