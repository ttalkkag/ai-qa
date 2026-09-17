# 더미 게시판으로 수정 요청 검증

단순 화면 조회를 넘어 관리자 준비 → 매니저 명세 확인 → 수정 접수 → worker 처리 → 상태 확인·재시도를 검증하는 시나리오다. 실제 실행 결과는 [검증 기록](verification.md)에 남긴다.

## 1. 시험 프로젝트와 요청

원본 템플릿은 [tests/fixtures/board](../../tests/fixtures/board/README.md)에 있으며, [게시판 기획서](../../tests/fixtures/board/docs/requirements.md)가 작업 요구사항이다. 목록·상세를 제공하는 Python 표준 라이브러리 WSGI 앱이고, 새 요청은 **제목·본문 검색**이다.

검색은 원본 템플릿에 의도적으로 구현하지 않는다. 기존 동작 검사 `baseline_checks.py`와 검색 완료 검사 `change_checks.py`를 나눠, 기존 테스트 통과를 요청 해결로 잘못 판단하지 않게 한다. 작업 사본의 `board.py`만 수정 대상으로 지정하고 기획서·검사기는 읽기 전용 입력으로 둔다. 실제 사용자 저장소·데이터·원격 저장소를 사용하지 않는다.

각 검사는 실제 WSGI 응답 또는 실제 HTTP 응답을 검증하고, 실제 assertion 결과로 JUnit과 종료 코드를 만든다. `--transport http`는 loopback 서버를 사용한다. 기본 제품 검증 명령에는 별도 서버가 필요 없는 `--transport wsgi`를 등록한다. WSGI 검사 통과만으로 브라우저·네트워크 동작을 검증했다고 표현하지 않는다.

## 2. 자동 검사

```sh
uv sync --locked --extra dev --python 3.11
npm ci --ignore-scripts
.venv/bin/python -m pytest tests/test_board_fixture.py tests/django/test_board_scenario.py -q
npm exec -- playwright install chromium firefox webkit
npm run test:e2e
```

Docker에서는 `make e2e-docker`로 같은 브라우저 검사를 실행한다. 테스트 설정·인증 경계는 [테스트 운영 안내](testing.md)를 따른다.

- fixture 회귀: 기존 기능 통과, 의도적으로 손상한 독립 사본의 실패, 미구현 검색의 실패, 기존 디렉토리 덮어쓰기 거부.
- Django 시나리오: 실제 Git·계정·프로젝트·기획서·확정 명세, 중복 제출, worker 차단, 별도 재시도, 원본·명세 불변, 역할 회수와 오래된 코드/설정 기준 거부, 등록된 JUnit 형식과 실제 증거의 연결.
- Playwright 시나리오: 관리자 로그인·프로젝트 등록·작업 정책·매니저 할당, 매니저 로그인·기획서 첨부·초안 저장·명세 확인·수정 시작·상태 polling·재시도. 기존 합성 결과 다운로드 시험과 별개의 실제 요청/Run을 생성한다.

자동 브라우저 시험은 Docker를 사용할 수 없는 격리 환경에서 실제 worker의 `QUEUED → PREPARING → FAILED / DOCKER_UNAVAILABLE`와 재시도를 확인한다. 성공 결과를 seed하거나 실제 모델 수정 성공으로 보고하지 않는다.

## 3. 자동 검사 후 직접 앱 실행

기존 Codex 로그인이 있는 Mac에서 다음 명령을 실행한다. 루트 프로젝트 커밋·push는 하지 않으며 더미 사본 안에서만 기준 Git commit을 만든다.

```sh
.venv/bin/python tests/e2e/serve-board-demo.py
```

launcher가 출력하는 내용을 확인한다.

| 출력 | 용도 |
| --- | --- |
| `App` | 실제 Django 앱의 로그인 URL |
| `Board` | 현행 게시판 URL |
| `Username` | 최초 관리자 `demo-admin` |
| `Credentials (0600)` | 이번 임시 앱에만 유효한 무작위 관리자 비밀번호 파일 |
| `Manifest (0600)` | 더미 Git 경로·기획서 위치·검증 명령·모델 CLI 경로·자식 PID |

항상 별도 private 임시 runtime을 생성하고 실제 Gunicorn·worker·게시판을 기동한다. 포트를 생략하면 빈 loopback 포트를 선택한다. 기존 서버나 운영 DB를 재사용하지 않는다. 최초 관리자만 만들며 프로젝트·매니저·멤버십·명세·Run·샌드박스 통과 결과는 미리 만들지 않는다. 관리자 비밀번호를 소스·문서·스크린샷에 기록하지 않는다. 앱은 로컬 Codex 로그인에 필요한 허용 환경만 상속하고, 게시판 서버는 별도의 빈 HOME·TMPDIR와 인증정보 없는 최소 환경을 사용한다.

### 관리자 화면

1. 출력된 게시판 URL에서 목록·상세와 검색 입력이 없는 현행을 확인한다.
2. App URL에서 `demo-admin`으로 로그인한다.
3. `프로젝트 추가`에서 manifest의 `source` 경로, 기준 `main`, 프로젝트 이름을 입력하고 등록 범위를 확인한다.
4. 작업 정책의 읽기 범위는 `.`, 수정 범위는 `board.py`로 지정한다. manifest의 `verification_command`를 JSON 배열로 입력하고 오프라인 준비 목록에는 **JSON 배열 `[]`**를 명시한다. 검사 명령 등록과 실제 검사 실행은 다르다.
5. 계정 관리에서 별도 MANAGER 계정과 임시 비밀번호를 만들고 게시판 프로젝트에 할당한다. 비밀번호 재설정·역할 변경은 현재 계정 버전을 사용한다.
6. AI 연결 화면에서 기존 CLI·로그인 확인과 Docker 환경 점검을 실행한다. 필요하면 `gpt-6-astra / xhigh` 등 표시된 프로필을 확인하되 검증 상태를 수동으로 바꾸지 않는다.

### 매니저 화면

1. 관리자 로그아웃 후 새 매니저 계정으로 로그인한다. 초기 비밀번호 변경을 요구하면 변경하고 다시 로그인한다.
2. 게시판 프로젝트에서 새 대화를 시작하고 검색 문제·기대 동작을 설명한다. manifest의 기획서를 첨부한다.
3. `요청으로 정리`에서 목표·현재/기대 동작·재현·완료 조건·수정/제외 범위를 작성해 초안을 저장한다.
4. `이 명세로 준비`를 눌러 불변 명세를 확인한다. 이 시점에는 수정이 실행되지 않는다.
5. `수정 작업 시작`을 누르고 Run ID와 상태 전이를 확인한다. 실패는 원인 코드와 함께 기록하고 결과 다운로드 링크를 만들어 성공처럼 표시하지 않는다.
6. 기록을 다시 열어 `새 실행으로 재시도`를 누르면 새 Run ID가 생성되는지, 이전 명세·실행·원본 코드가 유지되는지 확인한다. 현재 종료 polling은 재시도 폼을 새로 만들지 않으므로 실패 후 페이지를 다시 열어야 한다.

## 4. 판정과 종료

현재 기본 실행은 Docker 안에서 기존 Codex 파일 로그인을 사용한다. Docker 이미지가 준비되어야 하며 모델·effort는 실제 검증한다. 예전 SANDBOX_UNAVAILABLE 기록은 이전 실행 계약의 이력이다. 실제 모델 worktree 시험과 전체 게시판 웹 요청 성공은 별도 검증으로 구분한다.

새 통합 시험에서 발견한 JUnit 형식 불일치는 사용자 승인 후 수정했다. 실행기는 등록 형식 `junit-xml`과 기존 `junit`·`junit-v1`을 지원하며, 보고서 안전성 검사를 그대로 적용한다. 정상/손상 보고서의 실제 판정과 최종 결과는 [수정 검증 기록](verification.md#9-junit-형식-불일치-수정)에 구분한다. 이 수정은 Codex 실행 보호 조건을 바꾸지 않는다.

확인 후 launcher 터미널에서 `Ctrl+C`로 종료한다. 자신의 서비스 프로세스만 정리하며 runtime·로그·더미 원본·manifest는 증거로 보존한다. manifest의 `source_integrity`와 `cleanup_failures`를 확인한다. 이 보존 경로에는 임시 로그인 정보가 있으므로 외부에 업로드하지 않는다.
