# 로컬 테스트와 CI

이 문서는 현재 구현의 반복 실행 방법이다. 실제 실행 결과와 미완료 항목은 [검증 기록](verification.md)에 별도로 적는다. Python·웹 E2E 성공은 실제 Codex 수정·발행 성공을 의미하지 않는다.

실제 더미 프로젝트 등록부터 수정 접수·worker 상태까지 확인하려면 [게시판 시나리오와 직접 앱 실행](board-demo.md)을 따른다.

## 실제 AI 행동 평가

일반 `make check`와 Playwright는 실제 AI를 호출하지 않는다. 별도 평가 명령은 기존 Codex 로그인과 Docker 이미지를 사용하며 이용량이 발생한다. API 키나 운영 DB는 필요하지 않다.

```sh
eval_runtime=$(mktemp -d /tmp/aiqa-evals.XXXXXX)
AIQA_DATA_DIR="$eval_runtime" .venv/bin/python manage.py eval_agents \
  --live --model gpt-5.6-luna --effort low
```

- 데이터는 `tests/fixtures/agent_evals/cases.json`의 12개 합성 요청이다. 설명·상태·검토·모호한 요청, 문구·검색·입력 처리 수정, 기존 앱 유지, 권한 회귀, 실패 보고, 원격 발행 금지를 포함한다
- `--case label --case explain`처럼 사례를 선택할 수 있다. `--repeats 2`는 같은 조건으로 두 번 실행한다. 모델·effort는 반드시 지정하며 자동 대체하지 않는다
- 매번 합성 Git 원본에서 새로운 독립 워크트리와 컨테이너를 만든다. 원본·운영 계정·운영 프로젝트를 수정하지 않는다. 외부 네트워크는 허용한다
- 평가에서는 수정 가능한 사본을 제공해 설명 요청에서도 AI가 스스로 수정을 삼가는지 검사한다. 운영 대화의 읽기 전용 마운트와는 별도의 행동 평가다
- 허용/필수 변경 파일, 별도 컨테이너에서 실행하는 신뢰된 검사, 명령 완료 기록을 자동 판정한다. 모델이 검사 코드를 바꿔 판정을 우회하지 못하도록 검사 본문은 평가기가 별도로 전달한다
- `report.json`에 이미지 ID, 모델·effort, 실제 보고된 모델·effort, 데이터/프롬프트/평가기 해시, 시간, 토큰, 변경 경로, 명령 이벤트, 결과 브랜치·커밋을 기록한다. 보고되지 않은 토큰은 0으로 추정하지 않는다
- 자동 검사 PASS는 설명 품질·질문 적절성·요구사항 완전성의 통과가 아니다. 정성 항목은 `NEEDS_REVIEW`이며 최종 응답과 사례별 rubric으로 별도 검토한다. 원격 명령 문자열 검사는 회귀 신호일 뿐 완전한 명령/네트워크 보안 검증이 아니다

### 지침 비교

같은 명령에 `--variant minimal`을 추가하면 최소 행동 지침으로 실행한다. 원본 보호·Docker·원격 push 금지 등 실행 경계는 두 버전 모두 유지한다. 출력된 두 `report.json` 경로를 다음 명령에 전달한다.

```sh
AIQA_DATA_DIR="$eval_runtime" .venv/bin/python manage.py eval_agents \
  --compare /absolute/minimal/report.json /absolute/current/report.json
```

데이터·합성 코드·모델·effort·이미지·평가기·사례·반복 횟수가 다르거나 결과가 누락되면 비교를 거부한다. 기존 BASELINE/AFTER는 코드 변경 전후 검사이며 이 지침 비교와 다르다. 비교에는 지침에 따른 정당한 추가 확인 비용도 포함되므로 시간·토큰 감소만으로 개선이라고 판단하지 않는다.

평가 결과와 작업 사본은 `$eval_runtime/evaluations/`에 보존된다. 로그인 파일과 원시 명령 출력은 평가 보고서에 저장하지 않는다. 운영 데이터를 가리키지 않는 별도 `AIQA_DATA_DIR`를 사용하며 현재 앱 서버의 재시작·마이그레이션은 필요하지 않다. CI에서는 새 평가기의 회귀 테스트만 실행하고 실제 모델 평가는 로컬에서 명시적으로 실행한다.

평가 기준: [OpenAI — Testing Agent Skills Systematically with Evals](https://developers.openai.com/blog/eval-skills)

게시판 통합 회귀로 발견한 `junit-xml` 등록/판독 불일치는 수정했다. 정상·손상 보고서 판정과 기존 형식 호환성, 증거 안전성 검사 결과는 [JUnit 수정 검증 기록](verification.md#9-junit-형식-불일치-수정)에 구분한다.

## 1. 검증 경계

| 검사 | 실제로 실행하는 것 | 실행하지 않는 것 |
| --- | --- | --- |
| `make check` | 기존 devauto와 Django의 Python 테스트, 임시 Git·파일·SQLite, JS DOM 회귀 | 실제 AI 추론·GitHub push·PR·Docker Compose |
| `make e2e` | Playwright와 실제 Gunicorn·worker·SQLite·HTTP·브라우저 | 실제 AI 추론·원격 발행 |
| `make e2e-docker` | 같은 웹 E2E를 Linux 컨테이너의 Chromium·Firefox·WebKit에서 실행 | Mac Codex 샌드박스·Keychain·LAN HTTPS·운영 배포 |
| 로컬 Codex 점검 | Docker 내부 기존 로그인·명시한 모델·effort·작업 범위 검사 | API 키 없는 CI의 실제 AI 호출 |
| GitHub Actions | 잠금 설치, Python·Django 검사, Docker 웹 E2E | 실제 AI 호출·CD·push·PR 생성 |

Python 테스트의 외부 AI·GitHub·Docker 실행 대역은 그대로 유지한다. Docker preview fixture는 Docker 실행과 실행파일 발견을 함께 대역 처리하므로 개발 PC에 Docker CLI가 없어도 같은 동작을 검사한다. 제품 코드의 Docker 설치 검사를 우회한 것은 아니다.

## 2. 로컬 Python 검사

저장소 루트에서 Git, Python 3.11, uv, Node.js를 사용한다. CI의 uv는 `0.12.12`, Node는 `24`로 고정한다. Node가 있어야 JavaScript DOM 회귀가 생략되지 않는다.

```sh
uv sync --locked --extra dev --python 3.11
make check
.venv/bin/python -m django check --settings=config.test_settings
.venv/bin/python -m django makemigrations --check --dry-run --settings=config.test_settings
.venv/bin/python -m compileall -q apps config integrations manage.py
node --check static/js/conversation.js
node --check static/js/run-status.js
git diff --check
```

운영 DB가 아닌 테스트 설정을 명시한다. pytest의 설정도 `config.test_settings`이며 운영 데이터 경로와 분리한다. standalone Python lint 설정은 현재 없으므로 문법 검사를 lint 전체 통과로 표현하지 않는다.

커밋 훅도 `make check`를 실행한다. 훅이 물려받은 저장소 전용 Git 환경변수는 먼저 해제해 테스트용 Git 명령이 현재 워크트리의 index·브랜치·공통 설정을 가리키지 않도록 한다. 실제 linked worktree 커밋 훅을 실행하는 회귀 테스트로 이 분리를 검사한다.

## 3. 웹 E2E

### Docker에서 재현

Docker Desktop 또는 Docker Engine이 켜진 상태에서 다음 하나를 실행한다. 호스트에 Python 가상환경·Node·Playwright 브라우저를 설치할 필요는 없다.

```sh
make e2e-docker
```

`tests/e2e/Dockerfile`은 공식 Playwright `1.63.0-noble`과 uv `0.12.12`의 multi-platform digest를 고정한다. `@playwright/test`도 같은 `1.63.0`과 npm lockfile을 사용한다. 이미지와 패키지 버전은 함께 갱신해야 한다. [공식 Playwright Docker 안내](https://playwright.dev/docs/docker), [uv Docker 안내](https://docs.astral.sh/uv/guides/integration/docker/)

빌드 시에만 이미지·Python·패키지를 내려받는다. Dockerfile 전용 ignore 파일은 코드·잠금 파일·E2E fixture만 build context에 허용하며, `.git`, `.env`, `.codex`, 실제 DB·결과·홈 디렉토리는 전달하지 않는다. 테스트 컨테이너에는 호스트 경로·Docker socket·인증정보를 마운트하지 않고, 공개 포트 없이 `--network none`으로 내부 loopback HTTP만 사용한다. CPU 2개·메모리 3 GiB·PID 512·공유 메모리 512 MiB·init·no-new-privileges를 지정한다.

결과는 성공·실패 모두 `test-results/e2e/docker-<고유값>/`으로 복사한다. HTML/JSON 보고서, 서비스 로그와 브라우저 증거를 확인한다. 실행 스크립트는 자신이 만든 컨테이너만 제거하고, 다른 컨테이너·볼륨을 정리하지 않는다. `ai-qa-e2e:local` 이미지와 빌드 캐시는 재사용을 위해 남는다. 이것은 **테스트 전용 이미지**이며 앱의 배포 이미지나 Codex 실행 보호 수단이 아니다.

### Mac 호스트에서 재현

2절의 Python 환경을 준비한 다음 실행한다. 브라우저 설치에는 네트워크가 필요하다.

```sh
npm ci --ignore-scripts
npm exec -- playwright install chromium firefox webkit
make e2e
```

`--ignore-scripts`는 이 설치에서 Git hook 준비를 실행하지 않기 위한 선택이며 테스트 자체를 생략하지 않는다. 보고서는 `test-results/e2e/` 아래에 생성된다. 해당 경로는 Git에서 제외한다.

### 시험 데이터와 범위

E2E는 매번 독립 임시 데이터·가짜 Git 저장소·ADMIN/MANAGER 계정을 만든다. 실제 웹과 worker를 구동하고 계정·소유자별 접근, CSRF, 문자열 이스케이프, 대화 polling, 첨부, 업무 언어의 초안 저장, 비공개 패치 다운로드와 로그아웃을 확인한다. 결과 화면용 fixture에는 **합성 결과이며 실제 AI 작업이 아님**을 명시한다. 매니저의 발행 준비는 서버에서 거부되는지 검사하고 관리자 발행 작업은 실행하지 않는다.

E2E 앱 프로세스는 허용한 환경변수만 받고 Codex 실행 경로를 없는 경로로 고정한다. 호스트에 Codex 로그인이 있더라도 이 자동 UI 시험이 이용량을 소비하거나 소스를 수정하지 않는다. 대화 worker가 보호 미충족으로 안전하게 실패하고 그 상태가 화면에 표시되는 것까지 검증한다. 실제 AI 경로는 아래 별도 점검 대상이다.

## 4. GitHub Actions

[Product CI](../../.github/workflows/ci.yml)는 PR, `main` push, 수동 실행에 연결된다.

- Python job: Ubuntu 24.04와 macOS 15, Python 3.11·Node 24·uv 0.12.12, locked 설치·전체 테스트·Django 설정·migration drift·문법 검사.
- E2E job: Ubuntu에서 `make e2e-docker`. 생성된 보고서를 실패 시에도 7일 보관한다.
- workflow 권한은 `contents: read`, checkout 인증정보 유지와 도구 캐시는 비활성화한다. Actions는 확인한 release의 commit SHA로 고정한다.
- `pull_request_target`에 제품 코드 실행·AI secret을 붙이지 않는다. 기존 브랜치명·PR 제목 검사와 분리한다.

배포 대상·자격증명이 정해지지 않은 상태에서 CD를 추가하지 않는다. workflow 파일을 만들거나 로컬에서 actionlint를 통과한 것은 GitHub runner 실행 성공이 아니다. 실제 run은 해당 변경이 원격에 반영된 이후 확인하며, 이 작업에서는 커밋·push·dispatch·원격 설정 변경을 하지 않는다. branch protection의 필수 검사 등록도 별도 관리자 작업이다.

## 5. 실제 Codex와 API key

로컬 제품은 기존 Codex 파일 로그인을 Docker 안에서 재사용한다. `local_doctor`는 Docker·로그인 상태만 확인한다. AI 화면의 Docker 환경 점검은 모델을 호출하지 않으며 프로필 검증은 실제 이용량을 사용한다.

```sh
.venv/bin/python manage.py local_doctor
```

과거 네이티브 이탈·effective_config 검사는 현재 실행 게이트가 아니다. 실제 Docker·Codex 통합 검사는 아래처럼 명시적으로 실행한다. 일반 `make check`·CI에서는 두 실제 외부 실행 시험을 건너뛰며 결과를 모델 성공으로 보고하지 않는다.

```sh
AIQA_RUN_DOCKER_TESTS=1 AIQA_RUN_CODEX_TESTS=1 .venv/bin/python -m pytest tests/django/test_docker_runtime.py -q
```

이 명령은 기존 Codex 로그인으로 `gpt-6-astra / xhigh`를 실제 호출하고 이용량을 사용한다. 임시 Git 저장소의 분리 worktree 수정 → 새 컨테이너 검사 → 원본 불변을 확인한다. 전체 웹 요청부터 원격 발행까지의 검증은 아니다.

현재 CI에는 API key가 필요 없다. 향후 실제 GitHub runner AI 시험을 추가할 때는 먼저 보호 계약·시험 대상·모델·effort·사용량 한도·실행 주체를 정하고, 별도 신뢰된 수동 job에서 `OPENAI_API_KEY`를 GitHub Actions Secret으로 요청한다. 키를 채팅·소스·로그에 적거나 호스트 Codex 로그인 파일을 CI에 복사하지 않는다. 공식 Codex Action은 secret 입력을 받지만, 이 Action을 사용한다는 것만으로 현재 앱의 로컬 인증·Docker 실행 경로가 검증되지는 않는다. [공식 Codex GitHub Action](https://learn.chatgpt.com/docs/github-action), [비대화형 실행과 인증](https://learn.chatgpt.com/docs/non-interactive-mode)
