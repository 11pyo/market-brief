"""환경 설정 (pydantic-settings). 저장소 루트의 .env를 cwd와 무관하게 읽는다.

원칙:
  - LLM 1순위는 로컬 Claude Code CLI(headless `claude -p`). 구독 요금제 쿼터를 사용하며 API 키가 필요 없다.
  - Gemini/OpenAI/Anthropic API는 키가 설정된 경우에만 폴백으로 참여한다 (LLM_FALLBACK_PROVIDERS).
  - 외부 DB(Upstash Redis 등)는 사용하지 않는다. 모든 상태는 data/ 아래 JSON 파일이다.
"""
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mbrief.paths import ROOT


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ===== 데이터 소스 =====
    finnhub_api_key: str = ""                 # 뉴스·어닝 캘린더 (https://finnhub.io 무료 키)

    # ===== LLM: Claude Code CLI (1순위) =====
    claude_cli_enabled: bool = True
    claude_cli_path: str = ""                 # 비우면 PATH에서 claude 자동 탐색
    # 정책: Claude 호출은 sonnet 이상만 쓴다. haiku는 허용 목록에서 제외되어 sonnet으로 강등된다
    # (mbrief/llm/claude_cli.py의 _ALLOWED_MODEL_ALIASES).
    claude_cli_model: str = "sonnet"          # sonnet | opus | fable | claude-... 전체 모델 ID
    claude_cli_fallback_model: str = "opus"   # 과부하 시 CLI 자체 폴백(--fallback-model). 비우면 미사용
    claude_cli_timeout_sec: int = 720         # 1회 호출 상한 (구조화 출력 한국어 브리핑 실측 4~7분)
    claude_cli_retries: int = 1               # 실패 시 재시도 횟수 (인증 실패는 재시도하지 않음)
    claude_cli_translate_model: str = "sonnet"  # 추가 언어 번역 패스용 모델 (최소 sonnet)

    # ===== LLM: API 폴백 (선택) =====
    # 로컬 CLI 실패 시 시도 순서. 키가 없는 프로바이더는 자동 제외, 빈 문자열이면 폴백 없음.
    llm_fallback_providers: str = "openai,gemini"
    llm_timeout_sec: int = 300
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"

    # ===== 브리핑 =====
    briefing_langs: str = "ko"                # 쉼표 구분. 첫 언어로 분석 생성, 나머지는 번역 패스 (예: "ko,en")
    # 장중 시세 갱신(tick) 창. 한국 정규장 기준. 이 시간대 밖에서는 tick이 아무것도 하지 않는다.
    intraday_window: str = "09:00-15:30"
    briefing_days: str = "mon,tue,wed,thu,fri,sat"  # daily 명령이 실행되는 요일 (일요일 기본 제외)
    max_briefings_on_site: int = 60           # 정적 사이트에 포함할 최근 브리핑 수 (data/briefings/는 전부 보관)
    publish_portfolio_note: bool = False      # PART 8(소유자 포트폴리오 조언)을 공개 사이트에 포함할지
    tz: str = "Asia/Seoul"

    # ===== 정적 사이트 =====
    site_url: str = ""                        # 공개 URL (예: https://11pyo.github.io/market-brief-ai). canonical/sitemap/feed에 사용
    site_name: str = "증시흐름"
    site_title: str = "AI 모닝 마켓 브리핑 | 증시흐름"
    site_description: str = "매일 아침 07시, 로컬 Claude가 글로벌 뉴스와 시세를 분석해 만드는 헤지펀드 모닝노트 스타일 브리핑."
    github_repo_url: str = "https://github.com/11pyo/market-brief-ai"

    # ===== 배포 (gh-pages) =====
    # github     = gh-pages 브랜치로 푸시 (무료 플랜은 공개 저장소 필요)
    # cloudflare = Cloudflare Pages Direct Upload (저장소 불필요, 소스 비공개 유지 가능)
    publish_target: str = "github"
    cloudflare_project: str = ""      # Pages 프로젝트명 (소문자·숫자·하이픈)
    cloudflare_api_token: str = ""    # 무인 실행용. 비우면 `wrangler login` 세션을 사용한다
    cloudflare_account_id: str = ""
    publish_enabled: bool = True
    publish_remote: str = "origin"            # 리모트 이름 또는 저장소 URL. 비공개 저장소는 GitHub Pages 불가 → 공개 저장소 URL 지정
    publish_branch: str = "gh-pages"

    # ===== 알림 (선택) =====
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @field_validator("briefing_langs", "llm_fallback_providers", "briefing_days", mode="before")
    @classmethod
    def _strip_csv(cls, v: object) -> object:
        if v is None:
            return ""
        return ",".join(p.strip().lower() for p in str(v).split(",") if p.strip())

    # --- 파생 접근자 ---
    @property
    def langs(self) -> list[str]:
        return [p for p in self.briefing_langs.split(",") if p] or ["ko"]

    @property
    def primary_lang(self) -> str:
        return self.langs[0]

    @property
    def fallback_providers(self) -> list[str]:
        return [p for p in self.llm_fallback_providers.split(",") if p]

    @property
    def active_days(self) -> set[str]:
        return {d for d in self.briefing_days.split(",") if d}


settings = Settings()

# --------------------------------------------------
# Security Checklist
# Applied:
#   - Hard-coded Credentials: 모든 키·토큰은 .env/환경변수 경유, 소스 상수 없음
#   - Improper Input for Security Decision: CSV 설정값은 소문자·공백 제거로 정규화 후 화이트리스트 비교에 사용
# Not Applied:
#   - [WARN] .env 파일은 평문이다. OS 사용자 권한으로만 보호되므로 공용 PC에서는 사용하지 말 것.
# --------------------------------------------------
