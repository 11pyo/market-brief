"""프로젝트 경로 상수. cwd와 무관하게 패키지 위치 기준으로 고정한다."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # 저장소 루트
DATA_DIR = ROOT / "data"
BRIEFINGS_DIR = DATA_DIR / "briefings"          # 생성된 브리핑 레코드 (JSON, 영구 보관)
CACHE_DIR = DATA_DIR / "cache"                   # 시세·차트·캘린더 스냅샷 (재생성 가능)
CHARTS_DIR = CACHE_DIR / "charts"
MARKET_JSON = CACHE_DIR / "market.json"
SCORECARD_JSON = DATA_DIR / "scorecard.json"
PORTFOLIO_JSON = DATA_DIR / "portfolio.json"     # 소유자 포트폴리오 (선택, 비공개)
LOCK_FILE = DATA_DIR / ".daily.lock"
WEB_DIR = ROOT / "web"                           # 정적 프론트엔드 원본 (css/js/icons/manifest/sw)
TEMPLATES_DIR = ROOT / "mbrief" / "site" / "templates"
SITE_DIR = ROOT / "site"                         # 빌드 결과물 (gitignore, gh-pages로 배포)
LOG_DIR = ROOT / "logs"
