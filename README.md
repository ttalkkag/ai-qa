# AI Debug

비개발 매니저가 문제를 설명하고, 로컬 Codex로 코드 수정·검증을 진행하며, 관리자가 계정 권한으로 발행을 승인하는 도구를 만듭니다. 목표는 한 대의 Mac에서 실행하는 Django 풀스택 제품입니다.

현재는 `apps/`, `config/`, `integrations/codex/`에 Django 로컬 앱을 구현했으며, 기존 `devauto` 프로토타입은 별도로 유지합니다. 계정·프로젝트·요청 화면과 worker·검증·결과·승인·백업 경로가 있습니다.

**아직 실제 AI 수정에 사용할 수 있는 완성 상태는 아닙니다.** 현재 Mac의 Codex 네이티브 검사에서 자식 프로세스의 세션·그룹 이탈이 허용되어 실행을 차단합니다. 실효 설정 전체 감사도 미구현이며 실제 모델 호출·GitHub 발행은 검증하지 않았습니다. [구현 검증과 남은 작업](docs/operations/verification.md)을 먼저 확인하세요.

`AI Debug`는 제품 표시명, `ai-qa`는 저장소·Python 배포명·내부 npm manifest 이름, `devauto`는 현재 로컬 프로토타입의 Python package·CLI·API 이름입니다.

## 문서

- [로컬 제품 기획·도메인 안내](docs/README.md)
- [공통 용어](docs/CONTEXT.md)
- [목표 코드 구조·설치·운영](docs/operations/README.md)
- [구현 순서·검증 기준](docs/operations/implementation.md)
- [로컬 테스트·Docker Playwright·GitHub Actions](docs/operations/testing.md)

## 기준

- 현재 `docs/{domain}`의 목적·결정·수용 기준만 새 구현의 기준으로 사용합니다.
- 기존 Codex 로그인을 사용하며 AI API 키·Claude·클라우드 실행은 이번 범위에서 제외합니다.
- 매니저는 자신의 요청·결과에 접근하고, 원격 push·PR은 관리자 계정의 별도 승인이 필요합니다.
- 이전 문서는 `.docs/backup/`에 원문 보관했습니다. `.docs/0730-review/` 등과 함께 과거 참고이며 Git 제외 경로입니다.

## 현재 프로토타입 확인

```sh
make init
make check
.venv/bin/devauto --help
```
