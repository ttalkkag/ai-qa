# MP-001 조사 근거

확인일: 2026-09-17 · 코드 정적 확인과 공식 문서 조사
멀티 프로젝트 구현·DB 마이그레이션·컨테이너 기동·브라우저 검증을 수행했다는 뜻이 아니다

## 1. 현재 코드의 연결 지점

경로는 이 저장소의 현재 구현이다. 아래의 확장 방향은 [미확정 설계안](README.md)이다

| 근거 | 확인한 내용 | 영향 |
| --- | --- | --- |
| [projects/models.py](../../../apps/projects/models.py) · Project / ProjectConfigRevision | 고유 canonical_repo_path, 단일 source_ref, 읽기/쓰기 범위와 설정 버전 | 경로 배열 추가만으로 기존 정책·이력을 보존하기 어려움 |
| [projects/services.py](../../../apps/projects/services.py) · register_project | 새 브랜치 허용 경로로 단일 저장소 등록 | 다중 등록의 사전 검증·대상별 부분 실패·재시도 계약 필요 |
| [accounts/models.py](../../../apps/accounts/models.py) · ProjectMembership | 사용자와 프로젝트의 고유 연결 | 그룹 연결과 사용자 할당을 혼동하면 안 됨 |
| [accounts/services.py](../../../apps/accounts/services.py) · require_access / _object_scope | 객체에서 owner_id와 project_id 하나 추적 | 복수 대상 자료의 공통 접근 판정 필요 |
| [conversations/models.py](../../../apps/conversations/models.py) · Conversation | 대화가 한 프로젝트·소유자에 귀속 | 공동 문맥과 접근 회수 처리 필요 |
| [work_requests/models.py](../../../apps/work_requests/models.py) · RequestRevision | 기준 SHA·설정 버전 하나, 불변 snapshot | 대상별 코드 조합을 불변 명세로 확장 |
| [work_requests/services.py](../../../apps/work_requests/services.py) · confirm_revision | 한 대상의 설정 버전·기준 SHA 확인 | 전체 대상 검증 완료 후 명세 확정 필요 |
| [runs/models.py](../../../apps/runs/models.py) · Run / Job | 실행에 단일 project·workspace·결과 브랜치/커밋 | 부모 요청 진행과 프로젝트별 실행 기록 분리 검토 |
| [runs/workspace.py](../../../apps/runs/workspace.py) · create_workspace | 기준 tree에서 파일 투영, 실행 전용 저장소에 temp/aiqa 작업 브랜치와 worktree 생성 | 원본과 독립된 저장소 모델 유지하며 대상별 확장 |
| [codex/container.py](../../../integrations/codex/container.py) · run_argv / codex_command | 한 workspace 내부 cwd 검사, bridge 네트워크, 명시적 모델·effort | 공동 경로·서비스 주소·마운트 정책 설계 필요, 네트워크 차단 전제 아님 |
| [artifacts/models.py](../../../apps/artifacts/models.py) · ResultBundle / ResultReview | 단일 실행·기준 코드·결과 hash와 사용자/관리자 검토 | 여러 결과와 연동 검사를 통합 hash에 연결 |
| [approvals/models.py](../../../apps/approvals/models.py) · PublicationPolicy / Publication | 프로젝트별 발행 정책·상태 | 여러 저장소를 한 원격 트랜잭션으로 처리하지 못함 |

현재 도메인 계약: [프로젝트](../../projects/README.md), [계정](../../accounts/README.md), [요청](../../requests/README.md), [실행](../../runs/README.md), [결과](../../artifacts/README.md), [승인](../../approvals/README.md)

## 2. 공식 자료와 적용 판단

### VS Code · Multi-root workspaces

[공식 문서](https://code.visualstudio.com/docs/editing/workspaces/multi-root-workspaces)는 여러 폴더를 한 작업 공간에 연결하고, 표시 이름과 폴더별/공통 설정을 구분한다

- 적용 제안: 여러 저장소의 연결 목록과 프로젝트별 설정을 분리하고 별칭으로 출처를 표시
- 적용하지 않을 것: IDE의 폴더 접근을 이 앱의 계정 권한으로 간주하거나 `.code-workspace`를 실행 정책으로 그대로 사용
- 판단: UI상 하나로 묶는 것과 실행·권한까지 하나로 합치는 것은 별도 문제

### Git · worktree

[공식 문서](https://git-scm.com/docs/git-worktree)는 저장소의 추가 작업 트리와 `-b`를 통한 브랜치 생성을 설명한다. 다른 worktree에서 이미 checkout한 브랜치는 기본적으로 중복 checkout하지 못한다

- 적용 제안: 저장소마다 독립된 작업 사본·브랜치·기준 SHA와 결과 커밋 기록
- 현재 앱은 원본 Git 공용 디렉토리를 공유하지 않고 파일 투영으로 만든 실행 전용 저장소에 worktree를 생성한다. 일반적인 원본 저장소의 `git worktree add`와 구별해야 함
- 판단: 여러 저장소를 묶어도 단일 Git 이력·커밋이 생기지 않음. 강제 checkout으로 충돌을 무시하지 않음

### Docker Compose · 기동 순서와 준비 완료

[공식 문서](https://docs.docker.com/compose/how-tos/startup-order/)는 컨테이너 실행 상태가 서비스 준비 완료를 뜻하지 않는다고 설명한다. `service_healthy`와 `service_completed_successfully` 등 의존 조건을 제공한다

- 적용 제안: DB 준비 → 마이그레이션 완료 → API 준비 → 프론트엔드/브라우저 검사 순서를 명시
- 검토할 것: 장기 실행 서비스 종료, startup timeout, 실패 로그 수집, 테스트 데이터 초기화, 요청별 자원 정리
- 판단: 단순히 backend 명령 다음 frontend 명령을 실행하는 것만으로 연동 검증 준비를 보장하지 못함

### Docker Compose · 네트워크

[공식 문서](https://docs.docker.com/compose/how-tos/networking/)는 기본 네트워크의 서비스 이름 검색과 컨테이너 포트/호스트 포트 차이를 설명한다. Compose 프로젝트 이름은 별도로 지정할 수 있다

- 적용 제안: 실행별 환경 이름을 정하고 컨테이너 간에는 서비스 이름, 호스트 브라우저에는 공개한 preview 주소 사용
- 현재 bridge 네트워크와 외부 접근 허용 원칙 유지. 공동 서비스 연결을 이유로 모든 외부 네트워크를 차단하지 않음
- 판단: Compose 프로젝트 이름은 이 앱의 업무 Project ID나 사용자 권한이 아님. 실행별 고유 리소스 식별자로만 사용

### Git · push의 원자성 범위

[공식 문서](https://git-scm.com/docs/git-push)의 `--atomic`은 지원되는 원격에서 ref 갱신을 모두 성공시키거나 모두 실패시키는 기능이다. 여러 원격 저장소에 걸친 원자성을 제공하는 것으로 해석하면 안 된다

- 적용 제안: 멀티 저장소 발행 지원 시 저장소별 승인 대상·시도·성공 여부·불확실 상태와 전체 부분 완료 상태 기록
- 판단: 로컬 통합 결과 승인과 실제 원격 발행을 분리하고, 최초 구현은 전자까지 검증하는 것이 단순함

## 3. 아직 검증하지 않은 부분

- Codex 호출에 여러 사본을 제공할 때의 도구 경로 판정·문맥 크기·지침 충돌과 읽기 전용 대상 보호
- Python/Node 공동 환경의 이미지 조합, 서비스 기동 시간·메모리·저장 공간 비용
- Compose 채택 여부와 기존 실행 어댑터에 통합할 구체적인 방식
- 같은 Git 공용 디렉토리를 사용하는 다른 checkout, 모노레포 모듈·submodule의 중복 식별
- 공동 데이터 마이그레이션과 프론트엔드 변경의 호환성 검사 기준
- 기존 결과·승인 이력을 유지하는 데이터 이전 방식과 되돌리기 계획

이 항목들은 구현 전 실험·수용 테스트 대상이다. 공식 도구가 지원하는 기능만으로 현재 앱이 이미 지원한다고 판단하지 않는다
