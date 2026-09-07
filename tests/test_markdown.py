"""mbrief.site.markdown 안전 렌더러 테스트.

핵심 관심사는 딱 두 가지다.
  1. LLM/뉴스 유래 텍스트가 절대 실행 가능한 마크업이 되지 않는다 (XSS).
  2. http(s)가 아닌 스킴은 절대 링크가 되지 않는다 (javascript:, data: 등).
"""
from __future__ import annotations

import re

import pytest

from mbrief.site.markdown import inline_markdown_safe, render_markdown_safe


# ===== 1. XSS 차단 =====


def test_script_tag_is_neutralised():
    html = render_markdown_safe("<script>alert(1)</script>")
    assert "<script" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_img_onerror_payload_is_escaped():
    html = render_markdown_safe('<img src=x onerror="alert(1)">')
    assert "<img" not in html
    assert "onerror" in html  # 텍스트로는 남지만 속성으로 해석되지 않는다
    assert "&lt;img" in html


def test_escaped_before_markdown_so_injected_tags_cannot_survive_bold():
    html = render_markdown_safe("**<b onmouseover=alert(1)>hi</b>**")
    assert "<strong>" in html
    assert "<b " not in html
    assert "&lt;b onmouseover=alert(1)&gt;" in html


def test_quotes_and_ampersand_escaped():
    html = render_markdown_safe('a "b" & c \'d\'')
    assert "&amp;" in html
    assert "&#x27;" in html or "&#39;" in html
    assert "&quot;" in html


def test_attribute_breakout_attempt_in_url_is_contained():
    html = render_markdown_safe('https://evil.test/"><script>alert(1)</script>')
    assert "<script" not in html
    # 링크가 만들어져도 따옴표는 이미 엔티티라 속성을 탈출할 수 없다
    assert '"><script' not in html


# ===== 2. 위험 스킴 링크화 금지 =====


@pytest.mark.parametrize(
    "payload",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "  javascript:alert(document.domain)",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
    ],
)
def test_dangerous_schemes_never_become_links(payload):
    html = render_markdown_safe(f"see this: {payload}")
    assert "<a " not in html
    assert "href=" not in html


def test_javascript_scheme_inside_a_line_with_a_real_url_only_links_the_real_url():
    html = render_markdown_safe("good https://example.com/x bad javascript:alert(1)")
    assert html.count("<a ") == 1
    assert 'href="https://example.com/x"' in html
    assert "javascript:alert(1)" in html  # 평문으로만 남는다
    assert 'href="javascript' not in html


def test_bare_https_url_is_autolinked_with_safe_rel():
    html = render_markdown_safe("출처: https://www.federalreserve.gov/newsevents.htm 참고")
    assert 'href="https://www.federalreserve.gov/newsevents.htm"' in html
    assert 'rel="noopener noreferrer nofollow ugc"' in html
    assert 'target="_blank"' in html


def test_trailing_punctuation_is_not_part_of_the_link():
    html = render_markdown_safe("링크는 https://example.com/page. 였다")
    assert 'href="https://example.com/page"' in html
    assert "page.</a>" not in html


def test_url_query_ampersand_stays_escaped_in_href():
    html = render_markdown_safe("https://example.com/q?a=1&b=2")
    assert 'href="https://example.com/q?a=1&amp;b=2"' in html


def test_url_inside_code_span_is_not_linked():
    html = render_markdown_safe("`https://example.com/x`")
    assert "<code>https://example.com/x</code>" in html
    assert "<a " not in html


# ===== 3. 마크다운 서브셋 =====


def test_headings():
    html = render_markdown_safe("# H1\n## H2\n### H3")
    assert "<h1>H1</h1>" in html
    assert "<h2>H2</h2>" in html
    assert "<h3>H3</h3>" in html


def test_bold_italic_code():
    html = render_markdown_safe("**굵게** *기울임* `코드`")
    assert "<strong>굵게</strong>" in html
    assert "<em>기울임</em>" in html
    assert "<code>코드</code>" in html


def test_bold_beats_italic_for_double_asterisk():
    html = render_markdown_safe("**둘 다** 아님")
    assert "<strong>둘 다</strong>" in html
    assert "<em>" not in html


@pytest.mark.parametrize("bullet", ["- ", "• ", "* "])
def test_unordered_lists(bullet):
    html = render_markdown_safe(f"{bullet}첫째\n{bullet}둘째")
    assert html.count("<li>") == 2
    assert "<ul>" in html and "</ul>" in html
    assert "<li>첫째</li>" in html


def test_ordered_list():
    html = render_markdown_safe("1. 하나\n2. 둘\n3. 셋")
    assert "<ol>" in html
    assert html.count("<li>") == 3


def test_blockquote_and_hr():
    html = render_markdown_safe("> 인용문\n\n---")
    assert "<blockquote>인용문</blockquote>" in html
    assert "<hr>" in html


def test_paragraphs_and_line_breaks():
    html = render_markdown_safe("첫 줄\n둘째 줄\n\n새 문단")
    assert "<p>첫 줄<br>둘째 줄</p>" in html
    assert "<p>새 문단</p>" in html


def test_mixed_document_structure_order_is_preserved():
    html = render_markdown_safe("## 제목\n본문\n\n- 항목\n\n> 인용")
    order = [html.index(tag) for tag in ("<h2>", "<p>", "<ul>", "<blockquote>")]
    assert order == sorted(order)


# ===== 4. 강건성 =====


@pytest.mark.parametrize("value", [None, "", "   ", "\n\n"])
def test_empty_inputs_return_empty_string(value):
    assert render_markdown_safe(value) == ""


def test_control_characters_are_stripped_so_placeholders_cannot_be_forged():
    html = render_markdown_safe("\x01" + "0" + "\x02" + " `code`")
    assert "\x01" not in html and "\x02" not in html
    assert "<code>code</code>" in html


def test_non_string_input_is_coerced():
    assert render_markdown_safe(12345) == "<p>12345</p>"


def test_very_long_input_terminates():
    html = render_markdown_safe("줄\n" * 30000)
    assert html  # 라인 상한이 있어도 정상 종료하고 결과를 낸다


def test_no_raw_angle_brackets_survive_from_input():
    payload = "<div><span>x</span></div>\n<svg onload=alert(1)>"
    html = render_markdown_safe(payload)
    # 결과에 남은 태그는 전부 렌더러가 만든 화이트리스트 태그여야 한다
    tags = set(re.findall(r"</?([a-zA-Z0-9]+)", html))
    assert tags <= {"p", "br", "h1", "h2", "h3", "ul", "ol", "li", "hr", "blockquote", "code", "strong", "em", "a"}


# ===== 5. inline_markdown_safe =====


def test_inline_supports_only_emphasis_and_code():
    assert inline_markdown_safe("**a** *b* `c`") == "<strong>a</strong> <em>b</em> <code>c</code>"


def test_inline_never_creates_links():
    html = inline_markdown_safe("https://example.com javascript:alert(1)")
    assert "<a " not in html


def test_inline_escapes_html_and_flattens_newlines():
    html = inline_markdown_safe("<script>x</script>\nsecond")
    assert "<script" not in html
    assert "\n" not in html
    assert "&lt;script&gt;" in html


def test_inline_handles_none():
    assert inline_markdown_safe(None) == ""


# --------------------------------------------------
# Security Checklist
# Applied:
#   - XSS: script/img/속성 탈출 페이로드가 마크업으로 살아남지 않음을 회귀 테스트로 고정
#   - Open Redirect / 위험 스킴: javascript:/data:/vbscript:/file: 링크화 금지를 파라미터 테스트로 고정
#   - Infinite Loop: 3만 줄 입력이 정상 종료함을 확인
# Not Applied:
#   - [WARN] 렌더 성능(대용량 입력 처리 시간) 벤치마크는 포함하지 않는다.
# --------------------------------------------------
