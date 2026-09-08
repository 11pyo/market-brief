# 구 GitHub Pages 주소 은퇴용 리다이렉트

2026-09-08에 사이트를 Cloudflare Pages로 옮기면서, 구 주소
`https://11pyo.github.io/market-brief/` 를 새 주소로 넘기기 위해 `gh-pages` 브랜치에
올린 내용이다.

## 왜 지우지 않고 리다이렉트인가

- GitHub API가 이 저장소의 Pages 비활성화를 거부한다 (`Deactivating GitHub pages ... is not allowed`).
- `gh-pages` 가 기본 브랜치라 브랜치 삭제도 거부된다 (`Cannot delete the default branch`).
- 이미 공유된 링크와 블로그 글이 있어 404로 만드는 것보다 새 주소로 넘기는 편이 낫다.

`robots.txt` 로 크롤링을 막고 `noindex, follow` 로 색인은 새 주소만 남긴다.

## 다시 올리는 방법

`publish --target github` 를 실행하면 이 브랜치가 전체 사이트로 덮어써진다.
그 경우 아래로 되돌린다.

```bash
python -c "from pathlib import Path; from mbrief import publish; publish.publish_github(Path('deploy/gh-pages-redirect'), branch='gh-pages', message='retire: Cloudflare Pages로 이전')"
```

새 주소를 바꿀 때는 이 폴더의 `index.html` 과 `404.html` 안의 URL 두 곳을 함께 고친다.
