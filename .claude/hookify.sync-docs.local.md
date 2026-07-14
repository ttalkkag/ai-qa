---
name: sync-docs
enabled: false
event: file
action: warn
conditions:
    - field: file_path
      operator: regex_match
      pattern: \.(php|ts|tsx|js|jsx|py|svelte|cs|rs)$
---

📝 **Documentation sync required**

코드가 변경되었습니다. `docs/`의 관련 문서를 **추가하거나 갱신**해야 하는지 확인하세요:

- 새 기능 추가 시 → 문서 **생성** 필요
- 기존 기능 수정/삭제 시 → 문서 **갱신** 필요

[ ] `docs/research/` - 기존 근거나 가정이 달라진 경우
[ ] `docs/plans/` - 범위, 요구사항 또는 구현 계획이 달라진 경우
[ ] `docs/report.md` - 구현 결과와 검증 증거가 생긴 경우
[ ] `README.md` 또는 API 문서 - 사용자 사용법이 달라진 경우
