# AI — 로컬 Codex와 Docker 실행

기준일: 2026-09-14. 실행 기준은 사용자 결정에 따라 **프로젝트별 작업 공간, 분리된 Git 워크트리, Docker 실행, 외부 네트워크 허용**이다. 네이티브 프로세스 그룹 이탈 금지·전체 개인 설정 감사·모델 파일 도구 차단 시험은 제품 실행의 필수 조건이 아니다. 과거 `SANDBOX_UNAVAILABLE` 기록을 PASS로 바꾸지 않고 새 실행 경로를 사용한다.

## 1. 책임

AI 도메인은 로그인 확인, 목적별 모델 선택, 입력 구성, Codex 실행과 응답 기록을 담당한다. 계정 권한·명세 확정·검증 통과·원격 발행 승인은 앱의 해당 도메인이 결정한다. API 직접 호출·Claude·원격 러너 서비스는 추가하지 않는다.

## 2. 실행 환경과 로그인

- 웹과 worker는 관리자 PC에서 실행하고, Codex 및 프로젝트 준비·검증 명령은 작업별 Docker 컨테이너에서 실행한다
- Docker Desktop과 실행 이미지가 필요하다. Docker가 없으면 호스트에서 프로젝트 명령을 대신 실행하지 않는다
- 서버 계정의 기존 `CODEX_HOME/auth.json`을 해당 Codex 컨테이너에만 읽기 전용으로 연결하고, 컨테이너 내부 전용 홈에 임시 복사하여 사용한다
- 인증값은 앱 DB·브라우저·프롬프트·로그에 저장하지 않는다. 호스트 인증 파일은 수정하지 않는다
- 준비·검증 컨테이너에는 Codex 인증을 전달하지 않는다. 호스트 홈 전체·SSH agent·Docker 소켓·앱 DB·다른 실행은 마운트하지 않는다
- 파일 로그인이 아닌 OS Keychain 전용 로그인은 자동 이전하지 않는다. 파일 로그인이 없으면 `LOGIN_REQUIRED`로 안내한다
- 컨테이너에서 갱신된 로그인은 호스트에 자동 덮어쓰지 않는다. 호스트 로그인이 만료되면 호스트에서 갱신 후 새 실행을 시작한다

인증 파일은 접근 토큰을 포함한다. 로그인 재사용 방식은 [공식 인증 문서](https://learn.chatgpt.com/docs/auth)를 따른다. 네트워크가 열린 Codex 컨테이너의 프로그램이 내부 인증 파일에 접근할 수 있으므로 신뢰하는 프로젝트를 대상으로 사용한다. 컨테이너 내부 코드와 Codex 인증을 완전히 분리한다고 주장하지 않는다.

## 3. 모델과 effort

| 프로필 | 목적 | 초기 설정 | 제한 |
| --- | --- | --- | --- |
| conversation | 질문·요구사항 정리 | gpt-5.6-luna / low | 5분, 소스 읽기 전용 |
| implementation | 코드 수정 | gpt-6-astra / xhigh | 30분, 해당 워크트리 수정 |
| implementation_deep | 심층 수정 | gpt-6-astra / ultra | 30분, 기본 비활성 |

모델과 effort는 매 호출 명시하고 프로필 버전을 기록한다. 지원되지 않는 조합을 다른 모델로 자동 대체하지 않는다. 새 프로필은 관리자 확인 모달을 거친 실제 최소 호출로 검증한다. 상태 조회는 모델을 호출하지 않는다. 응답에 실효 모델·effort가 없으면 `not_reported`로 구분한다.

## 4. 작업 공간과 네트워크

- 원본의 확정 committed 코드를 별도 저장소로 준비하고 수정용 Git 워크트리를 생성한다. 원본 미커밋 변경은 포함하지 않는다
- 컨테이너 작업 디렉토리는 해당 사본으로 고정한다. 수정 컨테이너에는 해당 실행의 Git metadata만 추가 연결한다
- 대화 소스는 읽기 전용, 수정 소스는 쓰기 가능하다. 준비·검사는 해당 검사 사본에서 실행하고 검사 전후 소스 hash로 변경 여부를 확인한다
- 프로젝트의 읽기 제외 파일은 AI 입력 사본에서 제외하고, 수정 허용 범위 밖 변경은 결과 확정 시 거부한다
- Docker bridge 네트워크를 사용한다. 웹 검색, 외부 API 접근, 패키지 다운로드를 일괄 차단하지 않는다
- 프로젝트 준비 명령은 컨테이너의 도구 경로를 사용한다. Mac의 venv나 실행 파일 경로를 컨테이너에 그대로 전달하지 않는다
- 네트워크 허용은 앱의 push·PR 승인 생략을 의미하지 않는다. 원격 발행은 기존 관리자 계정 승인 흐름을 유지한다

## 5. Codex 호출

`integrations/codex/container.py`가 Docker argv를 만들고 `apps/runs/processes.py`가 클라이언트 프로세스를 관리한다. 프롬프트는 stdin으로 전달한다.

```text
docker run --rm --init --network bridge --workdir <task_workspace> ...
  codex exec --ignore-user-config --strict-config --ephemeral --json
  --sandbox danger-full-access --model <model>
  -c model_reasoning_effort="<effort>"
  -c approval_policy="never" -c web_search="live"
  --cd <task_workspace> --skip-git-repo-check -
```

내부 `danger-full-access`는 **Docker 컨테이너 안에서만** 사용한다. Docker 밖 호스트 실행으로 전환하지 않는다. Docker를 외부 격리 경계로 사용하는 방식은 [공식 실행 보안 문서](https://learn.chatgpt.com/docs/agent-approvals-security)를 참고한다.

JSONL 최종 응답·완료 이벤트·종료 코드·출력 완전성을 확인한다. 이벤트 1 MiB, 스트림 8 MiB, 최종 응답 200 KiB 제한을 유지한다. AI가 테스트 통과를 주장해도 실제 검사 증거를 대신하지 않는다.

`AIInvocation.execution_events`에는 명령·파일 변경 이벤트의 순서, 명령 ID, 시작/완료, 종료 코드를 저장한다. 원시 stdout·추론 내용은 이 필드에 저장하지 않으며 명령 내 자격정보는 마스킹한다. 이벤트 1,000개·명령 4,096자 제한으로 생략이 발생하면 `trace_complete=false`로 기록하여 완전한 실행 증거로 평가하지 않는다. 과거 호출도 기본값은 미확인이다.

공통 구현 지침은 `apps/ai/prompts.py`에서 버전 관리한다. 매니저가 스킬을 선택하거나 기술적인 평가 규칙을 입력하지 않는다. 프로젝트 검증 명령은 실제 코드 검사를 담당하며, 요청별 완료 조건의 의미상 충족은 AI 보고·변경·검사 증거를 함께 검토한다. 반복 평가와 지침 비교는 [로컬 테스트](../operations/testing.md#실제-ai-행동-평가)를 따른다.

## 6. 실패·종료·재시도

작업별 컨테이너 이름을 ProcessRecord에 기록하고 종료 시 해당 컨테이너 제거를 시도한다. 종료를 확인하지 못한 로컬 실행은 `ABANDONED` 기록과 워크트리를 보존한다. 종료 시각을 허위로 채우거나 성공 결과를 만들지 않는다. 해당 슬롯은 해제하며 재시도는 새로운 Run·워크트리·컨테이너를 만든다. 이전 프로그램의 늦은 응답은 기존 lease 검증으로 새 실행에 반영하지 않는다.

무한 자동 재시도는 하지 않는다. 사용자가 다시 실행하면 이전 종료 미확인만으로 막지 않는다. 원격 발행의 결과 불확실성은 별도 도메인이 관리하며 push·PR을 중복 실행하지 않는다.

## 7. 화면과 진단

AI 화면에는 Codex 연결, Docker 실행 환경, 모델 프로필, 최근 호출 기록을 표시한다. GET 조회만으로 CLI·모델을 호출하지 않는다. Docker 환경 점검은 모델 없이 임시 작업 폴더의 실제 파일 생성·읽기를 검사한다. 과거 네이티브 검사는 새 실행 준비 조건이 아니다.

| 오류 | 조치 |
| --- | --- |
| DOCKER_UNAVAILABLE | Docker Desktop 실행·연결 확인 |
| DOCKER_IMAGE_MISSING | 실행 이미지 빌드 |
| LOGIN_REQUIRED | 서버 계정의 Codex 파일 로그인 확인 |
| MODEL_UNAVAILABLE / MODEL_MISMATCH | 지정 모델·effort 확인, 자동 대체 없음 |
| CONTAINER_TERMINATION_UNCONFIRMED | 기존 컨테이너·폴더 보존, 새 실행 가능 |
| RATE_LIMITED / CLI_TIMEOUT | 제한·시간 초과 기록 후 새 실행 여부 판단 |

## 8. 수용 기준

- API 키 추가 입력 없이 기존 Codex 로그인으로 Docker에서 호출한다
- 모든 호출에 지정 모델·effort가 전달된다
- 원본 보존, 실행별 워크트리·컨테이너 분리, 외부 네트워크 사용을 실제 검사한다
- 종료 미확인 기록이 있어도 새 로컬 실행을 만들 수 있다
- 손상된 출력·검사 실패·종료 미확인을 성공이나 발행 승인으로 바꾸지 않는다
