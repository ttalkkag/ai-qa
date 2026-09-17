# 실행 도메인

확정된 요청을 독립 작업 사본에서 수정·검증하고, 확인 가능한 결과로 끝내는 계약입니다. 이 문서는 새 Django 구현의 기준이며 현재 `devauto`가 아래 기능을 구현했다는 뜻이 아닙니다.

현재 `apps/runs`는 독립 Git worktree·Docker 실행·검증·결과 보존을 담당합니다. 네트워크는 허용하며, 종료 미확인 로컬 작업은 격리 기록을 남기고 새 실행에서 재사용하지 않습니다. 실제 확인 범위는 [검증 기록](../operations/verification.md)을 따릅니다.

## 1. 목적과 책임

- 매니저는 요청을 확정하고 `수정 시작`을 누른 뒤 진행 상황·취소·결과를 확인합니다. CLI 인자나 프로세스 관리는 입력하지 않습니다.
- 실행 모듈은 대기열, 작업 사본, 상태 전이, 프로세스 생명주기, 결정적 검증을 책임집니다.
- [요청](../requests/README.md)은 확정 명세, [AI](../ai/README.md)는 Codex 호출·샌드박스, [결과물](../artifacts/README.md)은 불변 증거를 소유합니다.
- [승인·발행](../approvals/README.md)은 별도 흐름입니다. `SUCCEEDED`는 로컬 결과 생성 완료이며 push·PR·merge·배포 완료가 아닙니다.
- 앱의 수정용 Codex 호출은 실행마다 한 번입니다. 한 호출 안에서 Codex 내장 도구가 수정·자체 확인을 반복하는 것은 deadline 안에서 허용하지만, 앱이 호출 종료 후 검사 실패를 이유로 자동 재호출하지 않습니다. Codex 자체 검사와 앱의 필수 AFTER 검사는 구분합니다.

## 2. 저장할 데이터

| 객체 | 필수 필드·규칙 |
|---|---|
| `Run` | UUID, project_id, request_revision_id, requested_by, status, state_version, created_at, started_at, finished_at |
| `Job` | UUID, kind, subject_id, actor_id, idempotency_key, queued_at, state(QUEUED/RUNNING/DONE/FAILED/CANCELLED), slot, instance_id, lease 정보; 대화/명세 AI/모델 점검은 대화 슬롯, Run/발행 준비·실행·대조는 실행 슬롯 |
| 실행 기준 | baseline_commit_sha, snapshot_base_commit_sha, project_config_version, model_profile_version, previous_run_id, 제출 idempotency_key |
| 실행 정보 | workspace_id, current_stage, heartbeat_at, lease_owner, lease_epoch, lease_expires_at, deadline_at |
| 종료 정보 | stop_reason, error_code, 사용자 설명, result_bundle_id; 예외 원문을 사용자 메시지에 그대로 넣지 않음 |
| `ProcessRecord` | job_id, operation_id, run_id, stage, PID·시작 identity, process_group_id, container_name, worker_identity, instance_id, host_boot_id, started_at, ended_at, state; 종료 미확인은 ABANDONED이며 ended_at을 채우지 않음 |
| `CheckResult` | run_id, BASELINE/AFTER, check_id·설정 버전, argv, 상대 cwd, 환경 프로파일 ID, 시작·종료·duration, exit_code |
| 검증 증거 | checked_source_tree_sha256, outcome, parser_id·버전, report_path 선택, report_sha256 선택, collected/passed/failed/skipped_count, output_complete, log_artifact_id |
| `RunEvent` | run_id, 단조 증가 sequence, 이전/다음 상태, 사건 종류, actor_id 또는 worker_identity, 시각, 비밀 제거한 설명 |

상태·현재 단계는 DB가 원본입니다. 로그 문구나 AI의 “완료했습니다”를 읽어 상태를 추정하지 않습니다. `previous_run_id`는 재시도의 출처이며 이전 실행 레코드를 덮어쓰지 않습니다.

## 3. 외부 Interface와 화면

구현 위치는 `apps/runs/`입니다. Django view와 테스트는 아래 작은 Interface를 공유하고 worker 내부 단계 함수를 직접 호출하지 않습니다.

| Interface | 입력과 결과 | 거부 조건 |
|---|---|---|
| `create_run(actor, revision_id, key)` | 요청 도메인이 호출; Run ID와 기존/신규 여부 | 미확정 요청, 접근권한 없음, 준비 안 된 프로젝트, 동일 revision의 중복 primary run |
| `get_run(actor, run_id, after_sequence)` | 상태·단계·누적시간·새 이벤트·결과 링크 | 현재 계정의 소유권/프로젝트 권한 없음 |
| `cancel_run(actor, run_id, key)` | 취소 접수 상태; 실제 종료는 비동기 | 타인 실행에 대한 권한 없음, 이미 종료됨 |
| `retry_run(actor, run_id, key)` | 같은 확정 revision으로 새 Run, previous_run_id 기록 | 실행 중, 기준 SHA/정책 변경, 준비 검사 실패 |

- 조회 화면은 `대기 → 준비 → 수정 → 검증 → 결과`로 표시하고 실패·취소·중단을 별도로 표시합니다. 근거 없는 진행률 퍼센트는 표시하지 않습니다.
- 상태 조회는 AI를 호출하지 않습니다. 화면이 열린 동안 2초 간격으로 새 이벤트를 조회하고 숨겨진 탭에서는 10초로 줄입니다.
- `POST /runs/<id>/cancel/`, `POST /runs/<id>/retry/`는 로그인·CSRF·객체 권한을 검증합니다. GET으로 상태를 바꾸지 않습니다.
- 같은 key의 같은 요청은 기존 응답을 돌려주고, 같은 key를 다른 revision에 재사용하면 409로 거부합니다.
- 사용자가 대화에서 추가한 요구는 현재 실행에 주입하지 않습니다. 새 명세로 확정한 뒤 별도 실행합니다.

## 4. 실행 순서

1. 제출 시 계정·프로젝트·확정 revision을 확인하고 `QUEUED`를 저장합니다. 계정 권한은 worker가 시작할 때도 다시 확인합니다.
2. worker가 전역 실행 슬롯을 조건부 점유하고 `PREPARING`으로 바꿉니다. DB 트랜잭션은 여기서 종료합니다.
3. [프로젝트](../projects/README.md)의 등록된 local ref가 revision의 committed SHA와 같은지 검사합니다. dirty·untracked 파일은 보존하고 실행 제외 사실을 표시합니다.
4. 해당 SHA의 전체 기준 tree를 보존하고 허용 파일로 실행 전용 저장소를 만듭니다. 이 저장소에서 `git worktree add --detach`로 수정 worktree를 생성합니다. 원본 `.git`·객체·hooks·원격 설정은 공유하지 않으며 baseline 검사 사본도 별개입니다.
5. Docker 연결과 실행 이미지를 확인하고 해당 작업 디렉토리만 마운트합니다. 원본 저장소·다른 작업·호스트 홈·Docker 소켓을 공유하지 않습니다. 외부 네트워크와 웹 검색은 허용합니다.
6. 각 사본에서 등록된 PreparationCommand로 환경을 준비하고 baseline 검사를 수행합니다. 준비·검사는 별도 Docker 컨테이너 안에서 실행되며 패키지 다운로드가 가능합니다. 사본에는 생성물을 쓸 수 있지만 전후 source hash가 바뀌면 실패 처리합니다. 원본 환경은 사용하지 않습니다.
7. baseline 증거를 보존하고 깨끗한 실행 사본에서 명시한 model·effort의 Codex 수정 호출을 한 번 수행합니다. 상태는 `RUNNING`입니다.
8. Codex와 하위 프로세스 종료를 확인한 뒤 `VERIFYING`으로 바꿉니다. 전체 보존 baseline에 허용 변경만 적용하고 비노출·보호 파일을 원상 결합해 최종 후보 source tree/hash를 먼저 고정합니다. 설정 파일의 복원이 Codex 설정 재활성화를 뜻하지는 않습니다.
9. 후보를 `after-checks/<run_id>/`에 독립 복사하고 새 컨테이너에서 같은 버전의 AFTER 검사를 수행합니다. 매 검사 전후 후보 hash가 같아야 합니다. 검사 컨테이너 종료 후 허용 범위·변경 크기·비밀 검사를 거쳐 검사했던 동일 후보만 봉인합니다. source가 바뀌면 `SOURCE_CHANGED_DURING_CHECK`로 실패합니다.
10. 완전한 결과와 필수 검사 통과이면 `SUCCEEDED`, 그렇지 않으면 `FAILED`로 끝냅니다. 결과와 종료 상태 연결은 짧은 DB 트랜잭션으로 확정합니다.

모든 대상 코드 실행은 준비·baseline·AFTER 검사를 포함해 Docker 컨테이너 안에서 수행합니다. Docker가 없을 때 호스트에서 대신 실행하지 않습니다. Git hooks·외부 diff helper를 비활성화한 파일 수집은 호스트에서 수행하되 대상 프로젝트 코드를 실행하지 않습니다.

### committed SHA를 사본으로 만드는 방법

- Git tree/blob를 필터·hooks 없이 읽고 파일로 투영합니다. 실행별 비공개 저장소 `staging/<run_id>/worktree-repo`와 그 저장소에 연결된 수정 worktree를 만들며 원본 객체·alternates·hardlink를 공유하지 않습니다.
- `baseline_commit_sha`는 원본에서 선택한 SHA입니다. 새 독립 저장소의 기준 commit은 `snapshot_base_commit_sha`로 따로 기록합니다. 서로 다른 SHA를 같은 값이라고 표시하지 않습니다.
- 원본 `.git/config`·remotes·hooks·개인 Git 설정을 복사하지 않습니다. snapshot 생성은 fetch·checkout·stash·원본 파일 쓰기를 하지 않습니다.
- 읽기 제외 파일을 포함한 전체 baseline은 신뢰된 비공개 영역에 보존하고 AI 사본에는 허용 파일만 투영합니다. 최종 tree는 사본 전체가 아니라 `전체 baseline + 허용된 변경`입니다. 비노출 경로의 부재는 삭제가 아니며, 삭제는 노출된 수정 허용 경로의 명시적 차이로만 수집합니다.
- 원본 개인 Codex 설정을 컨테이너에 복사하지 않습니다. 실행은 `--ignore-user-config --strict-config`를 사용합니다. 비노출 파일은 AFTER 검사 전에 원상 결합합니다. 세부 경계는 [AI](../ai/README.md)를 따릅니다.
- 원본 staged·unstaged·untracked는 시작 사본에 포함하지 않습니다. 실행 전후 목록·내용 hash·index·ref를 비교하고 외부 사용자의 동시 변경은 별도 감지하되 되돌리지 않습니다.

### 로컬 결과 브랜치와 커밋

- 수정 워크트리는 detached HEAD가 아닌 `temp/aiqa-<run_id>` 브랜치를 사용한다
- 이 브랜치는 원본 저장소가 아니라 `staging/<run_id>/worktree-repo`의 독립 Git 저장소에 존재한다. 원본 GitHub Desktop 저장소에는 자동으로 나타나지 않는다. 해당 작업 워크트리를 별도로 열어 확인할 수 있다
- 필수 AFTER 검사와 결과 묶음의 완전성·비밀정보 검사를 통과한 뒤 앱이 결과 커밋을 생성한다. 모델의 staging 상태가 아니라 고정된 검증 결과에서 허용된 파일만 커밋하며 생성물·비노출 파일은 포함하지 않는다
- 커밋 메시지는 `fix: apply verified AI QA result <run_id>`이며 기준은 독립 snapshot commit이다. 원본 SHA와 동일한 Git 이력을 가진 커밋으로 취급하지 않는다
- `Run.result_branch`, `Run.result_commit_sha`에 기록하며 관리자는 결과 화면의 ‘로컬 Git 결과’에서 확인한다. 실패한 검사에는 성공 결과 커밋을 생성하지 않는다
- 이 커밋은 로컬 결과 보존이지 사용자 완료 확인·관리자 승인·원격 발행이 아니다. 원본에 반영하거나 원격에 발행하는 단계는 별도다
- 결과 묶음의 `evaluation.json`에는 완료 조건별 검토 상태, AI 분석 보고, 실제 명령 이벤트를 담는다. 일반 검사 통과만으로 개별 완료 조건을 PASS로 표시하지 않는다
- 기존 committed symlink는 상대 target이 사본 안의 실제 파일을 가리키는 경우만 보존합니다. 절대·탈출·dangling·순환 link는 준비 전에 차단하며 읽기·복사·hash 수집은 NOFOLLOW 방식으로 link target 자체를 검사합니다.
- 새로운 symlink 또는 target 변경은 지원하지 않습니다. 기존 link의 mode·target은 보존하고 최종 tree hash에는 target 문자열 자체를 넣습니다. 검증 중 link가 교체되면 실행을 중단합니다.
- submodule(gitlink)·Git LFS pointer가 있는 기준은 `UNSUPPORTED_SNAPSHOT`으로 차단합니다. 자동 submodule update·LFS 다운로드·원격 접근을 하지 않으며 “파일 일부만 준비한 성공”도 만들지 않습니다.
- PreparationCommand는 기본 300초, 최대 1,800초이며 사본별 전용 환경을 사용합니다. 관리자 등록 명령이나 Codex 도구의 패키지 다운로드는 컨테이너 안에서 가능합니다. 원본·다른 사본의 writable dependency·cache는 공유하지 않습니다.
- Codex 수정 환경과 AFTER 검사 환경은 별개입니다. AFTER는 등록된 준비 명령으로 새 환경을 구성하며 Codex가 변경한 실행 파일을 검사 도구로 재사용하지 않습니다. 선택적 의존성 묶음을 사용한다면 후보 lock hash와 맞아야 합니다.
- 선택적 오프라인 의존성 묶음은 hash·잠금 파일과 Docker 이미지의 Linux 플랫폼·Python 버전을 대조한 뒤 각 사본의 `.aiqa-inputs/`로 복사합니다. 묶음 없이 온라인 준비 명령을 사용하는 것도 가능합니다.

## 5. 상태와 전이

| 상태 | 의미 | 허용되는 다음 상태 |
|---|---|---|
| `QUEUED` | 실행 대기, 자식 프로세스 없음 | PREPARING, CANCELLED, FAILED |
| `PREPARING` | 사본·정책 확인, baseline 검사 | RUNNING, FAILED, CANCEL_REQUESTED, INTERRUPTED |
| `RUNNING` | Codex 수정 호출 진행 | VERIFYING, FAILED, CANCEL_REQUESTED, INTERRUPTED |
| `VERIFYING` | AFTER 검사와 결과 봉인 | SUCCEEDED, FAILED, CANCEL_REQUESTED, INTERRUPTED |
| `CANCEL_REQUESTED` | 종료 요청됨, 아직 종료를 입증하지 못함 | CANCELLED, FAILED, INTERRUPTED |
| `SUCCEEDED` | 필수 검사를 통과한 완전한 로컬 결과 | 없음 |
| `FAILED` | 실행·검증·결과 수집 실패 | 없음 |
| `CANCELLED` | 사용자 취소 처리; 종료 미확인 기록은 별도 보존 | 없음 |
| `INTERRUPTED` | worker 중단으로 실행 연속성 상실; 기존 환경 재사용 금지 | 없음 |

- 종료 상태에서 기존 실행을 재개하지 않습니다. 재시도는 새 Run입니다. 성공 결과를 바꾸려면 새 요청 revision이 필요합니다.
- 성공 봉인은 작업 컨테이너 종료와 검증 완료 후에만 가능합니다. 종료 미확인 로컬 작업은 실패·중단으로 기록하고 프로세스 기록을 `ABANDONED`로 남깁니다. ended_at을 허위로 채우거나 기존 결과를 성공으로 취급하지 않습니다.
- `CANCEL_REQUESTED` 뒤의 최종 상태는 원인에 따라 사용자 취소 `CANCELLED`, 실행 오류·deadline `FAILED`, worker 유실 `INTERRUPTED`입니다.
- 상태 갱신은 현재 상태·state_version·lease_epoch 조건을 함께 검사합니다. 늦게 도착한 이전 worker의 성공 이벤트는 무시하고 감사 기록을 남깁니다.

## 6. 작업 점유와 SQLite

- `apps.runs`의 관리 명령 `python manage.py process_jobs`가 작업을 처리합니다. 별도 runner HTTP 서버, 메시지 브로커, 독립 배포 단위는 만들지 않습니다. `apps.ai`는 호출 기록·모델 설정·Codex adapter를 소유합니다.
- HTTP 요청은 영속 Job 등록까지만 수행합니다. 대화·명세 분석·발행 준비·원격 상태 대조도 CLI 완료를 HTTP 안에서 기다리지 않고 Job 결과를 polling합니다. Job DONE은 해당 단계 완료이며 Run 성공·Publication 발행 성공을 대신하지 않습니다.
- 전역 대화 슬롯 1개와 실행 슬롯 1개를 둡니다. 다른 프로젝트도 코드 수정·검증·실제 발행은 같은 실행 슬롯을 사용합니다. 승인 대기는 슬롯을 점유하지 않습니다.
- 각 슬롯의 대기 작업은 `(queued_at, id)` 순서입니다. 긴 수정 중에도 대화 슬롯은 동작할 수 있지만 대화에는 수정·발행 권한이 없습니다.
- 슬롯 claim은 SQLite의 짧은 write transaction 안에서 만료 시각·소유자·epoch를 비교하는 조건부 갱신입니다. 성공한 worker 한 개만 작업을 시작합니다.
- lease는 45초, heartbeat는 5초입니다. CLI 출력이 없어도 heartbeat는 별도 주기로 갱신합니다. lease 만료만으로 새 실행을 시작하지 않습니다.
- 프로세스를 기다리는 동안 DB write transaction을 유지하지 않습니다. 이벤트·heartbeat는 각각 짧게 기록하고 DB 잠금 오류를 이유로 CLI를 재실행하지 않습니다.
- 수정 호출의 deadline은 30분이며 프로젝트 검사 timeout은 명령당 기본 300초, 최대 1,800초입니다. `deadline_at`은 현재 단계의 제한 시각으로 갱신하며 전체 Run을 30분으로 제한한다는 뜻은 아닙니다. 제한 초과는 성공이 아니라 종료 처리 후 `FAILED`입니다.
- child 실행 전 operation_id를 예약합니다. PID·시작 identity·프로세스 그룹을 기록하기 전에는 대상 코드 실행을 열지 않는 시작 동기화가 필요합니다.
- 시작 중 crash로 종료를 확인하지 못한 로컬 작업은 ABANDONED로 보존하고 슬롯을 해제합니다. 재시도는 새 Run·worktree·컨테이너입니다. 원격 발행 결과가 불명확한 경우에는 중복 push·PR 방지를 위해 별도 확인을 유지합니다.

## 7. 취소와 crash 복구

1. 아직 점유되지 않은 QUEUED 작업은 조건부로 CANCELLED 처리합니다. child가 없음을 확인합니다.
2. 실행 중 취소 요청은 먼저 `CANCEL_REQUESTED`로 기록하고 새 단계·검사를 시작하지 않습니다.
3. 기록된 PID와 시작 identity가 모두 일치하는지 검사한 뒤 해당 프로세스 그룹에 TERM을 보내고 최대 5초 기다립니다.
4. 남아 있으면 KILL을 보내고 최대 5초 기다립니다. 부모 PID 종료뿐 아니라 추적한 자손과 그룹 전체의 종료를 확인합니다.
5. 작업 컨테이너를 제거합니다. 종료가 불명확한 로컬 작업은 ABANDONED 기록과 기존 파일을 보존하고 슬롯을 해제합니다. 새 실행에서 이 환경을 재사용하지 않습니다.

PID만 일치하는 다른 프로세스에는 신호를 보내지 않습니다. identity 불일치·조회 실패를 종료 성공으로 기록하지 않으며 기존 사본도 삭제하지 않습니다. 이 불확실성이 새 로컬 실행 자체를 막지는 않습니다.

worker 재시작은 stale 작업을 정리하고 기존 Run을 INTERRUPTED로 기록합니다. 종료 미확인 로컬 프로세스는 ABANDONED로 남기되 신규 실행을 허용합니다. 자동 resume·모델 무한 재호출은 하지 않습니다. 원격 발행은 결과 확인 없이 반복하지 않습니다.

## 8. 검증 결과 판정

| outcome | 의미 | 필수 검사 통과로 계산 |
|---|---|---|
| `PASS` | 실제 실행·완전한 증거·종류별 성공 조건 충족 | 예 |
| `FAIL` | 실제 비정상 종료 또는 확인된 실패 항목 | 아니요 |
| `TIMEOUT` | 제한 시간 초과 및 종료 절차 수행 | 아니요 |
| `NOT_RUN` | 앞 단계 실패·취소 등으로 실행하지 않음 | 아니요 |
| `UNVERIFIED` | 파서 미지원·손상된 결과·출력 유실 등으로 판정 불가 | 아니요 |

- 관리자가 등록한 required check가 최소 1개 있어야 수정 실행을 허용합니다. 명령이 없으면 성공하는 `noop`을 만들지 않습니다.
- lint·build 등 `COMMAND` 검사는 exit 0, timeout 없음, 증거 완전성을 판정하며 테스트 개수는 `N/A`입니다. “테스트 통과”로 바꾸어 말하지 않습니다.
- `TEST` 검사는 등록된 파서로 실제 실행된 테스트 수·실패·skip을 확인합니다. 실행 0개·전부 skip은 UNVERIFIED입니다. 일부 skip은 통과 수에서 제외하고 별도 표시하며, 실행된 테스트가 있고 실패 0·증거 완전일 때만 PASS입니다. 파서 미지원이면 개수는 `unknown`이며 통과로 계산하지 않습니다.
- JUnit은 등록된 report_path에서 이번 단계·검사에 새로 생성된 결과만 수집합니다. 이전 실행 파일·symlink·누락·손상 XML·외부 엔티티 참조는 거부합니다. stdout 요약과 report 내용이 충돌해도 UNVERIFIED이며 출처가 다른 보고서를 대신 쓰지 않습니다.
- 검증 결과는 등록 명령이 고정된 source에서 실제 보고한 증거이지 업무 의미나 악의적 테스트의 정직성을 증명하는 보증은 아닙니다. 테스트/검증 스크립트·수집 설정이 수정됐으면 변경 경로와 전후 hash를 결과·승인 화면에 별도 표시합니다. 테스트 삭제·무력화로 완료 기준을 만족했다고 주장하지 않습니다.
- BASELINE의 실제 코드 검사 FAIL은 기존 결함으로 표시하고 수정할 수 있습니다. baseline 설치 실패·timeout·미실행·증거 부족은 기준을 알 수 없으므로 수정 전에 차단합니다.
- AFTER의 required 검사 모두 PASS이고 결과물이 완전해야 SUCCEEDED입니다. “기존 실패와 같다”는 이유로 AFTER 실패를 면제하지 않습니다.
- 준비 명령과 모델 도구의 네트워크는 컨테이너 안에서 허용합니다. 설치 실패는 검사 실패로 남기며 호스트 설치로 전환하지 않습니다.
- AI 검토 의견은 보조 증거입니다. AI가 PASS라고 말해도 실제 검사 결과·상태·발행 권한을 바꾸지 않습니다.

## 9. 오류와 재시도

| 오류 코드 | 사용자 안내 | 처리 |
|---|---|---|
| `BASE_CHANGED` | 준비한 코드 기준이 변경되었습니다 | 새 revision 확인, 기존 실행 재사용 금지 |
| `PROJECT_NOT_READY` | 관리자 설정 확인이 필요합니다 | 수정 시작 전 중단 |
| `DOCKER_UNAVAILABLE` / `DOCKER_IMAGE_MISSING` | Docker 또는 실행 이미지가 준비되지 않았습니다 | Docker 준비 후 새 실행; 호스트 직접 실행으로 전환 금지 |
| `AI_FAILED` | AI 작업을 완료하지 못했습니다 | 원인 분류·부분 결과, 자동 모델 대체 없음 |
| `CHECKS_INCOMPLETE` | 검증이 완료되지 않았습니다 | 미실행/알 수 없음 구분, 발행 금지 |
| `PROCESS_STATE_UNKNOWN` | 이전 프로세스 종료 미확인 | 로컬 작업은 별도 보존 후 새 실행 허용; 원격 발행은 중복 방지 확인 유지 |
| `RESULT_INCOMPLETE` | 결과를 안전하게 보관하지 못했습니다 | 성공·발행 금지, 보존된 증거 안내 |
| `SOURCE_CHANGED_DURING_CHECK` | 검사 중 검증 대상 코드가 바뀌었습니다 | 성공·발행 금지, 변경 경로·검사 ID 기록 |

재시도는 새 idempotency_key와 새 Run ID를 사용하며 동일 key의 재전송은 한 번만 생성합니다. 원본 SHA·project_config_version이 달라졌다면 재시도가 아니라 새 확정 명세를 요구합니다. 실패 결과와 재시도 결과는 화면에서 나란히 비교할 수 있습니다.

## 10. 결정 사항

- **RUN-01:** 원본 저장소는 읽기 기준이며 committed SHA의 독립 사본에서만 수정합니다. dirty·untracked 자동 반영은 하지 않습니다.
- **RUN-02:** Django 관리 명령·SQLite·전역 실행 슬롯 1개로 시작합니다. 실행 슬롯 안에서도 DB transaction과 subprocess 대기를 분리합니다.
- **RUN-03:** native sandbox 보호를 입증하지 못하면 실행하지 않습니다. 격리 제품을 새로 만드는 대신 안전 조건을 기능의 전제로 둡니다.
- **RUN-04:** 명시적 취소 접수와 실제 종료를 구분합니다. lease 만료는 프로세스 종료 증거가 아닙니다.
- **RUN-05:** baseline·AFTER 증거를 분리하고 실제 실행 여부·개수를 기록합니다. 자동 수정 반복·자동 resume은 하지 않습니다.
- **RUN-06:** 결과 성공과 관리자 발행을 분리합니다. 모델이나 매니저가 Run 상태를 발행 승인으로 바꿀 수 없습니다.

## 11. 인수 조건

- [ ] **RUN-AC-01:** 같은 제출을 동시에 보내도 primary Run은 1개이며 서로 다른 worker 2개가 같은 실행 슬롯을 점유하지 않는다.
- [ ] **RUN-AC-02:** 원본 tracked·dirty·untracked·Git index·ref가 실행 전후 동일하고 사본과 원본의 파일/Git object가 공유되지 않는다.
- [ ] **RUN-AC-03:** baseline 검사 생성물이 수정 시작 사본에 섞이지 않고 모든 프로젝트 코드 실행이 native sandbox를 통과한다.
- [ ] **RUN-AC-04:** 취소 중 생성된 자손, TERM을 무시하는 자손, PID 재사용, process 조회 실패에서 거짓 CANCELLED·슬롯 해제가 없다.
- [ ] **RUN-AC-05:** worker crash·lease 만료·DB lock·이전 epoch의 완료 이벤트로 중복 수정·발행이 발생하지 않는다.
- [ ] **RUN-AC-06:** required check 없음, 테스트 0개, 전부 skip, 파서 미지원, 출력 유실을 PASS·SUCCEEDED로 표시하지 않는다.
- [ ] **RUN-AC-07:** baseline 실제 실패와 AFTER 실패를 구분하며 required AFTER 실패가 있는 결과로 발행할 수 없다.
- [ ] **RUN-AC-08:** 긴 수정 중 상태 조회에 AI 호출이 없고 대화가 가능하다. 다른 프로젝트 수정·발행은 실행 슬롯에서 대기한다.
- [ ] **RUN-AC-09:** 계정 접근권한 회수 후 조회·재시도·취소 요청의 권한을 다시 검사하고 관리자 취소 경로는 유지한다.
- [ ] **RUN-AC-10:** fake 검사, 현재 prototype 테스트, 실제 Codex·샌드박스 검증 결과를 별도 증거로 보고한다.
- [ ] **RUN-AC-11:** 준비/검사에서 source를 바꾸려는 fixture가 차단되며, 검사 전후 tree 불일치·Codex의 검사 도구 변조·stale JUnit으로 SUCCEEDED 결과를 만들지 못한다.
- [ ] **RUN-AC-12:** Run 없는 대화·모델 점검·발행 Job도 공통 프로세스 추적·취소·복구 대상이며, 복원된 instance의 이전 Job은 자동 claim하지 않는다.

## 12. 기존 코드와의 관계

`devauto/runner/worker.py`, `pipeline.py`, `subprocesses.py`, `workspace.py`, `gates.py`, `devauto/core/state_machine.py`는 현재 prototype 동작을 확인할 참고 코드입니다. 기존의 `noop` 성공, 단순 clone 작업 사본, 부모 프로세스 종료 위주 취소, 발행을 섞은 상태를 새 계약의 구현으로 간주하지 않습니다. 새 실행 모듈의 Interface 테스트로 위 인수 조건을 검증한 부분만 이전합니다.
