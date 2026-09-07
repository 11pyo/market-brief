"""외부 데이터 소스 모듈.

market_data       : yfinance 시세 · 섹터 ETF · CNN 공포탐욕 · 차트
news_collector    : Finnhub + RSS 뉴스 수집
news_filter       : 티어·점수 기반 뉴스 선별 및 프롬프트 포맷
calendar_service  : FOMC 일정 · BLS 지표 결과 · Finnhub 어닝 캘린더
httpguard         : 응답/요청 호스트 화이트리스트 훅 (리다이렉트 경유 SSRF 차단)

모든 모듈은 고정 HTTPS URL만 호출하며 사용자 입력 URL을 받지 않는다.
리다이렉트 종착지까지 httpguard의 허용 호스트 목록으로 강제한다.
"""

# --------------------------------------------------
# Security Checklist
# Applied:
#   - 해당 없음 - 패키지 선언 전용 모듈, 실행 코드 없음
# Not Applied:
#   - [WARN] 해당 없음
# --------------------------------------------------
