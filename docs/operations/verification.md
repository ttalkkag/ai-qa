# 현재 구현과 검증 기록

## 2026-09-14 등록된 base의 실제 사용자·관리자 검증

### 수행한 흐름

로컬 웹 `http://127.0.0.1:8000`에서 guest와 admin을 서로 다른 브라우저 세션으로 로그인했다. 등록된 **베이스** 프로젝트에 guest를 할당하고 README 확인용 필수 Docker 검사를 등록했다. 원격 발행 설정은 활성화하지 않았다.

1. guest가 README 한국어 소개·빠른 시작 안내를 요청했다
2. `gpt-5.6-luna / low`로 명세를 분석하고, 관련 도메인·영향 범위·충돌 검토를 포함한 제안을 확인·보완했다
3. guest가 확정한 명세 v1을 `gpt-6-astra / xhigh`로 실행했다
4. guest가 첫 결과를 확인하고 “프로젝트별 안내” 섹션 추가를 재요청했다
5. 기존 요구사항과 보완 내용을 합친 새 초안을 AI로 다시 분석하고 명세 v2를 확인·실행했다
6. guest가 두 번째 결과의 변경과 검증을 확인한 뒤 작업 완료를 선택했다
7. admin이 승인 목록에서 해당 결과를 열고 **로컬 결과 승인**을 선택했다
8. guest 화면과 DB에서 **관리자 승인 완료** 및 서로 다른 확인·승인 계정을 확인했다

| 증거 | 식별자·결과 |
| --- | --- |
| 요청 | `d1e86053-111c-4724-a0a7-f3b1ec96e759` |
| 첫 성공 실행 | `0824d6a3-6bbd-4fe4-b131-fedf53a50728` · 119초 |
| 재수정 성공 실행 | `599534b0-12eb-451b-a253-e209d01447c1` · 101초 |
| 최종 승인 결과 | `9499d516-340f-461f-8ade-ca85dda3e539` |
| 결과 해시 | `c5702cc1c6c264439234893ff4739614dcf9c1f0bcbaa387db2c639867fe9b28` |
| 변경 범위 | 두 실행 모두 `README.md` 하나 |
| 독립 검사 | `readme-contract` · BASELINE 4개 중 3개 실패 → AFTER 4개 통과 |
| 확인·승인 | `accepted_by=guest`, `decided_by=admin`, `state=APPROVED` |
| 원본 base | clean · HEAD `5f6d2be5ae00260e0d9519b3240332f5803da643` 유지 |
| 원격 작업 | Publication 0개, 발행 Job 0개, push·PR·배포 미실행 |

각 수정은 서로 다른 독립 Git worktree와 Docker 컨테이너에서 실행했다. 완료 후 작업 컨테이너가 남지 않은 것을 확인했다. 원본 README는 변경하지 않았고, 결과는 비공개 패치와 작업 사본으로 보존했다. 이 검증은 원격 저장소 반영이나 배포 성공을 의미하지 않는다.

`readme-contract`는 제목 유지·make help·make check·템플릿 설명을 실제로 검사한다. 추가한 “프로젝트별 안내”와 `make check`의 정확한 설명은 최종 패치를 직접 읽어 확인했다. base의 `make check`는 echo 예시이므로 실제 테스트 통과로 계산하지 않았다.

### 발견·수정한 문제

- 정상적인 디렉토리 심볼릭 링크를 없는 대상으로 판단해 base 분석을 중단했다. 커밋된 디렉토리 별칭을 허용하고 순환·범위 이탈 검사를 유지했다
- 검증 결과용 `.aiqa-output` 상위 경로가 제외 경로와 겹치면 보고서 작성 전에 `CONFIG_UNSUPPORTED`가 발생했다. 선언된 결과·임시 경로와 필요한 상위 경로를 일관되게 처리했다
- 일반 수정 상담에도 실행 상태 답변 규칙이 적용돼 실행이 없다는 답변만 생성했다. 상태 조회 규칙과 요구사항 분석 규칙을 분리했다
- AI 명세 분석이 Makefile 주석만 믿거나 선택적 문구를 필수 질문으로 오인했다. 실제 명령 본문 확인과 필수·선택 질문 구분을 명시했다. 제안은 자동 실행하지 않고 사용자가 보완·확인한다
- 실제 모델 작업 보고가 결과 요약에서 누락됐다. 요구사항별 반영·변경·검증·미확인 보고를 보존하고 앱의 독립 검증 결과와 구분했다
- 사용자 결과 확인·재수정·관리자 로컬 승인이 없었다. 불변 결과 해시와 계정에 결속된 ResultReview를 추가하고 원격 발행과 분리했다. 새 초안·새 실행이 있으면 과거 결과를 새로 승인하지 못한다
- 명세 제안 완료가 화면에 반영되지 않고 실패 후 재시도 버튼도 갱신되지 않았다. 자동 상태 갱신을 추가하되 작성 중인 내용은 보존한다
- 결과/요청 상태, 승인 동선, 모바일 줄바꿈을 보완했다. AI 보고의 제목을 구분하고 파일 링크는 실행 가능한 링크가 아닌 파일명으로 표시한다. 원본 보고는 summary.md에 보존한다

실제 AI가 모든 누락과 영향을 항상 정확하게 찾아낸다는 보장은 아니다. 이번 시험에서도 첫 제안의 make check 설명과 두 번째 제안의 불필요한 실행 제외 조건을 사용자가 수정하는 절차로 보완했다. AI 분석 → 사용자 명세 확인 → 독립 검사 → 사용자 결과 확인 → 관리자 승인이라는 단계가 그 차이를 드러내도록 유지한다.

### 최종 자동·화면 검사

- `make check`: **951 passed, 2 skipped**, 106.66초. 별도 opt-in 실제 Docker·AI 시험 2개는 기본 suite에서 제외되며, 위 실제 base 웹 시험을 별도 증거로 기록했다. 기존 의존성 폐기 경고 2개가 남는다
- 전체 기존 E2E: Chromium **65개**, Firefox·WebKit **130개** 통과. 모바일·태블릿·PC 320–1920px의 공통 화면·권한·정렬·상태·게시판 흐름 포함
- 추가 상태 갱신·입력 보존·재시도·결과 승인 E2E: 3개 엔진에서 **15개** 통과. 이 중 승인·재수정 6개는 위 전체 E2E와 중복이고, 갱신 회귀 9개가 추가다
- 실제 headful 브라우저: guest의 요청·재수정·완료 확인, admin의 승인 목록·승인, 최종 Request 상태를 직접 조작했다. 실제 결과 화면의 390px·1440px 스크린샷과 가로 넘침 없음 확인
- Django system check 오류 없음, migration drift 없음, Python compileall 및 변경 JavaScript 구문 검사 통과
- 변경 문서 8개의 로컬 링크·코드 fence 및 `git diff --check` 통과
- CI/GitHub Actions 원격 실행, 원격 push·PR·배포, Windows 호스트 실행은 수행하지 않았다

로컬 화면 증거는 Git 제외 경로 `test-results/e2e/base-approved-mobile.png`, `base-approved-desktop.png`에 있다. 자동 E2E의 격리 DB·합성 결과는 실제 Codex 성공 증거와 구분하며, 실행별 보고서는 이후 테스트 실행으로 갱신될 수 있다.

## 2026-09-14 실행 기준 변경

현재 계약은 독립 Git worktree·Docker 실행·외부 네트워크 허용이다. 이전 setsid·setpgid·effective_config 차단은 더 이상 실행 조건이 아니다. 아래의 이전 날짜별 네이티브 실패 기록과 당시의 차단 판단은 **과거 검증 이력**이며 현재 실행 조건으로 적용하지 않는다.

- 기존 파일 로그인으로 Docker 안에서 `gpt-5.6-luna / low` 응답을 실제 확인했다
- `gpt-6-astra / xhigh`가 임시 프로젝트의 독립 worktree에서 파일을 수정하고 새 Docker 컨테이너의 검사가 통과했다. 원본 Git 상태·파일은 동일했다
- Docker 파일 쓰기·외부 HTTPS 접근·미마운트 호스트 파일 비노출을 확인했다
- 명시적 Docker·Codex 시험 `tests/django/test_docker_runtime.py`: 7개 통과
- `make check`: 940개 통과, 실제 외부 실행 시험 2개 제외. 이후 종료 미확인·프로젝트 정책·Docker 의존성 회귀 확인은 233개 통과, 외부 실행 시험 2개 제외
- Chromium E2E: 63개 통과. E2E는 실제 AI 성공이 아니라 UI·역할·미준비 오류·재시도를 검증한다
- 로컬 웹·worker 재시작과 `runs.0004` migration 적용 후 계정 2개·프로젝트 2개가 유지됐다. 실제 브라우저에서 Docker 점검과 두 기본 모델 프로필 검증을 제출했고 worker 처리 후 모두 완료됐다
- Django 설정·migration drift·Python 문법·문서 로컬 링크·code fence·`git diff --check` 통과
- 전체 웹 요청에서 결과 생성 및 실제 GitHub 발행까지 완료했다는 의미는 아니다. CI 실행·push·배포는 수행하지 않았다

## 이전 검증 이력

기준일: 2026-09-13. 기준 HEAD는 `fb29cfb`이며 이 기록은 그 이후의 미커밋 작업 트리를 대상으로 한다. 기존 `devauto` 프로토타입을 교체하거나 기존 DB를 변환하지 않고 Django 앱을 추가했다. 커밋·push·GitHub 원격 변경은 하지 않았다. 각 절의 검사 결과는 해당 변경 당시의 기록이며, 메뉴·사용 가이드는 11절, 공통 디자인 검증은 12절, Account 검증은 14절, 최신 프로젝트 시작 안내 검증은 15절에 있다.

## 1. 결론

Django 웹·계정·프로젝트·대화·명세·worker·결과·승인·백업의 코드 경로와 자동 검사를 구현했다. 임시 데이터로 실제 관리자/매니저 화면과 운영 명령을 확인했다. 설치·화면 확인 방법은 [운영 절차](README.md#5-최초-설치와-로컬-웹-실행)에 있다.

**현재 실제 AI 수정 작업에 사용할 수 있는 완성 상태는 아니다.** Codex 네이티브 샌드박스에서 자식 프로세스의 `setsid`·`setpgid`가 허용되어 자손 전체 종료를 보장할 수 없다. 전체 실효 설정 감사도 미구현이다. 보호 게이트가 실제 소스 분석·수정·검사·발행을 차단하며, 개인 full-access 상속이나 관리자 수동 PASS로 우회하지 않는다.

## 2. 구현한 경계

| 위치 | 구현 내용 | 검증의 한계 |
| --- | --- | --- |
| `config/`, `manage.py` | 잠금 의존성, SQLite WAL, 비공개 데이터, loopback/프록시 검사, CSRF, SSR, 웹/worker 설정 일치·maintenance lock | 실제 HTTPS LAN·인증서 조합 미검증 |
| `apps/accounts/` | ADMIN/MANAGER, 소속·소유권, 초기 비밀번호·세션 만료, 마지막 ADMIN 보호, 역할 회수 | 두 기기의 동시 사용 미검증 |
| `apps/projects/` | Git 등록·읽기 검사, 불변 설정 revision, SHA·경로·argv·준비 자료 검증, 원본 filter 실행 차단 | 실제 네이티브 환경 준비·프로젝트 검사 미검증 |
| `apps/conversations/`, `apps/work_requests/` | 영속 메시지·polling, 첨부, 명세 제안/채택·수동 보완, 불변 확인본·멱등 제출 | 실제 모델의 요구 구체화·장기 대화 자동 요약 미검증/미구현 |
| `apps/runs/` | 2슬롯, claim/lease/epoch, PID 등록 후 실행 gate, 취소·복구, Git 독립 사본, 고정 후보·독립 AFTER | 네이티브 프로세스 이탈 때문에 실제 AI 실행 활성화 불가 |
| `apps/ai/`, `integrations/codex/` | 기존 로그인 진단, 모델/effort 버전 고정, 개인 설정 배제, 권한·기능 제한, JSONL·출력 제한 | 실제 추론 0회, 전체 실효 설정 감사·파일 도구 실증 미완료 |
| `apps/artifacts/` | 불변 manifest, 바이너리/삭제/실행 비트 패치, 검사 tree hash 결속, 비공개 다운로드·pin/만료 서비스 | 모든 실패의 결과 묶음·정리 스케줄러 미구현 |
| `apps/approvals/` | 계정 승인과 Job 원자성, 정책/결과 결속, 새 ref CAS, Draft PR·UNKNOWN/PARTIAL 복구 경로 | 실제 GitHub 조회·push·PR 미실행 |
| `apps/operations/` | 진단, writer 중지 후 SQLite API 백업, 새 경로 복원·과거 실행 권한 무효화 | 원본 인스턴스를 잃은 재해 복구는 현재 지원하지 않음 |
| `apps/portal/`, `apps/navigation.py`, `templates/includes/` | 역할별 헤더·경로·본문·푸터 분리, Admin 허브, 매니저용 Docs | 화면 이동 검증이며 실제 AI 실행·원격 발행 성공의 증거가 아님 |

## 3. 자동·정적 검사

환경: macOS 26.6 arm64, Python 3.11.14, Django 5.2.17, Gunicorn 23.0.0, 로컬 Codex CLI 0.154.0.

| 명령/범위 | 결과 |
| --- | --- |
| `uv sync --locked --extra dev --python 3.11` | 잠금 의존성 설치 성공 |
| `uv lock --check --python 3.11` | 잠금 파일 일치 |
| `make check` | **734 passed**, 102.41초. 기존 201개와 새 Django/통합 fixture 검사 포함 |
| `python manage.py check --settings=config.test_settings` | 오류 0개 |
| `python -m django makemigrations --check --dry-run --settings=config.test_settings` | 변경 없음 |
| `python -m compileall -q apps config integrations manage.py` | 성공 |
| `node --check static/js/conversation.js`, `node --check static/js/run-status.js` | 성공 |
| `uv build --wheel --out-dir <임시 경로>` | wheel 생성 성공, templates/static/migrations/worker 명령/어댑터 포함 확인 |
| `git diff --check` | 통과 |
| Markdown 경로·코드 블록 검사 | 루트 README 포함 14개 문서, 로컬 링크 106개, 코드 fence 짝 확인 |

위 Python 명령은 설치한 `.venv/bin/python`을 사용했다. Node는 JavaScript 회귀·구문 검사에 사용했으며 웹 앱 운영에 프론트 빌드 서버를 요구하지 않는다.

전체 테스트의 경고 2개는 기존 FastAPI/Starlette 테스트 경로의 httpx 사용 및 AnyIO `BlockingPortal` 별칭 폐기 경고다. 무시 설정을 추가하지 않았다. loopback 설정의 `check --deploy`는 HSTS·HTTPS redirect·Secure session/CSRF cookie 관련 경고 4개를 낸다. 기본 로컬 HTTP 예외이며 LAN 배포 검증 통과를 의미하지 않는다.

자동 검사는 실제 임시 Git·파일·SQLite와 모델/원격 대역을 함께 사용한다. 코드 수정 pipeline의 fake Codex 응답, 합성 JSONL, 로컬 Git CAS 결과를 실제 모델 호출·GitHub 발행 성공으로 계산하지 않는다. 원본 변경 대조 hash는 tracked·nonignored untracked·index·refs 대상이며 ignored 파일 전체를 읽어 확인한 증거는 아니다.

## 4. 실제 웹·운영 확인

앱의 기본 사용자 데이터가 아닌 별도 비공개 임시 경로와 비민감 Git 저장소를 사용했다. 테스트용 계정의 비밀번호·Django secret·provider 인증은 문서에 기록하지 않는다.

- 새 데이터에서 migrate → bootstrap_admin → collectstatic → Gunicorn 기동 성공.
- Chrome의 로그아웃·재로그인으로 ADMIN과 MANAGER의 별도 세션 확인. ADMIN은 프로젝트를 등록했고 MANAGER는 자기 소속 프로젝트만 조회했다. 매니저에게 관리자 메뉴·원본 절대 경로·모델 입력란이 노출되지 않았다.
- 매니저가 대화 생성 → 요청으로 정리 → 업무 언어로 명세 초안 저장을 수행했다. 준비되지 않은 프로젝트는 명세 확인/수정 시작 대신 관리자 준비 안내를 표시했다.
- `process_jobs --once`가 대화 Job을 처리했지만 `SANDBOX_UNAVAILABLE`로 종료되어 모델을 호출하지 않았다. 실패와 관리자 문의 안내를 확인했고, 새로고침 없이 응답 카드가 갱신되는 것을 재검증했다.
- `local_doctor`에서 기존 Codex 로그인·필수 CLI 옵션·DB·비공개 경로 PASS. worker 미기동/미검증 프로필은 WARN, sandbox는 NOT_RUN으로 구분했다. 진단 명령이 추론을 실행하지 않았다.
- 웹/worker 동시 기동 성공, 두 번째 worker 거부, 다른 bind/origin 설정의 동시 프로세스 거부를 실제 명령으로 확인했다.
- 웹이 살아 있을 때 백업 거부. 소유한 웹·worker를 종료한 뒤 백업과 새 경로 복원 성공. 계정 2개·프로젝트 1개 보존, 세션 0개, 새 instance, 요청 DRAFT·ready revision 해제를 확인했다.
- 별도 대기 메시지 Job 1개를 넣은 백업도 새 경로로 복원했다. 복원본 `process_jobs --once` 후 옛 Job은 `CANCELLED / RESTORED_SNAPSHOT`, AIInvocation은 `INTERRUPTED`, 활성 Job·ProcessRecord·세션은 각각 0개였다.
- 임시 원본 Git 저장소는 위 UI·worker·복원 과정 후 clean 상태였다. 실제 AI 수정 후 원본 보존까지 입증한 것은 아니다.

최초 웹 시험에서는 결과 다운로드·관리자 원격 발행을 끝까지 수행하지 않았다. 후속 Playwright에서 합성 결과의 다운로드를 확인했으며 아래 7절에 기록한다. 실제 관리자 원격 발행, LAN·두 기기·Mac 절전·재부팅·활성 모델 프로세스 강제 종료는 여전히 별도 실증 대상이다.

검증 중 발견하여 수정한 주요 통합 오류는 URL prefix 중복, 업로드 제한 시점, 대기 카드 갱신 누락, 기본 모델 seed와 테스트 데이터 충돌, 설정 불일치 기동, 발행 준비 폼의 고정 중복방지 키, WAL 백업 재검증 실패였다.

WAL 오류는 SQLite backup API로 만든 사본도 WAL 헤더를 가질 수 있는데, 첫 읽기 검증이 `-wal`·`-shm`을 생성하여 다음 복원의 정확한 파일 목록 검사를 깨뜨린 문제였다. 봉인 전 사본만 DELETE journal로 정규화하고 봉인된 백업만 `immutable=1`로 읽도록 고쳤다. 원본의 미checkpoint WAL 커밋 포함과 검증 전후 백업 파일 불변 회귀를 추가했다. 원본 DB에는 immutable 읽기를 적용하지 않는다. [SQLite WAL](https://www.sqlite.org/wal.html), [SQLite URI immutable](https://www.sqlite.org/uri.html)

## 5. 실제 네이티브 검사와 활성화 차단

실제 비밀 대신 가짜 파일·인증 경로·독립 workspace를 사용했다. 네트워크 시험은 시험용 loopback/Unix socket이며 Keychain 항목을 조회하지 않고 서비스 접근 차단만 점검했다. 모델·실제 원격 저장소는 사용하지 않았다.

| 결과 | 확인한 항목 |
| --- | --- |
| PASS 12개 | 허용 workspace 읽기/쓰기, readonly 쓰기 차단, 가짜 비밀 읽기 차단, 외부/다른 사본 쓰기·symlink 탈출 차단, loopback/Unix socket 차단, Keychain helper 접근 차단, 소스 설정 미적용, 외부 fixture 보존 등 |
| FAIL 3개 | `process_setsid`, `process_setpgid`, 종합 `process_escape`. 자식이 세션·프로세스 그룹을 이탈할 수 있음 |
| UNVERIFIED 2개 | `effective_config`: 전체 실효 설정 감사 미구현. `native_file_tools`: 동의·네이티브 선행 조건 미충족으로 미실행 |

세 FAIL은 독립적인 장애 3종이 아니라 두 이탈 시험과 그 종합 판정이다. 해당 시험 자식은 즉시 종료하고 `waitpid`로 회수했으며 daemon을 남기지 않았다. 별도로 검증한 Python 런타임의 exact read grant를 실제 sandbox 사본에 적용해 무해한 `print(123)` 명령이 exit 0인 것도 확인했다. 이 성공이 프로세스 이탈 FAIL을 상쇄하지 않는다.

현재 프로세스 그룹 신호·주기적 자손 탐색·고해상도 PID identity만으로 빠른 double-fork/세션 이탈의 전체 자손 종료를 입증할 수 없다. 따라서 검사 결과를 통과로 바꾸지 않는다. 현재 CLI/OS가 계약을 충족할 수 있는지 추가 검증하거나 실행 보호 설계를 다시 결정해야 한다. 다른 OS 사용자·VM·컨테이너 등을 추가하는 변경은 현재 기획에 자동 도입하지 않았다.

## 6. 남은 구현·인수 조건

1. 네이티브 프로세스 보호를 실제로 충족하는 실행 경로와 전체 실효 설정 감사를 완성한다. 해결 전 실제 소스/모델 작업·발행 활성화 금지.
2. 보호 통과 뒤 관리자 동의 아래 기존 로그인으로 Luna low, Astra xhigh 및 선택 ultra·파일 도구를 확인하고 실제 요청 → 수정 → 검증 → 패치 흐름을 수행한다. 내장 모델 목록·mock은 계정 지원 증거가 아니다.
3. 모든 초기 실패·취소·중단에 진단용 INCOMPLETE ResultBundle을 일관되게 봉인하는 경로를 추가한다. 현재 Run/Job 오류 기록은 있지만 모든 실패에 다운로드할 묶음이 있지는 않다.
4. 만료 서비스의 운영 실행 연결, workspace/orphan 정리, 90일 메타데이터 보관, 참조 pin 해제, 관리자 정리 사전 목록을 구현한다. 현재 디스크 자동 회수를 보장하지 않는다.
5. 첨부 이미지 inline preview와 장기 대화 자동 요약을 구현한다. 현재는 첨부 다운로드를 제공하며 대화 문맥 50,000자 초과 시 새 대화를 안내한다. 전체 필수 입력 100,000자 제한을 자동 요약으로 우회하지 않는다.
6. 실제 오프라인 의존성 준비·검사, 계정 회수 중 작업·실제 취소/재시작, HTTPS LAN을 별도 검증한다. 현재 runtime 비교는 Python major.minor이며 모든 언어·ABI 조합 지원을 뜻하지 않는다.
7. 사용자가 지정·승인한 검증용 GitHub 저장소에서만 새 ref·Draft PR·부분 실패·원격 불명 복구를 실증한다. 이번 작업에서는 원격 작업을 수행하지 않았다.

전체 완료 판정은 [인수 체크리스트](implementation.md)를 유지한다. 이 문서의 구현·테스트 개수만으로 “로컬 작업 완성” 또는 “발행 가능”을 선언하지 않는다.

최초 구현 검증 시 웹·worker와 브라우저 탭은 종료했다. 당시 기존 별도 변경인 `AGENTS.md`, `Makefile`, `package.json`, `package-lock.json`, `review-agent.md`는 내용 hash가 작업 전과 같았다. 루트 README의 이전 기획 변경을 보존하면서 현재 구현 상태만 갱신했고, HEAD와 빈 index를 유지했다. 임시 검증 자료와 실패를 재현한 최초 백업은 사용자 운영 데이터와 분리하여 남겼다. 이후 사용자가 승인한 테스트·CI 추가는 아래에 별도로 기록한다.

## 7. 후속 로컬·Docker·Playwright·CI 검증

2026-09-11, 같은 HEAD 이후의 미커밋 구현을 다시 검사했다. 사용자의 명시적 동의에 따라 테스트·CI 구성까지 추가했으며 제품의 실행 보호 조건은 변경하지 않았다. 반복 실행 명령과 인증 경계는 [테스트 운영 안내](testing.md)에 있다.

| 범위 | 확인 결과 |
| --- | --- |
| Mac Python 전체 | **734 passed**, 실패·skip 0, 기존 경고 2개, 102.71초 |
| Linux ARM64 Docker Python 전체 | **734 passed**, 실패·skip 0, 기존 경고 2개, 41.41초. 이 중 Django 533개 |
| Mac Playwright | Chromium·Firefox·WebKit 각 8개, **24 passed**, 31.4초; 실제 웹·worker·SQLite·HTTP 사용 |
| Docker Playwright | 공식 이미지 빌드 → 네트워크 없는 컨테이너 실행 → 같은 3개 브라우저 **24 passed**, 29.9초 → 보고서 복사·컨테이너 정리 |
| 정적·설정 | uv lock·의존성 호환, Django system check·migration drift, Python/JS 문법, shell 문법, JSON/YAML, diff 검사 통과 |
| GitHub Actions | `ci.yml` 추가, actionlint 1.7.12 통과. **원격 runner 미실행** |
| 실제 로컬 Codex | 0.154.0 로그인·CLI 계약 정상, 네이티브 **12 PASS / 3 FAIL / 2 UNVERIFIED** 재현. 추론 0회 |
| GitHub 발행·CD | push·PR·workflow dispatch·배포 0회 |

Mac은 macOS 26.6 arm64, Python 3.11.14, Node 26.8.2, uv 0.12.12다. Linux는 Playwright 1.63.0-noble ARM64, Python 3.11.16, Node 24.20.0, uv 0.12.12다. GitHub의 Ubuntu/macOS runner 또는 AMD64 Docker를 실제 실행한 증거는 아니다.

### 발견한 테스트 환경 문제와 수정

1. Linux 최초 전체 검사는 **726 통과 / 8 실패**였다. 기존 Docker preview fixture가 외부 명령만 mock하고 `shutil.which("docker")`는 실제 호스트에 의존했다. Docker CLI 없는 동일 환경에서 실패를 재현한 다음, 이미 mock된 Docker 실행과 함께 발견 여부만 대역 처리하여 734개 통과를 확인했다. 제품의 설치 검사나 실제 Docker 실행 로직은 바꾸지 않았다.
2. 임시 다중 계정 WebKit 시험은 `Provisiolal navigation canceled.`로 실패했다. 컨테이너의 PID 한도 도달 기록(`pids.events: max 9`)을 확인했다. 페이지 활성화 변경은 해결하지 못했고, 다른 조건을 유지한 채 PID 한도를 256 → 512로 변경하자 동일 WebKit 10개 시험이 통과했다. 정식 Docker 명령에도 512를 적용한다. 테스트 retry·skip으로 덮지 않았다.
3. Mac의 `/var` 임시 경로는 symlink이므로 fixture의 생성 경로를 `realpath`로 고정했다. E2E 종료는 자신이 생성한 서비스 프로세스 그룹만 처리하고, 한 서비스 종료 실패가 다른 서비스의 정리를 생략하지 않게 했다. SIGTERM을 무시하는 합성 후손을 만든 별도 진단 3회에서도 종료 후 해당 그룹의 프로세스 0개를 확인했다. 이것은 시험 서버 정리 검증이며 실제 Codex의 프로세스 이탈 보호 증거가 아니다.

### 브라우저 증거와 한계

로그인·관리자 화면·매니저 접근 거부·다른 소유자 자료 404·유효 CSRF가 있어도 매니저 발행 준비 403·문자열 이스케이프·보호 미충족 응답 polling·첨부·초안 저장·비공개 패치 다운로드·로그아웃을 확인했다. 모바일 크기의 결과/초안 스크린샷도 확인했다. 매니저의 자기 발행 목록 조회는 허용 계약이며, 승인 권한과 혼동하여 `/approvals/` 자체를 403으로 기대하지 않는다.

다운로드할 결과와 검사 상태는 **합성 fixture**다. 실제 AI 생성 결과·완료 판정·승인 후 원격 발행의 E2E가 아니다. 대화는 보호 미충족 상태에서 안전하게 실패해야 통과하며, 호스트 Codex 인증을 UI 시험에 전달하지 않는다. 시험 원본 Git은 clean·기준 commit 불변·remote 0개를 확인한다.

로컬 보고서는 `test-results/e2e/results.json`, `source-integrity.json`, `html/`에, Docker 보고서는 실행별 `test-results/e2e/docker-<고유값>/`에 남는다. 보고서는 Git 제외이며 단일 브라우저/항목 실행 시 최상위 보고서가 바뀔 수 있으므로 실제 `stats.expected`, `skipped`, `unexpected`, `flaky`와 실행 시각을 확인한다. 최종 전체 실행은 24개·skip 0·실패 0·flaky 0으로 구분한다.

이번 최종 Docker 보고서는 `test-results/e2e/docker-p543QHRm/`이다. Mac·Docker 양쪽 JSON의 24개 성공·오류 0·원본 보존 판정을 다시 읽어 확인했고, 이미지에 포함된 최종 E2E setup 파일의 SHA-256도 로컬 파일과 일치했다. 이미지 안에는 `.git`, `.env`, `.codex`, `.docs`, `docs`, Docker socket이 없었다.

추가한 CI는 PR/main push/manual, Ubuntu/macOS Python 검사와 Ubuntu Docker E2E만 수행한다. secret이나 CD job은 없다. 현재는 `OPENAI_API_KEY`를 요청할 필요가 없으며, 키를 추가해도 `effective_config` 미구현과 네이티브 프로세스 보호 실패가 해결되지는 않는다.

테스트를 위해 `Makefile`, npm manifest/lock의 기존 변경을 보존하며 E2E 명령·의존성만 추가했다. `AGENTS.md`·`review-agent.md`·기존 제품 변경은 건드리지 않았다. 커밋·push·원격 설정 변경 없이 HEAD와 빈 index를 유지한다. 테스트 전용 컨테이너는 제거하고 기존 다른 프로젝트 컨테이너·볼륨은 보존하며, 이미지 캐시는 남긴다.

## 8. 더미 게시판 기반 후속 검증

같은 날 후속 요청에 따라 [게시판 프로젝트·기획서](../../tests/fixtures/board/README.md)와 [재현 절차](board-demo.md)를 추가했다. 위 734개/24개는 이전 검증 기록이며, 게시판을 추가한 전체 검사 결과는 다음과 같다.

| 범위 | 결과 |
| --- | --- |
| Mac Python 최종 전체 | **749 passed / 2 failed**, 총 751개, skip 0, 기존 경고 2개, 114.56초. 수동 검증 후 launcher 회귀까지 포함해 재실행했으며 `make check` 실패 |
| 게시판 fixture | 위 전체에 포함된 8개 통과. 실제 HTTP·WSGI, 의도 손상·검색 미구현 감지, 덮어쓰기 거부, idle TCP 회귀 |
| 게시판 Django 시나리오 | 위 전체에 포함된 7개 중 5개 통과, JUnit 형식 불일치 2개 실패 |
| Mac Playwright | **27 passed**, 66.91초. Chromium·Firefox·WebKit 각 9개, skip·실패·flaky 0 |
| Docker Playwright | **27 passed**, 61.27초. 동일 3개 엔진, 네트워크 없는 Linux ARM64 컨테이너, skip·실패·flaky 0 |
| 정적 검사 | uv lock·의존성, Django check·migration drift, Python/JS/shell 문법, diff 검사 통과 |

Mac 최종 전체 Python 결과는 `test-results/mac-final-check-V6kMUppc/summary.json`, Mac 브라우저 결과는 `test-results/e2e/results.json`과 `html/`, Docker 결과는 `test-results/e2e/docker-2b4p57gp/`에 보존했다. 각 브라우저 보고서는 실제 접수/재시도 상태 JSON과 게시판·초안·실패 화면을 포함한다. Mac·Docker 모두 원본 clean, 기준 commit 불변, remote 0개, 독립 게시판 원본 3개를 확인했다. 이번 새 Python 전체 검사는 Mac에서 실행했으며 이전 Linux 734개 기록을 새 751개 검증으로 계산하지 않는다. launcher 회귀 추가 전 747 통과/2 실패였던 중간 전체 결과는 `test-results/mac-board-check-zb1KqA7s/summary.json`에 별도로 남겼다.

### 실제로 발견한 결함

1. **제품 JUnit 형식 불일치 — 이 검증 당시 회귀 2개 실패, 이후 9절에서 수정.** 프로젝트 등록은 `junit-xml`만 허용하지만 당시 `assess_check`는 `junit`만 처리했다. 실제 게시판 HTTP 검사 정상 6개 통과와 의도 손상 5개 통과/1개 실패가 모두 `UNVERIFIED / PARSER_UNSUPPORTED`로 오판됐다. 실패를 xfail·skip으로 바꾸지 않았으며, 당시 전체 테스트 통과·커밋 준비 완료로 판정하지 않았다.
2. **더미 서버의 빈 연결 점유 — 수정·회귀 통과.** 동기 WSGI 서버가 WebKit의 빈 TCP 연결을 기다리며 상세 요청을 막았다. 브라우저 없이도 다른 HTTP 요청의 2초 timeout을 재현했다. 더미 CLI만 스레드 서버로 변경한 뒤 같은 최소 재현과 원래 WebKit 시나리오가 통과했다. 검색 기능·제품 서버·테스트 timeout·retry는 변경하지 않았다. [Python socketserver](https://docs.python.org/3/library/socketserver.html)
3. **제품 작업 정책 폼의 JSON 초기값 — 미수정.** 이미 직렬화한 문자열을 `forms.JSONField`가 다시 직렬화하여 기본 빈 목록이 `[]`가 아닌 JSON 문자열 `"[]"`로 표시된다. 브라우저 시나리오에서는 검증 목록과 오프라인 준비 목록 `[]`를 명시적으로 입력하여 진행했다. 기본값을 그대로 저장하는 흐름까지 정상이라는 증거는 아니다.
4. **종료 후 재시도 버튼 갱신 — 미수정.** 상태 polling은 실패를 표시하지만 재시도 폼을 새로 만들지 않는다. 페이지를 다시 연 다음 새 실행으로 재시도했고, 이전 Run과 명세가 바뀌지 않음을 확인했다.

### 성공 판정의 경계

새 브라우저 시나리오는 프로젝트·작업 정책·소속·첨부·명세·Run을 웹에서 실제로 만든다. 대역 성공 결과를 넣지 않으며 실제 worker의 `QUEUED → PREPARING → FAILED / SANDBOX_UNAVAILABLE`와 별도 Run으로 재시도하는 흐름을 검증한다. 같은 프로젝트의 다른 매니저에게 요청·실행 GET뿐 아니라 유효 CSRF를 가진 초안 수정·재시도 POST도 404인지 확인했다.

게시판 검색은 의도적으로 미구현이며 검색 인수 검사는 HTTP·WSGI 각각 6개 중 5개 실패가 정상적인 변경 전 증거다. 이 실패 감지를 검사하는 상위 fixture 테스트의 통과를 검색 기능 완료로 해석하지 않는다. 기존 baseline 통과 역시 검색 완료가 아니다. 실제 AI 수정·패치 생성·원격 발행은 이번 자동 E2E에서 수행하지 않았다.

### 수동 실행 도구의 인증 환경 분리

후속 독립 리뷰에서 수동 launcher가 API 키 접미사만 제외하고 환경을 상속해 GitHub/AWS 토큰·SSH 인증 소켓도 더미 게시판에 전달할 수 있음을 발견했다. 합성 환경변수로 재현한 뒤 앱 환경은 `HOME`, 선택 `CODEX_HOME`, `PATH`, `TMPDIR`, `LANG`, `LC_ALL`과 명시적 로컬 앱 설정만 허용했다. 게시판은 별도 빈 HOME·TMPDIR, 최소 환경과 자기 작업 디렉토리를 사용한다. 실제 비밀 유출이나 외부 전송을 관찰한 것은 아니다.

`tests/test_board_demo_launcher.py`의 회귀 2개는 각각 RED → GREEN을 확인했고 별도 실행도 **2 passed**였다. 중간 749개 전체 실행에는 없었지만, 수동 검증 후 다시 실행한 최종 751개 전체 검사에는 포함했다. 수정 후 독립 재리뷰에서 추가 확정 결함은 없었다. 해당 launcher는 자동 Docker E2E의 실행 경로가 아니며, 보완 이후 아래 수동 검증에서 실제 사용했다.

### 자동 검사 후 새 앱을 직접 실행한 결과

중간 Python 전체 749개, Mac·Docker 브라우저 각 27개, launcher 추가 회귀 2개를 실행한 뒤 `serve-board-demo.py`로 새 Gunicorn·worker·게시판을 기동했다. Chrome에 새 탭을 열어 실제 UI를 조작했으며 기존 운영 DB나 자동 테스트 seed를 재사용하지 않았다. 기동 당시 최초 관리자만 존재했다. 수동 검증 종료 후 최종 Python 전체 751개도 재실행했다.

1. 게시판 목록 3개와 상세를 확인하고 관리자 `demo-admin`으로 로그인했다. 웹에서 게시판 Git 등록, 읽기 `.`, 수정 `board.py`, 필수 baseline WSGI 검사, 매니저 `demo-manager` 생성·프로젝트 소속 부여를 수행했다.
2. 실제 관리자 화면에서 네이티브 점검을 접수하고 worker 처리 후 **15 PASS / 3 FAIL / 2 UNVERIFIED**를 확인했다. 15 PASS에는 CLI 계약 3개가 포함된다. 프로세스 이탈 FAIL과 실효 설정 감사 미구현은 그대로이며 수동 PASS를 넣지 않았다.
3. `CLI·로그인 상태 확인` 버튼은 JSON 응답으로 이동하는 과정에서 Chrome `ERR_BLOCKED_BY_CLIENT` 화면이 나타났다. 브라우저 차단을 우회하지 않았고 UI 연결 결과 표시 성공으로 계산하지 않는다. 동일 launcher의 앱 환경에서 별도로 실행한 `codex login status`는 exit 0, `Logged in using ChatGPT`였다. 현재 관찰만으로 특정 확장 프로그램이나 앱의 결함으로 단정하지 않는다.
4. 관리자 로그아웃 → 매니저 최초 로그인 → 초기 비밀번호 변경 → 새 비밀번호 재로그인을 수행했다. 관리자 메뉴·호스트 절대 경로·모델 입력란이 매니저에게 보이지 않음을 확인했다.
5. 매니저가 검색 개선 대화를 작성하고 브라우저 파일 선택기로 실제 `requirements.md`를 첨부했다. 첨부 링크와 선택 유지, 업무 언어의 목표·완료 조건·수정/제외 범위를 저장한 초안 2를 확인했다. AI 제안은 보호 실패로 종료됐으며 수동 명세 작성과 혼동하지 않는다.
6. 명세 v1 확인만으로 수정이 실행되지 않음을 확인하고 `수정 작업 시작`을 눌렀다. 실제 Run `4637fd87-3c57-43f8-896b-0edd6345d088`은 polling으로 `FAILED / SANDBOX_UNAVAILABLE`를 표시했다. 새로 페이지를 열어 재시도한 Run `b54aacbb-837f-4657-9543-14d475e7a360`도 같은 원인으로 종료됐다.
7. 읽기 전용 SQLite 조회로 두 Run의 `QUEUED → PREPARING → FAILED`, 같은 불변 revision, 재시도 `previous_run_id` 연결, 첨부 1개를 확인했다. 수정 프로필은 `gpt-6-astra / xhigh`, 대화·요구 정리는 `gpt-5.6-luna / low`로 기록됐다. 검증된 모델로 표시하지 않았고 실제 모델 호출 0회, 작업 사본·CheckResult·ResultBundle 0개였다. ProcessRecord 4개는 모두 `SANDBOX_CHECK / STOPPED`이며 수정 모델 프로세스가 아니다.
8. 실패 화면과 여전히 검색이 없는 게시판을 시각 확인했다. 실행 이벤트의 빈 `message` 두 개가 화면에서 빈 번호 항목으로 보이는 표현 문제도 관찰했으며 제품 코드는 수정하지 않았다. 요청 화면은 최초 실행 링크를 보여 주지만 재시도 목록 전체를 보여 준다고 판단하지 않는다.
9. 매니저 로그아웃, 이번에 만든 Chrome 탭 종료, launcher Ctrl+C를 수행했다. 소유한 웹·worker·게시판과 Gunicorn 자식 종료, 원본 clean·commit 불변·remote 0, cleanup 오류 0을 확인했다. 기존 다른 프로젝트 컨테이너 5개는 유지했다.

수동 증거는 OS 임시 경로의 `aiqa-board-demo-vtckaj49/` 아래 `manifest.json`, `services.log`, 별도 `runtime/db.sqlite3`에 보존했다. 시험용 계정 정보도 있으므로 이 디렉토리를 외부에 업로드하지 않는다. 로컬 요약은 `test-results/manual-board-verification.json`에 있다. 현재 서버는 종료했으며 다시 확인하려면 [재현 절차](board-demo.md)의 launcher를 실행한다.

**이 단계의 판정:** 더미 프로젝트와 기획서, 자동·직접 브라우저의 계정별 수정 접수·차단·재시도 검증은 수행했다. 실제 Codex가 검색 코드를 수정하고 인수 검사를 통과하는 완료 흐름은 보호 차단으로 수행하지 못했다. 당시 JUnit 제품 회귀도 2개 실패 상태여서 전체 green이나 실사용 수정 완료로 선언하지 않았다. 이후 사용자 승인을 받은 JUnit 수정만 아래에 별도로 기록한다. 실행 보호 문제를 API 키나 원격 작업으로 우회하지 않는다. 제품 코드·커밋·push·GitHub Actions 원격 실행·배포는 이 테스트 고도화 단계에서 수행하지 않았다.

## 9. JUnit 형식 불일치 수정

2026-09-11, 사용자가 해당 오류만 수정하도록 승인한 뒤 실행기의 형식 선택 조건 한 줄을 변경했다. `apps/runs/verification.py`는 등록 형식 `junit-xml`을 기존 JUnit 판독 로직으로 처리하고, 기존 `junit`과 `junit-v1 → junit` 호환성을 유지한다. 신규 결과의 `parser_id`는 등록값 `junit-xml`을 그대로 보존한다. 등록 정책·DB schema·기존 봉인 결과는 변경하지 않았다.

수정 전 실제 게시판 HTTP 회귀 2개가 실패하는 것을 재확인했다. 기존 건수·해시 검사를 세 형식으로 확장했을 때도 `junit-xml`만 실패하고 기존 두 형식은 통과했다. 수정 후 정상 보고서의 PASS와 손상 보고서의 FAIL을 확인했다. `tests/django/test_verification_local.py`의 기본 JUnit 입력을 등록 형식으로 맞춰 기존 경로·symlink·시각·크기·XML entity·건수 충돌·전부 skip 거부 검사도 같은 형식으로 실행했다. 증거 검증 조건을 완화하거나 실패를 생략하지 않았다.

| 검사 | 결과 |
| --- | --- |
| `pytest tests/django/test_verification_local.py tests/django/test_board_scenario.py tests/django/test_projects.py -q` | **154 passed**, 40.95초 |
| `make check` | **753 passed**, 실패·skip 0, 기존 경고 2개, 119.55초, exit 0 |
| 변경 Python 문법·Django system check·migration drift·diff 검사 | 통과, system check 오류 0, migration 변경 없음 |
| 독립 코드 리뷰 | 반환 형식·호환성·증거 안전성 검사에서 추가 결함 없음 |

이전 751개 전체 검사에 형식 호환성 사례 2개를 추가해 총 753개다. 기존 게시판 회귀 2개의 실패도 해소됐다. standalone lint 명령은 구성되어 있지 않아 문법 검사와 구분한다. 브라우저·Docker E2E·실제 Codex 호출·원격 GitHub Actions는 이번 판독기 수정에서 재실행하지 않았으며 앞 절의 실행 기록과 혼동하지 않는다.

작업 정책 폼의 JSON 초기값, 종료 후 재시도 UI, Codex 실행 보호는 변경하지 않았다. 따라서 Python 전체 검사가 통과해도 실제 AI 수정 활성화·검색 구현 완료를 뜻하지 않는다. `docs/operations/testing.md`와 `board-demo.md`는 JUnit 수정 상태만 반영했다. 기존 로컬 변경은 보존했으며 HEAD `fb29cfb3`와 빈 index를 유지하고 커밋·push는 하지 않았다.

## 10. 내부 사용 비밀번호 정책 변경

2026-09-12, 사용자 요청에 따라 현재 비밀번호 검증은 **최소 6자만** 요구하도록 변경했다. `config/settings.py`에서 최소 길이를 12 → 6으로 바꾸고 사용자 정보 유사성·흔한 비밀번호·숫자 전용 제한을 제거했다. Django 해시 저장, 초기 비밀번호 변경 절차, 이전 비밀번호 확인, 세션 무효화, 계정별 권한은 유지한다. 외부 공개 시의 8자 이상·영어 대문자 1개 이상·특수문자 1개 이상은 [계정 정책](../accounts/README.md)에 후속 결정으로 기록했으며 현재 적용하거나 접속 주소로 자동 선택하지 않는다.

- 수정 전 숫자 6자·소문자 6자·사용자 이름과 같은 비밀번호의 계정 생성 회귀 3개가 실패했고, 수정 후 모두 통과했다.
- 계정 생성·변경·재설정의 5자 이하 거부, 6자 허용, bootstrap의 6자 허용 및 기존 세션·권한 검사까지 계정 테스트 **84개 통과**.
- `make check`: **762 passed**, 실패·skip 0, 기존 경고 2개, 111.90초. Django check 오류 0, migration 변경 없음, 변경 Python 문법·diff·문서 링크 검사 통과. standalone lint는 미구성이다.
- 실행 중이던 데모의 DB·secret·계정·프로젝트를 재생성하지 않고 웹·worker·게시판을 새 프로세스로 재기동했다. `admin`·`guest`의 ID·역할·권한 버전·기존 비밀번호는 보존했다.
- 재기동한 실제 HTTP에서 `admin/123456`, `guest/123456` 로그인, 관리자 화면 200/403을 확인했다. 관리자 계정 생성 폼에서 이미 존재하는 `admin`을 대상으로 5자는 비밀번호 오류 400, 6자는 비밀번호 검증을 통과한 뒤 중복 사용자 오류 409가 되는 것을 확인했다. 검사 때문에 추가 계정을 만들거나 기존 계정을 수정하지 않았다. 검증용 로그인 세션은 로그아웃했다.

기존 데모 주소 `http://127.0.0.1:8000`와 게시판 `http://127.0.0.1:8099`를 계속 실행 중이다. 재기동은 같은 runtime을 사용하는 별도 감독 세션이며 `manifest.json`에 새 PID와 `resumed-existing-runtime`을 기록했다. 이번 변경은 사무실 LAN 공개·외부 공개·HTTPS 설정·Codex 실행 보호 변경이 아니다. 브라우저 E2E·Docker·AI 호출·원격 Actions는 재실행하지 않았다. 커밋·push 없이 기존 작업 트리를 보존했다.

## 11. 공통 메뉴와 매니저 사용 가이드

2026-09-12, 헤더를 `Project / Request / AI / Admin / Docs / Account` 순서로 변경했다. `AI`·`Admin`은 관리자 전용이며, Docs·Account는 접근 가능한 이름과 툴팁을 가진 아이콘이다. Admin은 기존 요청 승인·계정 관리·운영 상태의 진입점이다. 요청 승인은 기존 원격 발행 승인으로, 로컬 코드 수정 시작에 새 관리자 승인을 추가하지 않았다. 매니저의 권한 있는 발행 상태 조회도 유지했다.

`/docs/`에는 한국어 매니저 사용 가이드를 추가했다. 로그인·프로젝트 선택부터 대화·명세 준비·수정 시작·진행 상태·결과 확인·계정·로그아웃까지 실제 버튼명으로 설명한다. 저장소의 개발 기획 파일을 읽어 서비스에 공개하지 않으며, Docs와 Admin은 GET 렌더링·기존 권한 검사만 수행한다. [화면 구성 기획](../navigation/README.md)과 계정·프로젝트·대화·요청·운영 문서도 같은 메뉴·경로 정의로 갱신했다.

| 검사 | 결과 |
| --- | --- |
| `make check` | **857 passed**, 실패·skip 0, 기존 경고 2개, 116.38초 |
| `playwright test --project=chromium` | **13 passed**, 실패·skip·flaky 0, 24.5초 |
| 메뉴·권한·경로 회귀 | 역할별 순서, 아이콘 이름, 활성 메뉴, Admin 링크, 직접 접근 거부, 안전한 경로, Docs 목차·링크·POST 거부 확인 |
| 브라우저 E2E | 1280px·390px 레이아웃, 관리자·매니저의 Docs/Account 이동, 키보드 Enter, 가이드 내부 목차, 기존 계정·게시판 흐름 확인 |
| 정적·실행 설정 검사 | 변경 Python·JavaScript 문법, Django system check, 문서 링크·형식, diff 검사 통과. standalone lint는 미구성 |

실행 중인 `http://127.0.0.1:8000`의 웹 프로세스만 재적용하고 정적 파일을 갱신했다. 실제 Chrome에서 관리자 세션을 유지한 채 Project·Admin·Docs·Account를 확인하고, 가이드 목차와 Admin의 요청 승인·계정 관리 링크를 직접 눌렀다. 활성 메뉴, 경로, 아이콘 표시와 Account 하단 로그아웃을 확인했으며 이 확인을 위해 계정·프로젝트·DB를 재생성하거나 로그아웃하지 않았다. 매니저·모바일·키보드 검증은 별도 E2E 환경에서 수행했다.

이번 검증은 로컬 Python·Chromium 및 직접 브라우저 확인이다. Docker·다른 브라우저 엔진·실제 Codex 호출·원격 GitHub Actions·push·PR·배포는 재실행하지 않았다. 1절의 실제 AI 수정 보호 차단을 해소했다는 의미가 아니며, 기존 작업 트리를 보존하고 커밋·push하지 않았다.

## 12. 공통 디자인과 폼 정렬 개선

2026-09-12, 공통 화면을 흰 배경·중립색·얇은 구분선 중심으로 정리했다. 큰 둥근 카드와 과한 버튼·여백을 줄이고 Header·Navigation·Main·Footer의 좌측 정렬 기준을 맞췄다. 정상 페이지에서 네비와 중복되는 제목·상위 목록·돌아가기 링크를 제거했다. 오류 복구·실행 상태·업무 전환 링크와 실제 저장·동의·승인·취소 동작은 유지했다.

공통 필드 템플릿은 라벨·입력·오류·도움말을 같은 규칙으로 출력하며, 단일 체크박스는 라벨과 같은 줄에서 시작한다. 복수 선택은 fieldset/legend와 16px 하단 간격을 유지한다. hidden 값, 필드 이름, 필수·선택 상태, 오류·도움말 ID 연결, HTML 이스케이프를 검사했다. AI 모델 표는 모바일 세로 배치로 전환하고 일반 표는 폭을 채우는 별도 스크롤 영역으로 감쌌다. 자세한 기준은 [공통 레이아웃](../navigation/README.md#6-레이아웃과-코드-배치)에 있다.

| 검사 | 결과 |
| --- | --- |
| 공통 디자인 반영 후 `make check` | **861 passed**, 기존 경고 2개, 153.40초 |
| `playwright test` | Chromium·Firefox·WebKit 각 15개, 총 **45 passed**, 실패·skip·flaky 0, 89.7초 |
| 화면 검증 | 1280px·390px 정렬 기준, 체크박스 라벨 클릭, 폼·버튼 높이, 표 너비·가로 넘침, 첨부 선택 간격, 기존 게시판 요청·권한 흐름 확인 |
| 정적 검사 | Django system check, 변경 Python·JavaScript 문법, 문서 링크·형식, diff 검사 통과. standalone lint는 미구성 |

이후 계정 관리 폼의 기존 DOM ID 중복을 발견했다. 추가 회귀는 수정 전 ID 30개 중 고유 ID 12개로 실패했다. 계정별 change/reset/membership 폼의 `auto_id`만 분리한 뒤 계정 테스트 **89 passed**와 해당 관리자 화면의 3개 브라우저 E2E **3 passed**를 확인했다. 이 추가 회귀 1개는 앞의 861개 전체 실행에 포함되지 않았으며, 후속 영향 범위 검사로 확인했다. POST 필드 이름·생성 폼·계정 처리·권한은 변경하지 않았다.

실행 중인 로컬 앱의 정적 파일과 웹 프로세스를 갱신했다. 실제 Chrome에서 프로젝트 등록 폼·AI 모델 표·Admin 목록을 직접 확인했고, 데스크톱·모바일 E2E 캡처도 시각 검토했다. 계정 관리 페이지를 추가 확인하는 과정에서 브라우저 연결이 timeout되어 그 화면의 최종 라벨 클릭 검증은 별도 E2E 결과로 구분한다. 기존 로컬 계정·프로젝트·DB는 재생성하거나 수정하지 않았다.

이 검증은 UI·폼·로컬 회귀 검증이다. 실제 Codex 모델 호출·Docker·원격 Actions·push·PR·배포는 실행하지 않았고, 기존 AI 실행 보호 상태를 변경하지 않았다. 커밋·push하지 않았다.

## 13. Account 본인 정보 관리

2026-09-12, Account를 로그인 본인의 이름·이메일 수정, 비밀번호 변경, 하단 로그아웃으로 한정했다. 역할·프로젝트 목록·관리자 문의 문구는 제거했다. 다른 사용자 계정·소속·권한 관리는 기존 관리자 전용 `Admin → 계정 관리`에 유지한다. 계정 기획·도메인 정의·메뉴 기획·웹 사용 가이드를 같은 범위로 갱신했다.

이름은 1~100자 필수, 이메일은 254자 이하 선택 입력이다. 기존 `User.email`을 사용하므로 DB migration은 없다. 로그인 ID·역할·비밀번호·세션 버전을 변경하지 않으며 이메일 인증·복구·로그인 기능은 추가하지 않았다. 서버가 로그인 본인을 수정 대상으로 고정하고 이름·이메일 두 필드만 검증·저장한다. 실제 변경은 두 필드의 감사 기록을 남기며, 같은 값의 재저장은 추가 기록을 만들지 않는다. 비밀번호 변경은 기존 현재 비밀번호 확인·6자 이상·재로그인 절차를 유지한다.

| 검사 | 결과 |
| --- | --- |
| 수정 전 화면 회귀 | ADMIN/MANAGER의 이름·이메일 입력란 회귀 **2 failed**로 재현 |
| 본인 정보 추가 회귀 | 입력 검증·CSRF·본인 대상 고정·권한/비밀번호/세션 보존·초기 비밀번호 제한·감사·재조회 등 **26 passed** |
| `make check` | **888 passed**, 기존 경고 2개, 130.79초 |
| `playwright test` | Chromium·Firefox·WebKit 각 15개, 총 **45 passed**, 실패·skip·flaky 0, 약 1.5분 |
| 브라우저 저장 검증 | 별도 E2E 데이터에서 관리자·매니저의 이름·이메일 저장·새로고침 유지·이메일 형식·비밀번호 화면 이동 확인, 시험 후 프로필 원래 값 복구 |
| 실행 설정·정적 검사 | 동일 로컬 runtime의 Django system check 오류 0, migration 변경 없음, Python·JavaScript 문법·문서 링크·diff 검사 통과. standalone lint는 미구성 |

실행 중인 로컬 앱의 웹 프로세스를 재적용하고 실제 Chrome의 관리자 세션에서 Account 입력란·저장 버튼·비밀번호 변경 링크·하단 로그아웃을 시각 확인했다. 비밀번호 화면의 현재/새 비밀번호 입력란과 Account 복귀도 직접 확인했다. 브라우저 연결 방식의 timeout 이후 네이티브 Chrome 조작으로 확인했으며, 실제 로컬 계정의 이름·이메일·비밀번호는 시험 목적으로 수정하지 않았다.

독립 읽기 전용 리뷰에서 이번 범위의 확정된 기능 오류·권한 우회·문서 불일치는 발견하지 못했다. Docker·원격 Actions·실제 AI 호출·push·PR·배포는 실행하지 않았으며 기존 AI 실행 보호 상태와 다른 로컬 변경을 보존했다. 커밋·push하지 않았다.

## 14. Account 아이디·닉네임 분리와 이메일 제거

2026-09-12, 후속 사용자 요청으로 13절의 이메일 편집을 폐기하고 Account를 아이디(읽기 전용)·닉네임 수정·비밀번호 변경·하단 로그아웃으로 변경했다. 아이디는 기존 username, 닉네임은 기존 display_name을 사용한다. 이메일은 화면·폼·저장 서비스 입력에서 제거했다. Django 호환용 email 컬럼과 기존 데이터는 삭제하지 않았으며, 위조 POST에도 이메일·아이디·타인 계정·권한이 변경되지 않는 회귀를 유지했다. 계정·도메인·메뉴 기획과 웹 사용 가이드를 갱신했다.

계정 회귀 **111 passed**, Chromium·Firefox·WebKit의 E2E는 각 15개, 총 **45 passed**였다. E2E는 닉네임 저장·재조회, 아이디 읽기 전용·불변, 이메일 미노출, 비밀번호 화면 이동과 기존 로그아웃 흐름을 확인했다. 변경 Python·JavaScript 문법, Django system check, 문서 링크·코드 블록·diff 검사를 통과했다. standalone lint는 미구성이다.

최종 `make check`는 **884 passed**, 기존 경고 2개, 121.40초였다. 이전 888개에서 폐기된 이메일 저장·형식 검사 사례를 이메일 미노출·위조 입력 무시 검사로 정리하면서 총 4개가 줄었으며 실패·skip은 없다.

기존 로컬 runtime의 웹 프로세스만 갱신했다. `admin`의 닉네임 `관리자`, `guest`의 닉네임 `게스트`, 두 계정의 사용자 지정 비밀번호·활성 상태·역할이 이미 일치하여 계정이나 비밀번호를 덮어쓰지 않았다. 실제 로컬 HTTP에서 두 계정으로 각각 로그인해 새 Account 응답과 비밀번호 링크를 확인하고 시험 세션만 로그아웃했다. 최종 화면의 브라우저 상호작용은 별도 E2E 환경의 결과이며, 실제 사용자 계정의 닉네임·비밀번호를 시험 목적으로 변경하지 않았다.

IP 표시는 이번 변경에 추가하지 않았으며, 접속 주소·프록시·외부 조회·AI 실행 보호 정책도 변경하지 않았다. Docker·원격 Actions·실제 AI 호출·커밋·push는 실행하지 않았다.

## 15. 첫 프로젝트 시작 안내와 등록 가이드

2026-09-13, 비어 있는 Project 메인과 등록 폼의 안내 누락을 수정했다. 관리자에게 서비스 목적·등록 버튼·시작 순서·등록 가이드를 제공하고, 프로젝트가 없으면 시작 순서를 펼치고 기존 프로젝트가 있으면 접는다. 매니저에게는 업무 언어 예시와 요청 순서·사용 가이드만 제공한다. 등록 폼은 서버 PC의 Git 폴더 경로 예시, 선택 입력, 기본 기준 버전, 등록 전 준비·등록 후 설정을 설명한다. Docs에 관리자 전용 등록·오류 대응 섹션을 추가했다.

준비 상태는 기존 ProjectHealth의 마지막 진단과 시각을 표시하며, AI 실행 보호·모델 검증 완료와 구분한다. GET에서 진단·CLI·모델을 실행하지 않고 신뢰 확인을 자동 체크하지 않는다. 등록·할당·실행·승인 정책과 DB schema는 변경하지 않았다.

- 안내 회귀 수정 전 **4 failed / 1 passed**, 수정 후 프로젝트·포털 검사 **135 passed**. 이후 기존 프로젝트의 안내 접힘·진단 의미 검사도 전체 실행에 포함했다.
- 최종 `make check`: **886 passed**, 기존 경고 2개, 162.76초.
- 전체 E2E: **44 passed / 1 failed**. 프로젝트 등록 가이드 이동, 1280px·390px 정렬·도움말 연결·동의 기본값, 매니저 안내와 기존 게시판 등록·요청 흐름은 세 엔진에서 통과했다. 실패는 Firefox의 기존 관리자 화면 검사에서 로그인 페이지 `page.goto`의 load 대기 45초 초과로, 변경한 프로젝트 화면에 도달하기 전 발생했다. 초기 실패 기록은 `test-results/e2e/results.json`과 실패 trace에 보존한다.
- 실패한 Firefox 검사만 `--repeat-each=3 --reporter=list --output=test-results/e2e-firefox-recheck`로 분리 재실행해 **3 passed**, 18.7초를 확인했다. 코드·시간 제한·재시도 설정은 바꾸지 않았다. 반복에서 재현되지는 않았지만 최초 전체 실행을 45개 무실패로 기록하지 않으며, 일시적 로딩 지연의 구체적 원인은 확정하지 않았다.
- Python·JavaScript 문법, Django system check, 문서 링크·코드 블록·diff 검사 통과. standalone lint는 미구성이다.

동일 runtime의 웹 프로세스만 갱신해 실제 Chrome에서 빈 Project와 등록 폼의 안내를 시각 확인했다. 별도 로컬 HTTP 시험 세션에서 관리자/매니저의 가이드·목차·필드 도움말·관리자 링크 분리를 확인하고 시험 세션을 로그아웃했다. 실제 프로젝트·계정·원본 파일은 등록·변경하지 않았다. Docker·원격 Actions·AI 호출·커밋·push는 실행하지 않았고, 기존 AI 실행 보호 차단을 해소한 것으로 판단하지 않는다.

## 16. 임시 브랜치 생성과 base 실제 요청·승인·로컬 커밋

2026-09-14, 관리자가 등록 또는 설정 저장 시 입력한 로컬 브랜치가 없으면 `develop → main → master → 현재 브랜치`에서 첫 유효 커밋을 찾아 새 브랜치를 생성하도록 변경했다. 기존 브랜치·현재 체크아웃·staged/dirty/untracked 파일은 변경하지 않는다. 읽기 전용 상태 점검과 실행 기준 조회는 브랜치를 생성하지 않는다. 권한·설정 버전·명령 정책을 먼저 검증하고, ref 충돌·커밋 없는 저장소는 오류로 처리한다. 기존 브랜치를 덮어쓰지 않는 compare-and-create 방식이며 Git ref와 DB가 단일 트랜잭션은 아니라는 한계는 프로젝트 문서에 명시했다.

실제 설정 화면에서 Branch만 변경했을 때 기존 검증 명령 JSON이 문자열로 이중 직렬화되어 저장이 실패하는 오류도 재현했다. JSONField에 배열 초기값을 전달하도록 수정하고, 화면에 렌더링된 값을 그대로 제출하는 회귀 테스트를 추가했다.

| 확인 | 결과 |
| --- | --- |
| `make check` | 956 passed, 2 skipped, 기존 경고 2개, 117.69초 |
| Chromium E2E | 확인 모달·결과 검토 관련 7 passed |
| 정적 확인 | Python 컴파일, Django system check, migration 변경 없음, diff 검사 통과; standalone lint 미구성 |
| 실제 브라우저 | 관리자 Branch 저장 → guest 대화·AI 분석 → 제안 적용·명세 확정 → 실제 수정 → guest 완료 확인 → admin 로컬 승인 |
| 실제 모델·실행 | 질문 분석 `gpt-5.6-luna/low`, 수정 `gpt-6-astra/xhigh`; 분리 작업 사본과 Docker 사용 |
| 등록된 검사 | README 계약 BASELINE 실패 → AFTER 4개 통과; README.md만 변경 |

실제 base 설정은 v3, 기준 브랜치는 `temp/ai-qa-testing`이다. 요청 `1680dfb0-8a95-48a2-803a-28a1388b23e7`, 실행 `c10cb2d7-df61-4eb9-b2dc-0688cfca30f4`, 결과 `139b0fb6-1e66-4aab-9de0-7e4581ab5747`를 생성했다. DB에서 결과 검토 상태 `APPROVED`, 확인자 `guest`, 승인자 `admin`을 확인했다. 작업 사본은 커밋 기준 detached HEAD이므로 모델 보고의 브랜치 미확인과 실제 원본의 명명된 브랜치 생성은 구분한다.

사용자가 GitHub Desktop에서 커밋을 확인하도록 별도로 요청했으므로, 승인된 README 결과와 Git blob hash가 동일한 내용을 원본 base의 임시 브랜치에 적용하고 로컬 커밋했다. 이는 앱의 승인 버튼이 원본을 자동 커밋하는 기능이 아니다.

- 저장소: `/Users/kimchanhyung98/Documents/GitHub/base`
- 체크아웃: `temp/ai-qa-testing`
- 커밋: `5b2eb39c62422fd775c73d4c053c7a89b5bae148` — `docs: add local AI QA testing guide`
- 변경 파일: `README.md` 하나; 승인 결과와 blob `789cd67b0d42c3f7a30c6cf1b81dbfff98e79073` 일치
- `main`·로컬의 `origin/main` 참조는 기존 `5f6d2be5ae00260e0d9519b3240332f5803da643` 그대로이며 작업 트리는 깨끗함
- 원본의 `make help`·`make check` 출력과 diff 검사 확인; base의 `make check`는 echo 예시이므로 실제 테스트·린트 통과로 표현하지 않음
- 앱 상태 새로고침에서 임시 브랜치·새 기준 코드 `5b2eb39c6242`·미커밋 변경 0건 확인
- `github open`으로 GitHub Desktop에 base 열기 요청 성공; 네이티브 UI 도구 연결 실패로 Desktop 화면 직접 판독은 미검증

원격 push·PR 생성·Actions 실행·배포는 하지 않았다. ai-qa의 변경은 커밋하지 않았고 기존 로컬 변경을 보존했다.

## 17. 브랜치 명명 규칙과 hook 확인

2026-09-14, base와 ai-qa의 `.husky/validate-branch.cjs`, `.husky/validate-commit.cjs` 및 hook 연결을 확인했다. 브랜치는 `main/master/develop/staging` 또는 소문자 `type/description`이며 다단계 경로를 허용한다. 커밋은 `type(scope): subject` 또는 `type: subject`이며 영문 subject는 소문자로 시작한다. 브랜치 hook은 pre-push, 커밋 메시지 hook은 commit-msg에서 실행하도록 구성되어 있다.

base의 `core.hooksPath`는 미설정이고 기본 `.git/hooks`에는 sample만 있어 자동 hook은 활성화되지 않은 상태였다. ai-qa는 `.husky/_`로 연결되어 있다. 설치·Git 설정 변경은 하지 않았다. base의 현재 `temp/ai-qa-testing`과 `docs: add local AI QA testing guide`를 검증 스크립트로 직접 확인해 통과했으며, 잘못된 커밋 메시지 대조군도 거부됨을 확인했다. 이는 이전 커밋 시 자동 hook이 실행됐다는 증거가 아니다.

웹 등록·설정 화면이 잘못된 명명 규칙을 받아들이고 HTML pattern이 없는 현상을 테스트로 재현했다. 공통 명명 규칙을 서버 입력 검증과 HTML pattern/title에 적용했다. Git 문법 검사는 유지하며, 등록·설정 서비스 직접 호출도 잘못된 명시적 이름을 거부한다. 기존 기준의 읽기 조회·실행과 빈 입력의 기존 현재 브랜치 선택은 보존한다. 대상 저장소의 임의 hook을 실행하는 기능은 추가하지 않았다.

- `make check`: **974 passed, 2 skipped**, 기존 경고 2개, 132.10초
- 확인 모달 E2E: Chromium·Firefox·WebKit 총 **18 passed**; 등록 1280px·390px 및 설정 화면의 브랜치 검사 포함
- 실제 `127.0.0.1:8000` 관리자 브라우저에서 `Feature/auth`·`my-feature`는 모달 이전 차단, `temp/ai-qa-testing`은 모달 진입 확인 후 취소; 프로젝트 설정·DB·base Git 변경 없음
- Python·JavaScript 문법, Django system check, migration 변경 없음, 문서 링크·diff 검사 통과; standalone lint 미구성
- 사용 가이드·프로젝트 기획 갱신; 새 커밋·push·PR·Actions 실행 없음
