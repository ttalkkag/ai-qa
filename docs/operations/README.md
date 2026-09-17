# 운영 — 한 대의 Mac에서 사용하는 Django 제품

## 1. 현재 상태와 목표

**2026-09-11 현재 Django 앱과 아래 설치·기동 명령은 구현되어 있다. 다만 실제 Codex 코드 수정까지 즉시 사용할 수 있는 완제품은 아니다.** 임시 데이터 디렉토리에서 잠금 의존성 설치, migration, 최초 관리자 생성, 정적 파일 수집, Gunicorn 기동, ADMIN 로그인과 프로젝트 등록을 확인했다. 구현 파일·자동 검사·실제 외부 실행의 확인 범위는 [검증 기록](verification.md), 남은 완료 조건은 [구현 순서와 검증](implementation.md)을 따른다.

실제 AI 작업은 프로젝트별 독립 Git worktree와 Docker 컨테이너에서 수행한다. 네트워크는 허용한다. 과거 macOS setsid·setpgid·effective_config 검사는 더 이상 실행 선행 조건이 아니다. Docker 연결·이미지와 기존 Codex 로그인, 명시한 모델·effort를 확인한다. 과거 검사 기록을 PASS로 바꾸지는 않는다.

기존 `devauto` FastAPI·Python CLI 프로토타입도 남아 있다. `make init`은 기존 개발 환경 설치 진입점이며 잠금 설치를 대신하지 않는다. 로컬 앱 설치에는 아래 `uv sync --locked`를 사용한다. `make check`는 기존 프로토타입과 새 Django 검사를 실행하고 `.venv/bin/devauto --help`는 기존 CLI를 확인한다. 검사 통과가 실제 AI 통합 완료를 뜻하지 않으며 프로토타입의 shared token 서버를 매니저용으로 노출하지 않는다.

목표는 Python 3.11 이상, macOS, Django 5.2 LTS, SQLite로 앱을 실행하고 기존 Codex 로그인으로 실제 프로젝트 작업을 끝내는 것이다. 현재 `uv.lock`의 Django는 5.2.17이며 첫 재현 환경은 Python 3.11이다. 업데이트할 때 5.2 계열의 지원 중인 보안 패치를 검토하고 잠금 파일을 함께 갱신한다. Python 호환성과 지원 상태는 [Django 릴리스 문서](https://docs.djangoproject.com/en/5.2/releases/5.2/)와 [공식 지원 버전](https://www.djangoproject.com/download/)을 재확인한다.

## 2. 프로세스 구조

```text
브라우저 ── Django 웹 ── SQLite / 비공개 결과 저장소
                         │
                   process_jobs (같은 Django 프로젝트)
                         ├─ 대화 슬롯 1: Codex 읽기 중심 호출
                         └─ 실행 슬롯 1: 수정 → 검증 또는 승인된 발행
```

웹은 인증·입력 검증·작업 등록·상태 조회·결과 표시를 담당한다. `process_jobs`는 동일 설정과 DB를 사용하는 Django 관리 명령이다. 별도 서비스 API·서버 주소·러너 토큰을 만들지 않는다. 프로세스 수명만 분리해 브라우저 종료나 HTTP timeout이 코드 작업을 죽이지 않게 한다.

대화는 기존 작업의 상태를 바꾸지 않고 실행 슬롯과 병행할 수 있다. 실제 수정·검증·발행은 프로젝트 전체에서 한 번에 하나다. 상태 갱신은 활성 화면 2초, 백그라운드 10초 polling을 기본값으로 한다. WebSocket, Channels, Redis, Celery는 필요하지 않다.

## 3. 현재 코드 구조

아래는 구현된 주요 파일이다. 테스트·migration 세부 파일·루트 AI 설정·임시 문서는 생략했다. 파일이 존재한다는 사실과 실제 AI 환경에서의 동작 완료를 구분한다.

```text
manage.py
config/
  runtime.py                  # 앱 전용 데이터 루트·비밀·설정·maintenance lock
  settings.py                 # 로컬 기본값과 명시적 환경 설정
  gunicorn.py                 # 동일 설정에서 bind·workers·threads를 읽는 서버 설정
  middleware.py               # Host·loopback·프록시·보안 헤더
  urls.py
  wsgi.py
apps/
  navigation.py               # 권한 확인된 화면 문맥으로 현재 경로 구성
  portal/                     # Admin 허브·로그인 사용자용 Docs 정적 SSR; 별도 DB 없음
  accounts/                   # 계정·역할·멤버십·접근 판정
  projects/                   # 로컬 프로젝트 설정·검증 명령
  conversations/              # 대화·메시지·대화 작업
  work_requests/              # 명세·개정본·제출
  runs/
    models.py                 # Run·Job·worker 상태
    services.py               # 제출·취소·상태 전이
    worker.py                 # claim·lease·복구·슬롯 조정
    processes.py              # 프로세스 식별·자손 추적·취소
    pipeline.py               # 준비 → 수정 → 검사 → 결과 수집
    workspace.py              # 원본 보존·독립 작업 사본
    source_guard.py           # 원본 저장소 변경 여부 대조
    verification.py           # 기준/변경 후 검사
    management/commands/process_jobs.py
  ai/                         # 프로필·호출 기록·문맥 정책
  artifacts/                  # 불변 결과·다운로드
  approvals/                  # 승인·발행·원격 대조
    transport.py              # 신뢰된 Git/gh 발행 명령
  operations/
    doctor.py                 # 읽기 중심 운영 상태 점검
    backup.py                 # 일관된 백업·새 경로 복원
    management/commands/      # local_doctor·backup_local·restore_local
integrations/
  codex/
    adapter.py                # CLI 연결·argv·stdin·이벤트 파싱
    container.py              # Docker 실행·작업 마운트·기존 Codex 로그인
    Dockerfile                # Codex·Python·uv·Playwright 실행 이미지
    permissions.py            # 경로 범위 자료형·이전 네이티브 검사 호환 코드
templates/
  base.html                   # Header → Navigation → Main → Footer 조합
  includes/
    header.html               # 로고·역할별 메뉴
    navigation.html           # 현재 화면·클릭 가능한 경로
    footer.html               # 작은 저작권·프로필 링크
  portal/
    admin.html                # 승인·계정·운영 진입점
    docs.html                 # 매니저용 한국어 사용 가이드
  accounts/ projects/ conversations/ work_requests/ runs/
  ai/ artifacts/ approvals/ operations/
static/
  css/app.css
  js/conversation.js
  js/run-status.js
```

각 Django 앱은 필요한 `models.py`, `forms.py`, `views.py`, `urls.py`, `services.py`, `migrations/`를 가진다. 정적 페이지와 링크를 제공하는 `portal`에는 업무 모델·마이그레이션을 추가하지 않는다. 인증·세션·CSRF·ORM·템플릿 이스케이프는 Django 기능을 사용한다. 매니저 화면은 서버 렌더링이며 필요한 갱신만 소량의 JavaScript로 처리한다. `/admin/`은 제품의 ADMIN 전용 허브이며 Django Admin 사이트는 등록하지 않았다. `is_staff`·`is_superuser`로 앱 역할 검사나 결과 승인 서비스를 우회할 수 없다.

Header는 로고·상위 메뉴, Navigation은 현재 화면의 경로, Main은 페이지 본문, Footer는 저작권·외부 프로필 링크로 구분한다. 헤더·네비는 가까이 배치하되 흰색 헤더와 밝은 회색 네비 배경을 구분하고 본문·푸터와 별도 영역으로 유지한다. 메뉴·URL·활성 표시·공통 디자인과 제품 `Docs`의 기준은 [내비게이션](../navigation/README.md)이 소유한다. 저장소의 `docs/`는 개발 기획서이며 `/docs/`가 이를 파일 경로로 읽어 공개하지 않는다.

뷰가 subprocess를 직접 호출하지 않는다. `work_requests`가 확정 명세를 만들고 `runs`에 등록하며, worker가 `ai`와 `integrations/codex`를 사용한다. `artifacts`가 고정한 결과만 `approvals`에 전달한다. 다른 도메인의 상태를 임의 ORM 업데이트로 바꾸지 않고 소유 서비스로 요청한다. 범용 `BaseRepository`, 이벤트 버스, provider registry, cloud/local adapter 계층은 만들지 않는다.

### Docker 실행 이미지 준비

Docker Desktop을 실행한 뒤 저장소 루트에서 최초 한 번 빌드한다. Docker가 없거나 이미지가 없으면 오류를 표시하며 호스트 실행으로 대체하지 않는다.

```sh
docker build -t aiqa-runner:local -f integrations/codex/Dockerfile integrations/codex
```

기본 이미지는 `aiqa-runner:local`이며 `AIQA_DOCKER_IMAGE`로 변경할 수 있다. 앱 웹과 worker를 같은 설정으로 재시작한다. Codex 호출에는 호스트의 기존 파일 로그인을 사용하며 별도 API 키를 요구하지 않는다. 컨테이너 로그인 범위와 제한은 [AI](../ai/README.md)를 따른다.

## 4. 로컬 데이터와 설정

기본 데이터 루트는 `~/Library/Application Support/AI QA`이고 `AIQA_DATA_DIR` 환경변수로 명시적으로 바꿀 수 있다. 앱 소스 밖의 전용 절대 경로여야 하며 프로젝트 등록·사본 생성에서도 원본 저장소와 중첩을 거부한다. 최초 생성은 없는 디렉토리, 빈 비공개 디렉토리 또는 허용된 `settings.json`만 있는 비공개 디렉토리에서 시작한다. 다른 자료가 든 임의 디렉토리를 채택하거나 권한을 일괄 변경하지 않는다. `~` 경로는 기본 위치 설명이며 `$HOME`을 재할당하라는 뜻이 아니다.

```text
AIQA_DATA_DIR/
  .aiqa-runtime               # aiqa-local-v1 앱 전용 표식, 0600
  settings.json               # 앱 설정, 관리자만 읽기/쓰기
  secret-key                  # 최초 생성 Django secret, 0600
  instance-id                 # 현재 실행 인스턴스 UUID, 0600
  proxy-token                 # lan_https에서 생성하는 프록시 확인 비밀, 0600
  db.sqlite3
  db.sqlite3-wal / db.sqlite3-shm  # SQLite가 운영 중 생성할 수 있는 파일
  workspaces/<run_uuid>/       # Codex 수정 사본
  baseline-checks/<run_uuid>/ # 변경 전 검사 전용 사본
  after-checks/<run_uuid>/    # 고정한 후보 source의 변경 후 검사
  conversations-tmp/<invocation_uuid>/ # 읽기용 문맥·모델 시험 사본
  dependencies/<bundle_uuid>/ # 관리자 등록 불변 의존성 자료
  uploads/<artifact_uuid>/content # 비공개 대화 첨부
  artifacts/<run_uuid>/<version>/ # 불변 결과·마스킹된 증거
  staging/<run_uuid>/baseline|candidate/ # 신뢰된 기준·후보 코드
  publishing/                # 신뢰된 발행 임시 저장소
  codex-config/              # 소스 설정과 분리된 앱 전용 CLI 구성 cwd
  canary-<generated>/         # 가짜 자료 기반 점검 중 임시 생성
  logs/                      # 마스킹된 운영 로그
  locks/maintenance.lock     # 웹·worker 공유 / 백업·복원 배타 잠금
  locks/configuration.lock   # 동시에 살아 있는 프로세스의 설정 hash 대조
  worker.lock                # worker 중복 실행 차단
  static/                    # collectstatic 출력
  backups/backup-<uuid>/      # manifest.json과 files/를 가진 로컬 백업
  restore-incomplete.json    # 복원 중/실패 표식; 존재하면 기동 거부
  restore-report.json        # 성공한 복원의 출처·재점검 항목
```

표식·secret은 최초에 배타적으로 생성하고 재기동 때 재사용한다. 디렉토리는 0700, 비밀·DB 파일은 0600을 기본으로 한다. 기존 파일의 권한이 과도하게 열려 있으면 오류를 해결한 뒤 재기동해야 한다. 디스크 권한은 같은 OS 사용자 아래 실행 도구를 격리하는 장치가 아니며 [AI 실행 권한](../ai/README.md)을 별도로 적용한다. 디스크 용량이 2 GiB 미만이면 신규 실행을 막고 기존 결과는 보존한다.

| 설정 | 기본값/규칙 |
| --- | --- |
| `AIQA_DATA_DIR` | 위 사용자별 경로; 웹/worker가 동일 경로 사용 |
| `AIQA_BIND` | `127.0.0.1:8000`; wildcard 자동 전환 금지 |
| `AIQA_PUBLIC_ORIGIN` | `http://127.0.0.1:8000`; LAN에서는 관리자가 설정한 정확한 HTTPS origin |
| `AIQA_ACCESS_MODE` | `loopback`; LAN은 명시적 `lan_https`와 보안 점검 필요 |
| `AIQA_CODEX_BIN` | 시작 시 탐지·고정한 Codex 절대 경로; 관리자 변경 뒤 재점검 |
| `AIQA_TRUST_PROXY` | 기본 false; `lan_https`는 명시적 true 필요 |
| `AIQA_DEBUG` | 기본 false; Django DEBUG 값, 개발용 명시 설정에서만 true |
| Django secret | 최초 초기화에 생성, 재기동 때 재생성 금지; 사용자/로그에 노출 금지 |
| SQLite | WAL, busy timeout 5초, 짧은 쓰기 트랜잭션 |

`settings.json`은 선택 사항이며 최대 64 KiB의 JSON 객체와 0600 권한을 요구한다. 허용 키는 `AIQA_BIND`, `AIQA_PUBLIC_ORIGIN`, `AIQA_ACCESS_MODE`, `AIQA_CODEX_BIN`, `AIQA_TRUST_PROXY`, `AIQA_DEBUG` 여섯 개뿐이다. `AIQA_DATA_DIR`, 모델 프로필, 임의 Django 설정, Git/gh/Codex 토큰은 넣지 않는다. 모르는 키는 시작 오류이며 모델 프로필은 DB와 AI 연결 화면에서 관리한다. 두 boolean 설정의 환경변수 표기는 `true`/`false` 또는 `1`/`0`이다. DB transaction을 subprocess 완료 때까지 유지하지 않으며 SQLite row lock을 가정하지 않고 조건부 UPDATE와 unique constraint로 claim·중복 제출을 제어한다.

데이터 루트는 `AIQA_DATA_DIR` 또는 OS 기본 경로로 먼저 정하고 그 안의 설정을 읽는다. settings.json이 자신의 데이터 루트를 재지정할 수는 없다. 나머지 값의 우선순위는 제품 기본값 → settings.json → 명시적 환경변수다. 해석된 설정 hash를 AI 보호 점검에 결속하고, 웹·worker·일반 관리 명령은 `configuration.lock`의 hash와 비교하여 동시에 살아 있는 프로세스끼리 설정이 다르면 기동을 거부한다. 동일한 데이터 경로·환경변수로 시작하고 설정을 바꾸면 모두 중지·재기동해야 하며, 살아 있는 공유 프로세스가 없을 때만 새 hash를 기록한다. 웹·worker는 수명 동안 공유 maintenance lock을, 백업·복원은 배타 lock을 잡아 동시에 쓸 수 없게 한다.

최초 `migrate` 전에 공통 초기화가 데이터 디렉토리·앱 표식·secret·instance 파일을 준비한다. 기존 운영 경로의 표식·비밀 파일을 지워 재초기화하는 것을 복구 수단으로 사용하지 않는다. 실제 DB·프로젝트·모델 레코드는 ORM이 소유하고 `settings.json`에 중복 저장하지 않는다.

## 5. 최초 설치와 로컬 웹 실행

아래 명령은 현재 구현된 설치·웹 기동 절차다. 설치와 관리자 화면 검증은 실제 AI 실행 보호 통과나 운영 수용 기준 전체의 완료를 의미하지 않는다. 실제 실행·미실행 항목은 [검증 기록](verification.md)에서 별도로 관리한다.

### 5.1 환경 준비

관리자 Mac에 Git, uv, 지원 중인 Python 3.11 이상이 필요하다. 초기 재현 절차의 Python은 3.11로 고정한다. Codex 기능을 점검하려면 호스트의 기존 Codex CLI·로그인이 필요하고, GitHub 발행을 쓸 때만 기존 로그인된 `gh`와 원격 쓰기 권한을 확인한다. Django·Gunicorn·WhiteNoise·Pillow·psutil·defusedxml과 개발 검사 의존성은 `uv.lock`으로 고정한다. 설치 때 잠금을 새로 계산하지 않고 일치 여부를 검사하는 [uv locked sync](https://docs.astral.sh/uv/concepts/projects/sync/)를 사용한다. 앱 설치에 Node 빌드를 요구하지 않는다.

```sh
uv sync --locked --extra dev --python 3.11
.venv/bin/python manage.py migrate
.venv/bin/python manage.py bootstrap_admin
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py local_doctor
```

기본 데이터 경로를 쓰면 별도 환경변수 입력이 필요 없다. 다른 전용 경로를 사용할 때는 위 명령과 두 서버 터미널 모두에 동일한 `AIQA_DATA_DIR` 절대 경로를 설정한다. `bootstrap_admin`은 username과 두 번의 숨김 password 입력으로 첫 ADMIN을 만든다. username을 미리 정할 때는 `--username`, 선택 email은 `--email` 인자로 전달하며 비밀번호 인자·환경변수는 받지 않는다. 이미 계정이 하나라도 있으면 재실행으로 승격·비밀번호 교체하지 않는다. 상세 계약은 [계정](../accounts/README.md)을 따른다.

`local_doctor`는 설치되지 않은 CLI·미로그인·DB/권한 문제를 FAIL로 종료하고 worker 미기동·미검증 프로필 등은 WARN/NOT_RUN으로 표시한다. exit code 0만으로 AI 준비 완료라고 판단하지 말고 모든 항목을 읽는다.

### 5.2 웹과 worker 시작

두 터미널을 같은 소스·가상환경·데이터 경로로 연다. 기본 loopback 기동 계약은 다음과 같다.

```sh
# 터미널 1: DEBUG=false, 정적 파일은 WhiteNoise
.venv/bin/gunicorn --config python:config.gunicorn config.wsgi:application
```

```sh
# 터미널 2: 단일 worker, 내부 대화/실행 슬롯 관리
.venv/bin/python manage.py process_jobs
```

개발 중에만 `.venv/bin/python manage.py runserver 127.0.0.1:8000`으로 화면을 확인할 수 있다. 일상 운영·LAN 공개에 runserver를 사용하지 않는다. Gunicorn 설정은 앱과 같은 `AIQA_BIND`를 읽고 workers=1, threads=4, timeout=60초를 기본으로 한다. 기동 절차에 별도 bind override를 섞지 않는다. loopback 모드에서는 공유 설정 로더가 loopback 이외 bind를 거부한다.

브라우저에서 `http://127.0.0.1:8000`에 접속한다. AI 화면에서 CLI·로그인 상태와 Docker 환경을 확인한 뒤 conversation·implementation 프로필의 실제 최소 호출을 검증한다. 모델 시험은 이용량을 사용하므로 제출 확인 모달을 거친다. 프로젝트 등록만으로 AI를 호출하지 않는다.

### 5.3 첫 프로젝트 등록

관리자가 로컬 repo 절대 경로, 기준 ref, 편집 허용/금지 경로, 필수 검사 argv·parser를 등록한다. repo가 dirty여도 원본은 그대로 두고 확정한 committed SHA만 작업 입력으로 쓴다. 누락된 `.ai-context`나 docs를 자동 생성하지 않는다.

Python 프로젝트의 준비 명령은 Docker 안에서 `['uv', 'venv', '.venv']`, `['uv', 'pip', 'install', '--python', '.venv/bin/python', '-r', 'requirements.txt']`처럼 등록한다. 검사 예시는 `['.venv/bin/python', '-m', 'pytest', '-q']`, `kind=TEST`, `parser_id=pytest-summary`다. 실제 프로젝트의 lock·requirements 파일에 맞춘다. 호스트의 uv·Python 절대 경로는 입력하지 않는다.

준비 명령·모델 도구·검사에는 외부 네트워크가 허용된다. 다운로드와 설치는 해당 사본의 컨테이너 안에서만 수행하며 baseline·수정·AFTER 환경을 공유하지 않는다. 준비·검사 전후 source hash를 대조하여 검사 중 코드 변경을 성공으로 처리하지 않는다.

오프라인 의존성 묶음은 선택 사항이다. 묶음을 사용한다면 호스트 macOS가 아니라 Docker 이미지의 Linux 플랫폼과 Python 버전에 맞춰 등록한다. 예를 들어 기본 arm64 이미지의 플랫폼은 `linux-aarch64`이며 Python 버전은 아래 명령으로 확인한다. 앱도 복사 전에 이미지의 실제 플랫폼·runtime과 bundle·lock hash를 비교한다.

```sh
docker run --rm aiqa-runner:local python3 --version
docker run --rm aiqa-runner:local uname -m
```

앱 서버의 Python 가상환경과 Docker 작업 환경은 별개다. macOS 전용 명령·SDK는 기본 Linux 이미지에서 실행할 수 없다. 실제 매니저 요청부터 발행까지의 전체 인수 검증과 실제 GitHub 발행은 [검증 기록](verification.md)의 완료 범위를 따르며, 작은 모델 시험만으로 완료됐다고 보지 않는다.

## 6. 다른 기기에서 접속

기본값은 loopback이지만 권한 모델은 처음부터 계정 기반이다. 같은 네트워크의 다른 기기 지원을 켤 때도 관리자 계정에만 승인 기능이 있다. 휴대폰 전용 승인 모듈·기기/IP 신뢰 예외는 만들지 않는다.

LAN 활성화는 관리자가 정확한 HTTPS origin과 접근 주소를 설정하고 신뢰된 인증서를 제공한 경우에만 허용한다. HTTPS reverse proxy가 TLS를 종료하고 Django는 loopback으로만 연결받는 구성을 사용한다. 인증서·프록시 설치를 앱이 자동 수행하거나 공인 터널·공유기를 자동 개방하지 않는다.

현재 설정 계약은 `AIQA_ACCESS_MODE=lan_https`, `AIQA_PUBLIC_ORIGIN=https://<정확한 호스트와 필요 시 포트>`, `AIQA_TRUST_PROXY=true`이며 `AIQA_BIND`는 LAN 주소가 아니라 `127.0.0.1:8000` 같은 loopback을 유지한다. 이 모드에서 최초 생성하는 비공개 `proxy-token` 값을 신뢰된 프록시가 `X-AIQA-Proxy-Token` 헤더에 넣어 Django로 전달해야 한다. 프록시는 클라이언트가 보낸 해당 헤더·forwarded 헤더를 제거/덮어쓰고, `X-Forwarded-Proto: https`와 설정한 origin의 Host를 전달한다. 비밀을 브라우저·공개 설정·접근 로그에 출력하지 않는다. token 없이 forwarded 헤더만 보낸 요청과 비-loopback peer는 거부한다. 프록시·인증서의 실제 LAN 조합은 아직 검증하지 않았다.

`ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS`는 정확한 host/origin만 등록한다. LAN 모드의 세션·CSRF 쿠키는 Secure, 세션은 HttpOnly, SameSite=Lax를 적용한다. 모든 변경은 POST+CSRF로 처리한다. 프록시가 직접 덮어쓴 헤더만 신뢰하며 외부가 보낸 forwarded header로 보안 판정을 바꾸지 않는다. [Django 배포 점검](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/)을 적용하고 `check --deploy`의 경고를 환경별로 검토한다.

loopback HTTP에서는 Secure 쿠키를 끄는 예외가 필요하지만 계정 인증·CSRF·Host/Origin 검사를 끄지 않는다. DNS rebinding·다른 웹사이트의 localhost 요청도 시험한다. LAN 모드에서 HTTPS origin 또는 명시적 프록시 허용 설정이 빠지면 기동을 거부한다. 외부 프록시의 인증서 설치·유효성까지 앱이 자동 점검하는 것은 아니므로 관리자가 공개 전에 확인해야 한다. 관리자가 명시적으로 loopback 모드를 선택하기 전에는 Secure 쿠키·origin을 평문 설정으로 자동 낮추지 않는다. 공개 인터넷 호스팅은 지원 범위가 아니다.

## 7. 상태 점검·장애·복구

`local_doctor`의 기본 동작은 DB 접근, 데이터 경로/권한, 디스크 여유, CLI 도움말/버전/로그인 상태, worker heartbeat, 프로필 설정 검사를 수행하고 `PASS/WARN/FAIL/NOT_RUN`을 구분한다. `.venv/bin/python manage.py local_doctor --json`으로 같은 결과를 JSON으로 볼 수 있다. 모델 추론이나 원격 쓰기는 하지 않는다. ADMIN 전용 `/operations/` 화면은 DB·파일 기록만 조회하고 CLI조차 실행하지 않는다. 헤더 `Admin` → `운영 상태`로 열며 네비게이션은 `Admin / 운영 상태`다. 같은 허브의 `요청 승인`은 사용자 확인된 로컬 결과 승인과 별도 원격 발행 검토, `계정 관리`는 사용자·프로젝트 할당 화면으로 연결한다. 보호 점검과 모델 시험은 별도 헤더 `AI`에서 다루며 현재 활성화 차단 사유는 §1과 검증 기록을 따른다.

| 상황 | 앱 동작 | 관리자 조치 |
| --- | --- | --- |
| worker 미기동/heartbeat 45초 초과 | 신규 작업 시작 불가, 대기/중단 안내 | 동일 데이터 경로로 worker 시작, 복구 판정 확인 |
| 중복 worker 기동 | lock 획득 실패로 새 worker 종료 | 기존 PID와 시작 시각 확인 |
| Mac 절전/재부팅 | 실행을 성공 처리하지 않고 실제 자손·lease 확인 | INTERRUPTED 결과 확인 후 명시적 재시도 |
| 취소 후 로컬 자손 생존 여부 불명 | ABANDONED 기록·기존 작업 보존, 새 실행 허용 | 새 Run·worktree·컨테이너 사용; 원격 발행 결과 불명은 별도 확인 |
| 모델 로그인/한도 오류 | 비밀 없는 오류·관리자 조치 표시 | 호스트에서 로그인/제한 확인, 자동 모델 대체 금지 |
| 의존성 없음 | DEPENDENCY_MISSING, 수정 시작 차단 | 등록된 준비 명령·잠금 의존성 확인 |
| DB 잠금/디스크 부족 | 부작용 전 작업 거부, 저장 실패 경고 | 남은 프로세스/공간 확인, DB 파일 삭제 금지 |
| 원격 응답 모호 | UNKNOWN/PARTIAL 유지 | 원격 읽기 대조로 사실 확인; 무조건 push 재시도 금지 |

worker 종료 신호는 새 작업 claim을 중단하고 추적한 작업에 취소를 전파한다. Docker 컨테이너 제거를 시도하며, 종료를 확인할 수 없는 로컬 작업은 ABANDONED로 보존하고 슬롯을 해제한다. 기존 파일을 삭제하거나 성공 결과로 쓰지 않는다. 재시도는 새 환경을 사용한다. 원격 발행 결과가 불명확하면 중복 발행을 막는 별도 확인은 유지한다.

관리자 비밀번호 분실은 호스트 터미널의 `manage.py changepassword <username>`으로 기존 계정의 비밀번호만 변경하고 기존 세션을 무효화한다. 계정 정지/마지막 관리자 훼손은 감사 기록과 백업을 확인한 관리 복구 절차로 처리하며 공개 HTTP 복구 endpoint나 기기 기반 관리자 우회를 만들지 않는다.

## 8. 보관과 백업

[결과물](../artifacts/README.md)의 보관 계약은 결과 30일, 마스킹된 로그 7일, 종료된 workspace 7일이다. 승인 대기·원격 결과 불명·발행 복구에 필요한 기록은 pin하여 삭제하지 않는다. 감사 이벤트의 보관 계약은 기본 90일이며 actor·대상·동작·결과·시각만 남기고 비밀·원문 프롬프트를 운영 로그에 넣지 않는다.

대화·요청·실행 메타데이터는 마지막 활동/실행 종료 후 90일 보관하는 것이 목표다. 대기·실행·승인/발행 미해결 상태와 참조 중인 기록을 함께 pin하고, 보관 기간이 다른 기록 간 참조를 임의로 끊어 과거 승인이 다시 유효해지게 하지 않는 계약을 유지한다. 기한이 지난 첨부를 요구하는 재시도는 새 자료·명세 확인이 필요하다.

현재 구현된 `artifacts.services.expire_results()`는 파일 만료·pin 판정 서비스이며 자동 스케줄러나 운영 정리 명령에 연결되지 않았다. 90일 메타데이터/감사 자동 정리, workspace 7일 정리, orphan 파일 수집·정리, 관리자 사전 정리 목록은 미구현이다. 실패·취소된 실행에서 진단용 ResultBundle을 일관되게 만드는 failure bundle 경로도 미완료다. 따라서 디스크가 자동 회수된다거나 모든 실패 실행에 다운로드할 결과 묶음이 있다고 안내하지 않는다. 운영 데이터 파일·DB를 직접 삭제해 이 빈틈을 우회하지 않는다.

구현된 `manage.py backup_local`은 **웹·worker·그 자손 중지 → 배타적 maintenance lock 획득 → 기록된 프로세스 종료 읽기 대조 → SQLite backup API와 파일 manifest로 snapshot 생성 → checksum 검사** 순서다. 다른 writer가 lock을 보유하거나 기록된 프로세스가 살아 있거나 식별이 불명확하면 거부한다. 종료를 위해 임의 PID에 신호를 보내지 않는다. worker claim만 멈추고 웹의 첨부 업로드·설정 변경이 계속되는 상태는 허용하지 않으며 live SQLite의 DB 파일 하나만 복사하지 않는다.

`.venv/bin/python manage.py backup_local`의 기본 결과는 현재 데이터 루트의 `backups/backup-<uuid>/`다. `--destination <새 절대 경로>`로 변경할 수 있지만 이미 존재하는 출력 디렉토리는 덮어쓰지 않는다. 압축 파일이 아니라 `manifest.json`과 `files/`를 가진 디렉토리 백업이며 format version, 원본 데이터 경로·instance, 포함 파일의 SHA-256·크기·제외 항목을 기록한다. 심볼릭 링크·하드 링크·특수 파일·경로 이탈·manifest 불일치·DB 참조 파일 누락은 거부한다. 복원에는 이 디렉토리 전체가 필요하다.

백업에는 DB, `.aiqa-runtime`, `instance-id`, Django `secret-key`, 존재하는 `settings.json`·`proxy-token`과 `uploads`·`dependencies`·`artifacts`·`logs`의 일반 파일을 포함한다. 만료 증거·감사 메타데이터도 DB에 포함된다. provider 인증·SSH 키·Keychain, workspaces/baseline-checks/after-checks/conversations-tmp, locks·worker.lock, publishing/staging·canary·codex-config 임시 자료, 수집된 static, backups 자신은 허용 목록 밖이라 복사하지 않는다. 백업 디렉토리/파일은 각각 0700/0600으로 보호한다. 활성화되지 않은 대기 Job도 백업할 수 있지만 복원 때 자동 재개할 수는 없다.

`.venv/bin/python manage.py restore_local <backup_path> --destination <new_empty_data_dir>`도 웹·worker 중지와 원본·목적지의 maintenance lock이 필요하다. 두 인자는 절대 경로이며 목적지는 앱 소스·원본 데이터·백업과 겹치지 않는 새 빈 디렉토리여야 한다. **복원 명령을 실행할 때 `AIQA_DATA_DIR`는 백업 원본 인스턴스 경로를 가리켜야 한다.** 원본 경로·instance가 백업과 일치하고 원본 DB로 이전 프로세스 종료를 확인할 수 있어야 한다. 현재 명령은 원본을 잃었거나 다른 호스트로 옮기는 재해 복구 도구가 아니다.

백업 버전·체크섬·manifest를 검증한 뒤 복원하며 기존 데이터는 덮어쓰지 않는다. 도중에 실패한 새 경로에는 `restore-incomplete.json`이 남고 앱은 그 경로의 기동을 거부한다. 이 표식을 수동 삭제해 활성화하지 말고 원본을 보존한 상태에서 실패 원인을 확인해 다른 빈 경로로 다시 복원한다. 성공하면 표식을 없애고 `restore-report.json`에 출처·재점검 항목을 남긴다. 그 뒤에만 `AIQA_DATA_DIR`를 새 경로로 바꾸고 `collectstatic`을 다시 실행한다.

- 복원본은 새 `instance_id`를 발급하고 모든 세션·기존 승인을 무효화한다. 과거 lease/epoch/PID는 감사 자료일 뿐 새 인스턴스의 실행·프로세스 종료 권한으로 사용하지 않는다.
- 복원된 비종료 Job은 CANCELLED(RESTORED_SNAPSHOT), QUEUED Run은 CANCELLED, 나머지 비종료 Run은 실제 이전 프로세스 종료 확인 뒤 INTERRUPTED로 전환한다. 비종료 AIInvocation도 INTERRUPTED로 남긴다. 이전 완료 기록은 보존하고 새 worker가 옛 대기열을 자동 claim하지 못하게 한다.
- WorkRequest는 DRAFT로 돌리고 draft version을 증가시켜 기존 ready revision을 해제한다. 확정 명세 이력 자체는 보존하며 이전 instance의 Run을 직접 retry하는 요청도 재확인을 요구한다. 모델 프로필·native/file-tool 점검은 미확인으로 되돌리고 프로젝트 readiness도 다시 확인한다.
- 미완료 발행은 UNKNOWN으로 두고 원격 읽기 대조가 끝날 때까지 쓰기를 금지한다. 새 승인 없이 옛 승인 Job을 재등록하지 않는다. 과거 PUBLISHED도 마지막 관측 시각을 보여주며 현재 원격 상태가 그대로라고 단정하지 않는다.
- 원본 repo 경로는 보존하고 앱 내부 dependency 경로만 새 데이터 루트로 바꾼다. 현재 원본 repo 위치·기준 SHA·설정·Codex/gh 연결·권한 보호를 다시 점검한다. 관리자는 필요한 보호 검증을 다시 완료하고 요청 소유자가 명세를 새로 확인·제출한다. 관리자가 타인의 명세 확인을 대신하지 않는다. 현재의 native 활성화 차단은 복원으로 해제되지 않는다.

## 9. 결정과 운영 수용 기준

아래 항목은 유지되는 제품 계약이며 설치·웹 기동·mock/fixture 검사만으로 전체 완료 처리하지 않는다. 특히 OPS-AC-01의 실제 AI 작업, OPS-AC-03의 실제 강제 종료·절전, OPS-AC-04의 실제 HTTPS LAN, OPS-AC-05의 실제 원격 중복 방지는 별도 실증이 필요하다. 최신 확인 범위는 [검증 기록](verification.md)에 둔다.

- **OPS-01**: 단일 Mac·단일 Django 프로젝트, worker는 관리 명령이고 원격 러너 서비스가 아니다.
- **OPS-02**: SQLite와 HTTP polling으로 시작, 별도 큐 서버나 프론트 빌드 시스템을 필수화하지 않는다.
- **OPS-03**: loopback 기본, 명시적 HTTPS LAN 활성화, 계정 권한은 기기에 독립적이다.
- **OPS-04**: 데이터는 소스 밖에 보관하며 provider 인증은 앱 백업·DB로 복제하지 않는다.
- **OPS-AC-01**: 새 데이터 디렉토리에서 문서 절차만으로 관리자 로그인과 실제 첫 프로젝트 작업이 가능하다.
- **OPS-AC-02**: worker 없이 웹은 조회·설정과 원인 안내를 제공하지만 새 작업을 실행 중이라고 표시하지 않는다.
- **OPS-AC-03**: 중복 worker·재부팅·강제 종료 후 중복 코드 수정/발행 없이 복구된다.
- **OPS-AC-04**: LAN 비활성·Host/Origin 위조·CSRF 누락이 부작용 없이 거부되고 LAN 모드는 HTTPS가 필수다.
- **OPS-AC-05**: 실제 백업을 새 데이터 경로로 복원해 결과 무결성·세션 무효화·원격 중복 방지를 확인한다.
- **OPS-AC-06**: 웹 업로드·설정 변경·정리 프로세스가 활성 상태이면 백업을 거부하고, 일관된 백업에는 참조 파일이 빠지지 않는다.
- **OPS-AC-07**: QUEUED 작업이 든 과거 백업을 복원해도 AI 호출·코드 수정·발행이 자동 재실행되지 않고, 이전 PID를 새 프로세스 종료 대상으로 사용하지 않는다.
