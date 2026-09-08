"""LLM 계층.

prompts.py       : 시스템 프롬프트 · 구조화 JSON 스키마 · 마크다운 렌더러
claude_cli.py    : 로컬 Claude Code headless(-p) 프로바이더 (1순위, 구독 쿼터)
api_providers.py : Gemini/OpenAI/Anthropic API 폴백 (키가 있을 때만).
                   openai/gemini는 같은 JSON 스키마로 구조화 출력을 시도하고, 거부되면 텍스트로 폴백한다.
engine.py        : 프로바이더 체인 + 구조화 출력 검증
translate.py     : 추가 언어 번역 패스
"""

# --------------------------------------------------
# Security Checklist
# Applied:
#   - 해당 없음 - 패키지 선언 전용 모듈, 실행 코드 없음
# Not Applied:
#   - [WARN] 해당 없음
# --------------------------------------------------
