# AI QA 자동화

AI를 활용한 QA 및 오류 수정 자동화를 로컬 우선으로 실행하는 하네스입니다.

## 실행

```bash
python -m pip install -e ".[dev]"
DEVAUTO_SHARED_TOKEN=change-me devauto --host 0.0.0.0 --port 7700
```

CLI의 `--host`, `--port` 값은 `/settings`에 반영됩니다. 그래서 실제로 띄운 서버 기준으로 QA 웹 URL과 LAN 접근 상태를 확인할 수 있습니다.

선택적으로 별도 worker 프로세스를 실행할 수 있습니다.

```bash
DEVAUTO_SHARED_TOKEN=change-me devauto worker
```

프로세스 비정상 종료 뒤 중간 상태로 남은 run은 아래 명령으로 복구합니다.

```bash
DEVAUTO_SHARED_TOKEN=change-me devauto recover-stale --older-than-minutes 60
```

런타임 데이터는 기본적으로 이 저장소의 `.devauto/`에 저장됩니다. 머신 단위 설치처럼 쓰려면 `DEVAUTO_HOME=~/.devauto`를 설정하세요.

브라우저에서 엽니다.

```text
http://localhost:7700
```

같은 LAN에 있는 QA 팀원이 접속할 때는 `localhost`가 아니라 이 머신의 LAN IP를 사용해야 합니다.
서버를 `0.0.0.0`에 바인딩하면 `/settings` 페이지에서 감지된 QA LAN URL 후보를 보여줍니다.

`DEVAUTO_SHARED_TOKEN`을 설정했다면 UI는 `?token=...`을 붙여 열고, JSON API는 `X-Devauto-Token` 헤더를 사용하세요.

## 현재 흐름

- 프로젝트를 등록합니다.
- YAML에서 전체 프로젝트 실행 계약을 가져옵니다.
- 가져온 YAML 프로젝트 계약을 symlink가 없는 데이터 루트의 프로젝트 레지스트리에 저장하고 각 프로젝트의 `config_path`를 노출합니다.
- run 요청을 저장하기 전에 제목 공백, 제어 문자 등을 정규화하고 잘못된 입력을 거부합니다.
- 작업공간을 만들기 전에 프로젝트 진단으로 저장소, Docker, deterministic command, AI role command, policy, publish 설정 오류를 잡습니다.
- 로컬 저장소 branch-ref와 Docker 확인을 포함한 진단 하위 검사는 정리된 child environment로 실행합니다.
- project policy가 덮어써져도 push, deploy, sudo, 파괴적 Docker 정리, SSH 같은 harness-level hard-deny 명령은 항상 차단합니다. 명령 비교는 shell 공백 변형도 같은 명령으로 취급합니다.
- 실행을 생성합니다.
- 각 실행에 영구 `session_id`를 부여하고 API 응답, UI, context packet, execution grant, final trace까지 전달합니다.
- 웹 UI에서 대상 브랜치와 실행 mode override를 선택해 실행을 생성할 수 있습니다.
- 알 수 없는 project ID는 JSON과 웹 UI 실행 생성 모두에서 저장 전에 일관되게 거부합니다.
- 서버가 `0.0.0.0`에 바인딩된 경우 `/settings`에서 감지된 QA LAN URL과 미리보기 URL 후보를 확인할 수 있습니다.
- 로컬 runner는 한 번에 하나만 활성화합니다. 추가 실행은 `QUEUED`로 저장됩니다.
- 별도 로컬 프로세스로 `devauto worker`를 실행해 SQLite의 `RECEIVED` 실행과 실행 가능한 `QUEUED` 실행을 처리할 수 있습니다.
- 프로젝트를 가리지 않고 로컬 runner 작업을 직렬화합니다. terminal 상태가 아닌 실행이 있으면 새 실행은 `QUEUED`가 되고, 활성 실행이 terminal 상태가 된 뒤 가장 오래된 queued 실행이 시작됩니다.
- API background task와 worker가 같은 실행을 동시에 준비하지 않도록 준비 전에 atomic claim을 수행합니다.
- plan 승인도 atomic claim으로 처리해 중복 승인 제출이 grant나 실행을 중복 생성하지 못하게 합니다.
- plan 반려도 replanning 전에 atomic claim으로 처리해 동시에 들어온 반려 요청이 rejection trace나 planner rerun을 중복 생성하지 못하게 합니다.
- QA 미리보기와 publish 승인도 continuation side effect 전에 atomic claim으로 처리해 approval, commit, push, deploy, publish artifact가 중복되지 않게 합니다.
- QA 미리보기와 publish 반려도 terminal side effect 전에 atomic claim으로 처리해 rejection trace, cleanup, queued-run 시작이 중복되지 않게 합니다.
- cancel도 atomic claim으로 처리해 cleanup, cancellation report/final trace, queued-run 시작이 중복되지 않게 합니다.
- API background task나 worker의 예기치 못한 예외는 실행을 중간 상태로 방치하지 않고 `11-final-report.md`, `13-run-final.json`을 남긴 terminal `FAILED` 실행으로 표시합니다.
- publish나 QA 승인 continuation 중 예기치 못한 예외도 final trace artifact와 함께 terminal `FAILED` 실행으로 표시합니다.
- 중간 상태로 오래 멈춘 active 실행은 `devauto recover-stale`로 수동 복구하거나, worker에 `devauto worker --recover-stale-minutes N` 옵션을 주어 자동 확인하게 할 수 있습니다.
- 프로젝트를 격리된 실행 작업공간으로 clone합니다.
- 작업공간 준비 Git 명령은 AI, gate, publish 단계와 같은 sanitized child environment로 실행합니다.
- 기존 실행 작업공간 경로가 없거나, symlink이거나, 디렉터리가 아니거나, git worktree가 아니거나, 실행 branch가 아니면 context 수집이나 실행 전에 거부합니다. 잘못된 workspace path는 executor/publish/preview-cleanup에서 사용하지 않고 API/UI/final trace/failure report에도 숨깁니다.
- 각 프로젝트의 retention 설정에 따라 symlink가 없는 data-root 실행 작업공간만 정리하며 산출물과 database row는 보존합니다.
- context와 Plan Doc 산출물을 생성합니다.
- 요청과 terminal 실행 snapshot을 `00-request.json`, `13-run-final.json`으로 저장합니다.
- planner/reviewer prompt를 만들기 전에 context file tree에서 configured forbidden path를 제외합니다.
- `AGENTS.md`, `README.md`, `docs/README.md` 같은 표준 프로젝트 context 파일이 있으면 작은 snippet을 `00-context.md`에 포함합니다.
- 요청 문구와 승인된 Plan Doc candidate file을 기준으로 변경 위험도를 분류하고, high-risk 실행은 프로젝트 publish approval 설정과 무관하게 publish approval을 요구합니다.
- planner가 plan 승인 전에 작업공간 파일을 바꾸면 escalated 상태로 중단합니다.
- 필수 Plan Doc 섹션을 검증합니다. malformed planner output은 `01-plan-invalid.md`에 보존하고 보수적인 fallback `01-plan.md`로 대체합니다.
- 선택적으로 plan 승인 전에 read-only `ai.plan_reviewer`를 실행할 수 있습니다. `fix`/`escalate` decision이나 작업공간 변경은 execution grant 없이 중단됩니다.
- `mode=auto`에서는 small로 분류되고 유효한 Planner Plan Doc만 자동 승인합니다. standard, high-risk, fallback, replan 문서는 `02-auto-policy.md`를 남기고 human approval을 기다립니다.
- executor write access 전에 plan approval을 요구합니다.
- plan 반려 시 `02-plan-rejection.md`를 쓰고 human feedback을 포함해 Plan Doc을 다시 생성합니다.
- plan 승인 뒤 `02-execution-grant.json`을 쓰고 executor/fix prompt에 포함합니다.
- 구체적인 candidate path가 있는 경우 deterministic gate 전에 승인된 Plan Doc `Candidate Files` 범위를 강제합니다. gate-fix와 reviewer-fix 뒤에도 같은 검사를 반복합니다.
- Git porcelain이 quote/escape한 경로도 Plan Doc scope와 forbidden path matching 전에 decode하므로 공백이나 tab이 있는 경로도 제대로 검사합니다.
- API와 UI에서 approval comment를 받고, plan/QA/publish approval 이력을 실행 상세에 표시합니다.
- 실행 상세 페이지에는 요청 설명, 대상 브랜치, mode, change class, review depth, retry count, timestamp, artifact, approval, gate를 표시합니다.
- 알 수 없는 실행 ID는 JSON event, 웹 UI 실행 상세, artifact, action page에서 404로 응답합니다.
- plan 승인 대기 중에는 Plan Doc/review를, QA 또는 publish decision 대기 중에는 final report/patch를 실행 상세에 inline preview로 보여줍니다.
- `/runs/{run_id}/artifacts`에서 plan, log, patch, report 등 실행 산출물을 탐색할 수 있습니다.
- 실행 events stream으로 실행 상세의 status, preview URL, artifact/gate/approval counter를 실시간 갱신합니다.
- local source repo snapshot을 grant에 저장하고 executor, reviewer, gate, publish 단계가 실행 작업공간 밖 source repo를 변경하면 escalated 상태로 중단합니다.
- gate, review, preview, publish로 넘어가기 전 cancellation boundary를 확인합니다. cancelled 실행은 `11-final-report.md`와 `13-run-final.json`을 남기며, 중복 cancel은 idempotent합니다.
- timeout된 AI CLI, gate, Docker, deploy command는 process group 단위로 종료해 harness가 timeout으로 표시한 뒤 child process가 남지 않게 합니다.
- configured deterministic gate를 실행하며, `install`이 설정되어 있으면 format/lint/type/test/build 검사보다 먼저 실행합니다.
- configured reviewer AI role을 실행하고 `08-*` review와 `09-judge.md` artifact를 씁니다.
- reviewer prompt, `08-*` review artifact, `09-judge.md`에 실행/session/change metadata를 포함합니다.
- reviewer AI가 deterministic gate 통과 뒤 작업공간 파일을 바꾸면 escalated 상태로 중단합니다.
- ignored `.env` 파일을 포함한 forbidden path 변경을 차단합니다.
- artifact를 쓰기 전에 흔한 secret key/value, URL credential, CLI flag, Authorization, Bearer, Cookie 형태를 redaction합니다.
- project command, AI role command, deploy command에 inline token/password/API key 형태가 있으면 저장 전에 거부합니다.
- AI role, deterministic gate, deploy command 안의 직접 Git commit/publish 명령을 차단합니다. commit은 Output Layer publish mode에서만 생성됩니다.
- artifact content, failure-report gate log excerpt, API path metadata, final trace path metadata는 symlink가 없는 실행별 data-root artifact directory 내부 path만 제공합니다.
- AI CLI, deterministic gate, Docker Compose, pipeline Git diff/status check, Output Layer git publish command, deploy command는 common token/secret/password/credential/cookie/API key 환경변수를 제거한 sanitized child environment로 실행합니다.
- child process를 시작하기 전에 cwd가 symlink, missing, non-directory이면 실행을 거부합니다.
- publish 전후로 local source repo snapshot을 다시 확인해 approval 대기나 deploy command가 실행 작업공간 밖 source repo를 변경하지 못하게 합니다.
- `deploy_command` 실행 전 `${TASK_TITLE}` 같은 runtime placeholder를 shell-quote하고, rendering된 command를 policy 검사합니다.
- configured `docker.env_file` path는 Docker Compose에 `--env-file`로 전달하되 secret file 내용을 읽거나 artifact에 복사하지 않습니다.
- 설정된 경우 publish 전에 `AWAITING_QA_APPROVAL`에서 멈춰 QA가 미리보기를 승인하거나 반려할 수 있게 합니다.
- 설정된 경우 `AWAITING_PUBLISH_APPROVAL`에서 멈춰 사람이 publish를 승인하거나 escalation report와 함께 반려할 수 있게 합니다.
- gate 통과 뒤 격리 workspace에서 `patch_only` artifact, `local_branch` commit, 승인된 `push_branch`, 승인된 `deploy_command` 중 하나로 publish합니다.
- `/settings`에서 bind address, QA 웹 URL, 미리보기 URL template, LAN access status, 미리보기 port range, 데이터 루트, project AI CLI command, configured secrets path를 확인할 수 있습니다.

## 접근 제어

`0.0.0.0`에 바인딩하기 전에 `DEVAUTO_SHARED_TOKEN`을 설정하세요. 설정된 경우 HTML page, HTML form, artifact link, JSON API route는 모두 token을 요구합니다.
브라우저 link, redirect, run event stream은 token을 URL-encode하므로 예약 문자가 포함된 token도 LAN UI 흐름에서 유지됩니다.

## QA 미리보기

publish 전에 QA 승인을 요구하려면 `docker.enabled: true`와 `publish.require_qa_approval: true`를 설정합니다.

QA 승인 후에는 publish로 이어집니다. QA 반려는 failure report를 작성하고 실행을 `ESCALATED`로 종료합니다.

Docker Compose 미리보기는 실행이 QA를 기다리는 동안 유지됩니다. 실행이 published, escalated, cancelled 상태가 되면 `docker compose down -v --remove-orphans`로 정리합니다. QA preview URL 없이 publish approval을 기다리는 실행은 publish approval 대기 전에 Compose를 정리합니다.

Compose `up`, `exec`, `ps`, `down`에는 설정된 경우 `PREVIEW_PORT`, `HOST_BIND_IP`, `DEVAUTO_ENV_FILE`이 모두 전달되므로 Compose override 파일에서 같은 변수를 일관되게 사용할 수 있습니다.

미리보기 port는 `DEVAUTO_PREVIEW_BASE_PORT`부터 `DEVAUTO_PREVIEW_BASE_PORT + DEVAUTO_PREVIEW_PORT_COUNT - 1` 범위에서 할당됩니다. 기본 범위는 `18080-18335`이며, startup 시 잘못된 server/preview port나 65535를 넘는 preview range는 거부됩니다.
`DEVAUTO_PREVIEW_HOST`가 `0.0.0.0` 같은 wildcard bind 주소이면, 저장되는 실행 미리보기 link는 브라우저가 열 수 있도록 `localhost`를 사용합니다. QA UI가 `0.0.0.0`에 바인딩된 경우 실행 상세, 실행 API response, 실행 event stream, final report, final run trace에도 다른 머신의 팀원이 사용할 수 있는 LAN preview URL 후보가 포함됩니다.

실행이 `AWAITING_QA_APPROVAL`로 들어가기 전 devauto는 `docker compose ps` 결과를 `docker-compose-health.log`로 씁니다. health check가 실패하면 실행을 escalated 상태로 중단하고 미리보기를 정리합니다.

## AI 검토

human approval 전에 Plan Doc을 검토하려면 `ai.plan_reviewer`를 설정합니다. 이 role은 read-only입니다. 작업공간 파일을 변경하면 `02-execution-grant.json` 생성 전에 실행이 escalated 상태가 됩니다. 원본 출력은 `02-plan-review-ai.md`에 저장되고 요약은 `02-plan-review.md`에 저장됩니다.

변경 위험도별 review를 활성화하려면 `ai.reviewer_a`, `ai.reviewer_b`, `ai.reviewer_c`를 설정합니다. Reviewer output에는 아래 한 줄이 포함되어야 합니다.

```text
DECISION: pass
DECISION: fix
DECISION: escalate
```

`fix`는 bounded feedback을 executor에게 전달하고 gate를 다시 실행합니다. `escalate`는 failure report를 씁니다.

Gate failure는 `policy.max_inner_gate_fixes`로 제한됩니다. Reviewer-requested fix는 `policy.max_outer_ai_fixes`로 별도 제한됩니다.

실행 상세는 이미 실행된 자동 gate/reviewer fix 시도 횟수를 `current_retry`로, 전체 configured fix budget을 `max_retries`로 보여줍니다.

각 deterministic gate 시도는 `gate-unit_test.log`, `gate-unit_test-2.log`처럼 별도 log artifact를 가집니다.

Failure report에는 현재 상태, 재시도 횟수, 최신 실패 gate, 실패 log 발췌, 변경 경로, 제안된 다음 단계, patch가 포함됩니다.

준비된 모든 실행은 `00-request.json`을 씁니다. Terminal 실행은 최종 run status와 artifact, approval, gate summary를 담은 `13-run-final.json`을 씁니다.

`GET /api/runs/{run_id}/events`는 status, artifact, gate, approval, preview summary field를 stream하므로 전체 실행 상세 조회 없이도 가볍게 진행 상황을 추적할 수 있습니다. 실행 상세 page는 이 stream으로 진행 상태를 갱신합니다.

## 작업공간 보존

Project YAML에서 `workspace.keep_success_runs`, `workspace.keep_failed_runs`를 설정할 수 있습니다. 실행이 terminal 상태가 된 뒤 devauto는 이 개수를 초과한 오래된 `workspace/` directory만 제거합니다. 실행 artifact, report, SQLite row는 추적성을 위해 보존됩니다.

## 큐

프로젝트와 무관하게 한 번에 하나의 active workflow만 실행합니다. 다른 요청이 들어오면 active 실행이 terminal 상태가 될 때까지 `QUEUED`로 남고, 가장 오래된 queued 실행이 자동으로 준비됩니다.

## 프로젝트 YAML

웹 UI의 `/projects` 또는 `POST /api/projects/yaml`로 repo, Docker, command, AI role, policy, publish mode 같은 프로젝트 설정을 YAML에서 가져올 수 있습니다.

Project ID는 URL과 persisted YAML filename에서 안전하게 쓰이도록 영문자, 숫자, `.`, `_`, `-`만 허용합니다.

Project command name과 AI role name도 gate label, artifact name, UI label로 안전하게 쓰일 수 있는 key 형태만 허용합니다.

`repo`, `policy`, `ai`, `commands`, `docker`, `publish`, `workspace` 같은 project contract section은 mapping이어야 하고, list-valued field는 scalar string이 아니라 YAML list여야 합니다.

HTTP(S) repository URL에는 credential을 포함할 수 없습니다. Project YAML에 token을 저장하지 말고 git credential manager나 SSH agent를 사용하세요.

Project command string, AI role command array, `publish.deploy_command`에는 inline token, password, secret, API key 값을 포함할 수 없습니다. Credential manager, Docker `env_file`, 사전 인증된 CLI를 사용하세요.

Docker Compose file은 workspace-relative path여야 하며 absolute path, `~`, `..`를 사용할 수 없습니다. Relative Docker env file도 같은 규칙을 따릅니다. Absolute 또는 `~` env-file path는 repo 밖 local secret file을 위해 계속 지원됩니다.

Docker runtime identifier는 사용 전에 검증합니다. project name prefix는 lowercase Docker-safe slug여야 하고, preview service는 safe key여야 하며, preview container port는 1-65535 정수, preview host bind 값은 IPv4 주소여야 합니다.

`docker.enabled`, `publish.require_qa_approval`, `publish.require_human_approval` 같은 boolean project setting은 명시적으로 정규화합니다. 애매한 값은 거부합니다.

Numeric project setting은 저장 전에 검증합니다. retry count와 workspace retention은 0 이상의 정수여야 하고, AI/deploy timeout은 양의 정수여야 합니다.

Run mode는 `human-reviewed`, `auto`만 허용합니다. 지원하지 않는 project default나 run override는 intake 중 거부합니다.

Publish mode는 project contract 저장 전에 `patch_only`, `local_branch`, `push_branch`, `deploy_command` 중 하나로 제한합니다.

Git branch input은 clone/checkout 전에 검증합니다. `default_branch`, `target_branch`, generated branch prefix는 safe git ref name이어야 하며 `-`로 시작하거나, whitespace, `..`, `@{`, `.lock` path segment를 포함할 수 없습니다.

## 프로젝트 진단

`/projects`의 진단 link, `GET /api/projects/{project_id}/doctor`, 또는 CLI로 실행합니다.

```bash
devauto doctor projects/example-project.yaml
```

진단은 workspace setup 전에 local execution contract를 검증합니다. repo/branch 접근, local repo branch가 tag가 아니라 `refs/heads/*`인지, Docker Compose 사용 가능 여부, Compose file, secret content를 읽지 않는 configured Docker env-file path, policy에 대한 deterministic command, AI role executable, publish/QA preview 일관성을 확인합니다.

준비된 모든 run은 `00-doctor.md`를 씁니다. doctor가 실패하면 clone, planner, executor, Docker, publish 단계 전에 run을 `ESCALATED`로 중단합니다.

## Publish 모드

Publish execution은 `READY_TO_PUBLISH`에서만 시작됩니다. publish approval을 기다리는 run은 approval 기록 없이 publish될 수 없습니다.

`patch_only`는 `10-final.patch`와 `12-publish.md`를 씁니다.

`local_branch`는 `.devauto/runs/{run_id}/workspace` 안에서, workspace가 expected run branch 위에 있을 때만 commit을 만듭니다. origin으로 push하지 않습니다.

`push_branch`는 격리 workspace commit을 만들고 configured `repo.branch_prefix` 아래 expected run branch로 `HEAD`를 push합니다. `publish.require_human_approval: true`가 필요하고, 기존 remote branch overwrite를 거부하며, target branch로 직접 push하지 않습니다.

`deploy_command`는 publish approval 뒤 격리 workspace에서 `publish.deploy_command`를 실행합니다. `publish.require_human_approval: true`가 필요하고, `git push`, `sudo`, 파괴적 Docker cleanup, SSH 같은 위험한 output-layer command는 계속 차단합니다.
