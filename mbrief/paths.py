"""프로젝트 경로 상수. cwd와 무관하게 패키지 위치 기준으로 고정한다."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # 저장소 루트
DATA_DIR = ROOT / "data"
BRIEFINGS_DIR = DATA_DIR / "briefings"          # 생성된 브리핑 레코드 (JSON, 영구 보관)
CACHE_DIR = DATA_DIR / "cache"                   # 시세·차트·캘린더 스냅샷 (재생성 가능)
CHARTS_DIR = CACHE_DIR / "charts"
MARKET_JSON = CACHE_DIR / "market.json"
VALUATION_DIR = DATA_DIR / "valuation"      # 섹터 PER/PBR 일별 기록 (append-only, 재생성 불가)
VALUATION_FILE = VALUATION_DIR / "sector_valuation.jsonl"
SCORECARD_JSON = DATA_DIR / "scorecard.json"
PORTFOLIO_JSON = DATA_DIR / "portfolio.json"     # 소유자 포트폴리오 (선택, 비공개)
LOCK_FILE = DATA_DIR / ".daily.lock"
WEB_DIR = ROOT / "web"                           # 정적 프론트엔드 원본 (css/js/icons/manifest/sw)
TEMPLATES_DIR = ROOT / "mbrief" / "site" / "templates"
SITE_DIR = ROOT / "site"                         # 빌드 결과물 (gitignore, gh-pages로 배포)
LOG_DIR = ROOT / "logs"

# --------------------------------------------------
# Security Checklist
# Applied:
#   - Path Traversal: 모든 경로를 이 파일 위치(__file__) 기준으로 고정 조합한다. 외부 입력이
#     경로 요소로 들어오지 않으며, 파일명 조합은 store가 정규식 화이트리스트로 재검증한다
#   - Hard-coded Credentials: 자격증명·비밀값을 포함하지 않는다 (경로 상수 전용)
# Not Applied:
#   - [WARN] Incorrect Permission on Critical Resource: 디렉터리 권한은 OS 기본값을 따른다.
#     data/portfolio.json, data/briefings/ 는 비공개 정보를 담으므로 다중 사용자 PC에서는
#     사용자 전용 ACL로 좁힐 것.
# --------------------------------------------------
