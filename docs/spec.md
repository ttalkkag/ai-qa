# devauto 스펙

> 이 문서는 저장소 코드(`devauto/`, `tests/`, `projects/`)를 전수 검토한 결과를 기준으로 작성한 프로젝트 스펙입니다.
> README.md가 "현재 동작 목록"이라면, 이 문서는 **목적·운영 모델·계약·모호했던 사항의 확정**을 다룹니다.
> 기준 시점: 2026-06-11.

## 1. 프로젝트 목적

**devauto는 개발자 한 명의 로컬 머신에서 실행되는 AI QA/버그 수정 자동화 하네스다.**

- AI가 직접 "운영 시스템"이 되는 것이 아니라, 로컬 오케스트레이터가 run / workspace / policy / docker / gate / artifact를 통제하고, AI CLI(Claude Code, Codex 등)는 그 안에서 **역할별로 호출되는 도구**다.
- 같은 LAN의 QA 팀원(비개발자 포함)이 브라우저로 접속해 **요청 등록 → 계획 승인 → 미리보기 확인 → 발행 승인**을 처리할 수 있는 단순한 웹 UI를 제공한다.
- 결과물은 자동으로 운영에 배포되는 것이 아니라 patch / 로컬 commit / 격리 branch push / 승인된 deploy 명령 중 **프로젝트별로 선택한 보수적인 방식**으로만 나간다.

한 줄 요약: *"내 로컬에서, 다른 사람들이 QA와 버그 수정 초안을 편하게 처리하게 하는 도구."*

## 2. 운영 모델과 사용자 역할

| 역할 | 담당 | 필요 지식 |
|------|------|-----------|
| **호스트 운영자 (개발자)** | 서버 실행, `DEVAUTO_SHARED_TOKEN` 설정, 프로젝트 YAML 작성·등록, AI CLI 설치/인증, Docker 환경 준비 | Git, Docker, YAML, AI CLI |
| **QA 팀원 (비개발자 가능)** | 웹 UI에서 run 생성(프로젝트 선택 + 제목/설명 입력), Plan 승인/반려, 미리보기 확인 후 QA 승인/반려, publish 승인/반려 | 브라우저 사용, 토큰 포함 URL |

- 접속 방식: 호스트 운영자가 `0.0.0.0`에 바인딩하면 QA 팀원은 `http://<호스트 LAN IP>:7700?token=...`으로 접속한다. `/settings`가 LAN URL 후보를 자동 감지해 보여준다.
- **프로젝트 등록(YAML 작성)은 비개발자 작업 범위가 아니다.** 개발자가 대상 프로젝트들의 repo 경로·명령·정책을 미리 등록해 두고, QA는 등록된 프로젝트를 드롭다운에서 선택만 한다. (목표였던 "프로젝트 경로 사전 설정 후 선택"은 이 구조로 충족된다.)

## 3. 시스템 구성

```text
QA 브라우저
  ↓ (LAN, shared token)
FastAPI 웹 UI/API  ── devauto/api/main.py (inline HTML, 템플릿 엔진 없음, 한국어 UI)
  ↓
SQLite 상태 저장  ── devauto/core/db.py (projects/runs/artifacts/approvals/gate_results)
  ↓
파이프라인 오케스트레이터 ── devauto/runner/pipeline.py (상태 전이, 승인, 재시도, escalation)
  ├─ Workspace Manager  ── runner/workspace.py (run별 격리 clone + branch)
  ├─ AI CLI Adapter     ── runner/ai_cli.py (planner/plan_reviewer/executor/reviewer 호출)
  ├─ Gate Runner        ── runner/gates.py (deterministic gate, Docker Compose preview)
  ├─ Review Harness     ── runner/reviewer.py (reviewer_a/b/c 집계 → pass/fix/escalate)
  ├─ Publisher          ── runner/publisher.py (4가지 publish 모드)
  └─ Artifact Store     ── runner/artifacts.py (secret redaction, 경로 폐쇄)
별도 worker 프로세스  ── runner/worker.py (`devauto worker`, 큐 polling, stale 복구)
```

- 기술 스택: Python 3.11+, FastAPI, Pydantic, SQLite, subprocess. 프론트엔드 빌드 없음(루트 `package.json`은 husky Git hook 전용).
- CLI(`devauto`): 서버 실행(기본), `worker`, `doctor <yaml>`, `recover-stale --older-than-minutes N`.
- 테스트: `tests/`에 약 202개. `test_pipeline.py`(71개)와 `test_api.py`(41개)가 중심.

## 4. 핵심 개념

### 4.1 Project

YAML로 선언하는 **프로젝트별 실행 계약**. `/projects` UI 또는 `POST /api/projects/yaml`로 등록하면 data root의 `projects/{id}.yaml`로 원문이 보존되고 SQLite에 저장된다. 주요 섹션: `repo`(url/default_branch/branch_prefix), `commands`(gate 명령), `ai`(역할별 CLI command/timeout), `policy`(mode, fix 한도, forbidden/high-risk 경로·명령), `docker`(compose, preview), `publish`(mode, 승인 요구), `workspace`(보존 개수). 상세 검증 규칙은 6장.

### 4.2 Run과 상태 머신

run은 하나의 QA/수정 요청 단위. 18개 상태와 `state_machine.py`의 전이 표로 통제된다.

```text
RECEIVED → QUEUED → PREPARING → (PLAN_REVIEWED) → AWAITING_PLAN_APPROVAL
  → PROVISIONING → EXECUTING ⇄ FIXING → DETERMINISTIC_CHECKS → AI_REVIEWING
  → (AWAITING_QA_APPROVAL) → READY_TO_PUBLISH → (AWAITING_PUBLISH_APPROVAL)
  → PUBLISHING → PUBLISHED
terminal: PUBLISHED / ESCALATED / FAILED / CANCELLED
```

- **동시성: 프로젝트 무관 전역 active run 1개.** active run이 있으면 신규 run은 `QUEUED`로 저장되고, terminal 전이 후 가장 오래된 queued run이 자동 시작된다 (`db.has_active_run()` — 전역 호출).
- 모든 승인/반려/취소/준비는 **atomic claim**(`try_transition_run`: `UPDATE ... WHERE status IN (...)`)으로 중복 side effect를 차단한다.
- 예기치 못한 예외·취소·stale run도 반드시 final report(`11-final-report.md`)와 final trace(`13-run-final.json`)를 남기고 terminal 상태로 닫힌다.

### 4.3 AI 역할

| 역할 | 호출 시점 | 쓰기 권한 | 미설정 시 동작 |
|------|----------|----------|----------------|
| `planner` | PREPARING | 없음 (위반 시 ESCALATED) | 보수적 fallback Plan Doc 생성 |
| `plan_reviewer` (선택) | Plan 승인 전 | 없음 (위반 시 ESCALATED) | 건너뜀 |
| `executor` | EXECUTING / FIXING | execution grant 범위 내 | skip 처리 |
| `reviewer_a/b/c` | AI_REVIEWING | 없음 (위반 시 ESCALATED) | deterministic fallback으로 pass |

- 프롬프트는 `ai_cli.py`/`reviewer.py`/`pipeline.py`에 **inline 문자열로 정의**되어 있다 (GOAL.md가 제안한 `prompts/`, `templates/` 디렉터리는 채택되지 않음).
- reviewer 출력 계약은 `DECISION: pass|fix|escalate` 한 줄. **judge는 별도 AI가 아니라** `ReviewHarness`의 집계 로직이다(escalate > fix > pass 우선). 결과는 `09-judge.md`로 기록된다.
- 사용할 reviewer 수는 `review_depth`(1~3)가 결정한다: `REVIEWER_ROLES[:review_depth]`.

### 4.4 위험도 분류 (`classify_change`)

요청 제목+설명과 승인된 Plan Doc의 Candidate Files를 기준으로:

| change_class | review_depth | 조건 |
|---|---|---|
| `high-risk` | 3 | 요청 문구에 HIGH_RISK_WORDS(auth, billing, payment, migration, secret, infra, deploy, delete, remove 등) 포함, 또는 candidate file이 `policy.high_risk_paths` 매칭 |
| `small` | 1 | 요청 문구에 "docs" 또는 "readme" 포함 |
| `standard` | 2 | 그 외 전부 |

- `high-risk`는 프로젝트의 `publish.require_human_approval` 설정과 무관하게 **publish approval을 강제**한다.
- `mode=auto`에서도 자동 승인되는 것은 **small + 유효한 Planner Plan Doc** 뿐이다. 나머지는 `02-auto-policy.md`를 남기고 human approval을 기다린다.

### 4.5 Deterministic Gate

`commands`에 설정된 항목만, 고정 순서로 하네스가 직접 실행한다 (AI의 자체 보고를 신뢰하지 않음):

```text
install → format_check → lint → typecheck → unit_test → build → integration_test → smoke_test
```

- Docker가 설정되면 `docker compose exec -T {service} sh -lc "..."`, 아니면 host shell. 두 경우 모두 sanitized child env.
- 실패 시 executor에게 실패 로그 발췌와 함께 fix를 요청한다. gate fix는 `policy.max_inner_gate_fixes`, reviewer 요청 fix는 `policy.max_outer_ai_fixes`로 **각각** 제한된다. 초과 시 ESCALATED.

### 4.6 Publish 모드

`READY_TO_PUBLISH` 상태에서만 시작 가능. 권장 도입 순서대로:

| 모드 | 동작 | 승인 요구 |
|---|---|---|
| `patch_only` | `10-final.patch` + `12-publish.md` 생성. Git 조작 없음 | 선택 |
| `local_branch` | 격리 workspace 안 run branch(`branch_prefix + run_id`)에만 commit. push 없음 | 선택 |
| `push_branch` | commit 후 `branch_prefix` 아래 새 origin branch로 push. 기존 branch overwrite 거부, target branch 직접 push 금지 | `require_human_approval` 필수 |
| `deploy_command` | 승인 후 격리 workspace에서 deploy 명령 실행. placeholder(`${RUN_ID}` 등)는 shell-quote 후 재검사 | `require_human_approval` 필수 |

### 4.7 QA 미리보기

`docker.enabled: true` + `publish.require_qa_approval: true`일 때:

- run별 preview port는 run_id suffix 해시로 `DEVAUTO_PREVIEW_BASE_PORT`(기본 18080)부터 256개 범위에서 할당.
- `docker compose up -d --build` → `docker compose ps`로 health 확인(`docker-compose-health.log`) → `AWAITING_QA_APPROVAL` 동안 유지 → terminal 시 `down -v --remove-orphans`.
- wildcard bind 시 run에는 브라우저가 열 수 있는 `localhost` URL을 저장하고, LAN 후보 URL을 API/SSE/UI/final report에 함께 노출한다.

### 4.8 Artifact

run별 `data_root/runs/{run_id}/artifacts/`에 번호 체계로 저장. 저장 전 secret redaction(키/값, URL credential, Authorization/Bearer/Cookie, CLI flag) 적용. symlink-free run별 디렉터리 내부 경로만 허용.

```text
00-request.json / 00-doctor.md / 00-context.md
01-planner.log / 01-plan.md / 01-plan-invalid.md / 01-planner-write-violation.md
02-plan-review(-ai).md / 02-auto-policy.md / 02-plan-rejection.md
02-execution-grant.json / 02-scope-violation.md
03-execution.log / 03-fix-{N}.log / 03-review-fix-{N}.log
gate-{name}.log / gate-{name}-{N}.log / docker-compose-{up,health,down}.log
07-diff.patch / 08-{reviewer}.md / 09-judge.md
10-final.patch / 11-final-report.md / 12-publish.md
13-retention.md / 13-run-final.json
```

## 5. 실행 수명주기 (요약)

1. **접수**: UI/API로 run 생성. 제목/설명 정규화, unknown project 404. active run 있으면 `QUEUED`.
2. **준비**: doctor 사전 진단(`00-doctor.md`, 실패 시 ESCALATED) → 격리 workspace clone + run branch → context packet(`00-context.md`, forbidden path 제외, 표준 문서 스니펫 포함) → Planner 실행 → Plan Doc 검증(필수 6개 섹션, malformed면 fallback) → 위험도 분류 → (선택) plan_reviewer.
3. **Plan 승인**: human 또는 auto(small만). 승인 시 `02-execution-grant.json` 생성 — Candidate Files 기반 `allowed_files`, source repo snapshot, 허용 명령 포함. 반려 시 feedback을 넣어 재계획.
4. **실행**: executor가 grant 범위 안에서 수정. diff가 Plan Doc scope를 벗어나면 `02-scope-violation.md` + ESCALATED. workspace 밖 source repo 변경도 ESCALATED.
5. **검증**: deterministic gate → 실패 시 bounded fix 루프 → AI review(`review_depth`만큼 reviewer) → judge 집계.
6. **QA/Publish 승인**: 설정에 따라 `AWAITING_QA_APPROVAL`(preview 확인) → `AWAITING_PUBLISH_APPROVAL` → publish.
7. **종료**: final patch/report/trace 저장, Compose 정리, workspace retention 적용, 다음 queued run 시작.

## 6. 프로젝트 YAML 계약 (검증 규칙 요약)

- `id`: 필수, `[A-Za-z0-9._-]` 1~128자 (URL/파일명 안전).
- `repo.url`: HTTP(S) URL에 credential 포함 금지. 로컬 절대 경로 허용. branch 입력은 safe git ref만(`-` 시작, 공백, `..`, `@{`, `.lock` 금지).
- `commands.*`, `ai.*.command`, `publish.deploy_command`: inline token/password/API key 형태 거부, harness hard-deny 명령 포함 거부. command/role 이름은 safe slug만.
- harness hard-deny(프로젝트 policy로 해제 불가): `sudo`, `docker system prune`, `rm -rf /`, `git commit`, `git push`, `ssh `, `deploy`(Output Layer 제외). shell 공백 변형, git global option 변형도 동일 판정.
- `docker.compose_files`: workspace-relative만(absolute/`~`/`..` 금지). `env_file`은 absolute/`~` 허용(로컬 secret 파일용, 내용은 읽지 않고 `--env-file`로만 전달).
- boolean은 명시값만(`"false"` 문자열 truthy 사고 차단), 숫자는 범위 검증(retry/retention ≥ 0, timeout > 0).
- mode 제약: run mode ∈ {`human-reviewed`, `auto`}, publish mode ∈ {`patch_only`, `local_branch`, `push_branch`, `deploy_command`}.
- 기본값 주요 항목: `default_branch=main`, `branch_prefix=aiqa/`, `forbidden_paths=[".env", ".env.*", "secrets/**"]`, `max_inner_gate_fixes=2`, `max_outer_ai_fixes=2`, `keep_success_runs=10`, `keep_failed_runs=30`, `publish.mode=patch_only`.
- `devauto doctor`(CLI/UI/API)로 등록 전후 계약 검증: repo/branch 접근, git/docker 실행 파일, compose/env 파일 경로, command policy, AI role executable, publish 일관성.

## 7. 웹 UI / API

### 화면 (5개, inline HTML, 전부 한국어)

| 경로 | 내용 |
|---|---|
| `/runs` | run 생성 폼(프로젝트 드롭다운, 제목, 브랜치/모드 override, 설명) + run 목록 테이블 |
| `/runs/{id}` | 메타데이터 전체, 상태별 **판단 컨텍스트 inline preview**(Plan 대기 → Plan Doc/리뷰, QA/publish 대기 → final report/patch), 승인·게이트 이력, 상태별 승인/반려/취소 버튼(코멘트 입력), SSE 자동 갱신 |
| `/runs/{id}/artifacts` | 산출물 목록과 열람 링크 |
| `/projects` | 프로젝트 목록, 간단 생성 폼, YAML import 폼, doctor 링크 |
| `/settings` | bind 주소, QA 웹/LAN URL, preview 설정, data root, 프로젝트별 AI CLI/secret 경로 |

### JSON API

```http
POST/GET /api/projects, POST /api/projects/yaml, GET /api/projects/{id}/doctor
POST/GET /api/runs, GET /api/runs/{id}
POST /api/runs/{id}/approve-plan | reject-plan | approve-qa-preview | reject-qa-preview
POST /api/runs/{id}/approve-publish | reject-publish | cancel
GET /api/runs/{id}/events          # SSE: status, preview URL, artifact/gate/approval 카운트
GET /api/runs/{id}/artifacts/{name}
```

## 8. 보안 모델

- **인증**: 단일 공유 토큰(`DEVAUTO_SHARED_TOKEN`). JSON API는 `X-Devauto-Token` 헤더, HTML/SSE/artifact는 `?token=` 쿼리(예약문자 URL-encode 유지). 미설정 시 인증 없음 — `0.0.0.0` 바인딩 전 설정이 전제.
- **권한 분리**: AI에는 commit/push/deploy 권한이 없다. commit/push/deploy는 Output Layer(Publisher)만 수행하고, 그 전에 반드시 승인 기록이 있어야 한다.
- **격리**: run별 workspace clone, source repo snapshot 비교로 workspace 밖 변경 감지, workspace 경로의 symlink/branch/worktree 검증, child process cwd 검증.
- **secret 최소화**: 모든 child process(AI/gate/Docker/git/deploy)는 TOKEN/SECRET/PASSWORD/API_KEY/COOKIE/CREDENTIAL 류 환경변수가 제거된 sanitized env로 실행. artifact 저장 전 redaction. context file tree에서 forbidden path 제외.
- **다중 사용자 권한 구분은 없다.** 토큰을 가진 모든 사람이 모든 작업(승인 포함)을 할 수 있다. 사내 LAN 신뢰 모델 전제.

## 9. 환경변수와 데이터 레이아웃

| 변수 | 기본값 | 설명 |
|---|---|---|
| `DEVAUTO_HOME` | `.devauto` (repo-local) | 데이터 루트. 머신 단위 운영 시 `~/.devauto` 권장 |
| `DEVAUTO_BIND_HOST` / `DEVAUTO_BIND_PORT` | `127.0.0.1` / `7700` | 서버 바인드 (CLI `--host/--port`가 Settings에도 반영) |
| `DEVAUTO_PREVIEW_HOST` | bind host 따름 | preview URL 호스트 |
| `DEVAUTO_PREVIEW_BASE_PORT` / `DEVAUTO_PREVIEW_PORT_COUNT` | `18080` / `256` | preview port 범위 (시작 시 범위 검증) |
| `DEVAUTO_SHARED_TOKEN` | 없음 | 공유 접근 토큰 |

```text
{data_root}/
  devauto.sqlite3
  projects/{project_id}.yaml          # import 원문 보존
  runs/{run_id}/
    workspace/                        # 격리 clone (retention으로만 삭제)
    artifacts/                        # 00-13 산출물 (영구 보존)
```

## 10. 명확화된 사항 (검토 중 모호했던 점의 확정)

코드 검토로 다음과 같이 확정한다. README/GOAL.md와 어긋나던 부분 포함.

1. **데이터 루트 기본값은 repo-local `.devauto/`다.** GOAL.md의 `~/.devauto`는 권장 운영 형태일 뿐이며, `DEVAUTO_HOME`으로 전환한다.
2. **큐는 전역 직렬화다.** "프로젝트별 1개"가 아니라 머신 전체에서 active run 1개. (DB에는 project 단위 조회 메서드가 있으나 파이프라인/worker/API 모두 전역 버전을 사용한다.)
3. **이 저장소에는 LLM 호출 코드가 없다.** AI는 전적으로 외부 CLI(`claude -p`, `codex exec` 등) subprocess에 위임되며, command 미설정 시 해당 역할은 skip되거나 fallback(보수적 Plan Doc, reviewer pass)으로 처리된다. 따라서 **AI CLI 설치·인증은 호스트 운영자의 사전 준비 사항**이다.
4. **judge는 독립 AI 역할이 아니다.** reviewer 결과의 결정 집계기이며 `09-judge.md`는 하네스가 작성한다. GOAL.md의 judge AI 구상은 단순화되어 흡수됐다.
5. **GOAL.md의 `prompts/`, `templates/`(Jinja2) 디렉터리는 채택되지 않았다.** 프롬프트와 보고서 포맷은 코드 inline f-string이다. 외부화는 필요 시 향후 과제.
6. **`review_depth`는 실사용 값이다.** `classify_change`가 산출(1/2/3)하고, AI review 단계에서 reviewer_a/b/c 중 몇 명을 호출할지 결정한다.
7. **`max_retries`는 표시용 합계다.** 실제 제한은 `max_inner_gate_fixes`(gate fix)와 `max_outer_ai_fixes`(reviewer fix)가 별도로 수행하며, `current_retry`/`max_retries`는 UI 추적용이다.
8. **high-risk run은 publish approval이 강제된다.** README의 "push_branch/deploy_command에서 승인 필수"에 더해, change_class가 high-risk이면 publish mode와 무관하게 승인 대기한다.
9. **plan_reviewer의 `fix` 결정은 escalate처럼 처리된다.** 자동 plan 수정 루프는 없으며, `fix`/`escalate` 모두 execution grant 없이 중단된다. (반려→재계획 루프는 human reject 경로에만 있다.)
10. **Plan Doc의 Candidate Files가 구체 경로 없이 비어 있으면 scope 강제가 적용되지 않는다.** 현재는 의도된 완화 동작이다 — scope 강제를 원하면 Plan Doc에 구체 경로가 나오도록 planner 프롬프트/승인 단계에서 확인해야 한다.
11. **`projects/example-project.yaml`은 그대로 실행 가능한 설정이 아니다.** `repo.url`이 placeholder이고 모든 `ai.*.command`가 빈 배열이다. 구조 검증을 통과하는 템플릿이며, 실사용에는 repo 경로와 AI CLI command를 채워야 한다.
12. **Docker health check는 `docker compose ps` 기반이다.** 컨테이너 기동 여부만 확인하고 애플리케이션 readiness(HTTP 응답 등)는 보장하지 않는다. 실질 검증이 필요하면 `commands.smoke_test`를 설정해야 한다.
13. **루트 `package.json`/`Makefile`은 하네스 기능과 무관하다.** package.json은 husky Git hook 전용이고, `make check`는 stub(echo만)이다. 실제 검증은 `python -m pytest`.
14. **GOAL.md의 Phase 구분(A~E)은 개발 순서 서사일 뿐이다.** 코드상 Phase 전부(harness, Plan 승인, executor+gate, QA preview, publish 4모드)가 단일 파이프라인에 구현되어 있다.

## 11. 알려진 한계와 미해결 사항

당장 동작에는 문제없지만, 목표("비개발자 친화")와 운영 관점에서 결정이 필요한 항목.

### 비개발자 UX 격차
- run 생성/승인 흐름은 비개발자가 쓸 수 있는 수준이나, **mode(`human-reviewed`/`auto`)와 target branch 입력에 대한 설명이 UI에 없다.** 용어 안내 또는 기본값 숨김 처리가 필요하다.
- 토큰을 `?token=...` URL로 수동 관리해야 한다. 북마크 공유로 우회 가능하지만 세션/로그인 형태가 더 안전하고 편하다.
- HTML 폼 제출이 검증 실패 시 일반 500으로 떨어지는 경로가 있다(JSON API는 상세 메시지 제공). 폼 에러 메시지 개선 필요.

### 운영/안전 관련
- 단일 공유 토큰이라 승인 권한 구분(누가 승인했는지 식별)이 없다. approval comment에 이름을 적는 관행으로 보완 중.
- preview health check가 readiness를 보장하지 않으므로 QA가 "빈 화면"을 볼 수 있다 → `smoke_test` 설정 권장.
- source repo isolation guard가 엄격해서(workspace 밖 source repo의 어떤 변경도 ESCALATED) 사람이 같은 머신에서 원본 repo를 건드리면 run이 중단될 수 있다. 운영 수칙으로 안내 필요.
- Candidate Files 추출이 정규식 기반이라 Plan Doc 표기에 따라 scope가 비어버릴 수 있다(→ 10항 10번의 완화 동작과 결합하면 scope 미강제).

### 명시적으로 범위 밖 (GOAL.md 16장과 일치)
- PR 자동 생성, production deploy, 병렬 executor, AI CLI의 Docker sandbox 격리, semantic code index, Slack/Discord 연동, 다국어 UI.

## 12. 구현 현황 요약

| 영역 | 상태 |
|---|---|
| Run 하네스(상태머신, 큐, 복구, trace) | 구현 + 테스트 |
| Plan Doc 생성/검증/승인/반려 재계획 | 구현 + 테스트 |
| Executor + deterministic gate + bounded fix 루프 | 구현 + 테스트 |
| AI review(1~3인) + judge 집계 | 구현 + 테스트 |
| Docker QA preview + QA 승인 | 구현 + 테스트 |
| Publish 4모드 + publish 승인 | 구현 + 테스트 |
| 웹 UI(5화면, SSE) / JSON API / 공유 토큰 | 구현 + 테스트 |
| 보안 가드(hard-deny, redaction, sanitized env, 격리 검증) | 구현 + 테스트 |
| 예시 프로젝트 YAML | 템플릿만 (실사용 값 채워야 함) |
| `make check`, 프롬프트 외부화, 로그인/권한 구분 | 미구현 |
