# 동네 게시판 — 로컬 수정 검증용 더미 프로젝트

개인정보·실제 계정·외부 서비스·외부 의존성이 없는 Python 표준 라이브러리 WSGI 게시판입니다. AI Debug의 원본 보존, 명세 확정, 코드 변경, 검증 결과 수집을 시험할 입력 저장소이며 제품 앱이 아닙니다.

## 현재 구현과 개선 요청

현재는 합성 게시글 3개의 목록과 상세만 구현되어 있습니다. HTML 문자열은 실행하지 않고 글자로 표시하며, 없는 경로는 404, GET 이외 요청은 405입니다. 데이터 생성·수정·삭제, 로그인, DB, 검색은 없습니다.

[기획서](docs/requirements.md)의 제목·본문 키워드 검색은 **아직 구현하지 않은 개선 요청**입니다. `baseline_checks.py` 통과는 기존 목록·상세·오류·HTML 이스케이프가 정상이라는 뜻이지 검색 구현이나 AI 수정 성공을 뜻하지 않습니다.

현재 baseline 검사는 **6개 통과**이며, 검색 완료 기준인 `change_checks.py`는 **6개 중 5개 실패(의도한 RED)**입니다. 빈 검색어의 전체 목록 표시 1개만 기존 기능으로 통과합니다. 검색 검사의 실패를 정상 동작이나 검색 완료로 보고하지 않습니다.

## 실행

Python 3.11 이상에서 이 디렉토리를 작업 디렉토리로 사용합니다.

```sh
python3 -I board.py --port 8099
```

브라우저에서 `http://127.0.0.1:8099/`를 엽니다. 호스트는 `127.0.0.1`로 고정되어 같은 컴퓨터에서만 접근합니다. 사용 중인 포트라면 다른 포트를 지정하고, 종료는 `Ctrl+C`입니다. `--port 0`은 OS가 빈 포트를 선택하며 실제 URL을 출력합니다.

## 현행 기능 검증

```sh
python3 -I baseline_checks.py --junit .aiqa-output/board-baseline/junit.xml
```

기본 `--transport http`는 실제 루프백 HTTP 서버에 요청합니다. `--transport wsgi`는 네트워크·서버 바인딩 없이 같은 `board.application`의 WSGI 인터페이스를 직접 호출하여 실제 상태·헤더·본문을 검사합니다. 어느 방식도 응답이나 성공 결과를 mock으로 대체하지 않습니다. 두 방식 모두 현재 baseline 6개가 통과합니다. 테스트 결과로 JUnit XML을 생성하고 실패·실행 오류가 있으면 종료 코드 1을 반환합니다. 출력 경로의 상위 디렉토리는 자동 생성됩니다. `.aiqa-output/`과 `__pycache__/`는 대상 저장소에서 제외합니다.

AI Debug의 native 샌드박스는 네트워크·루프백 바인딩도 차단하므로 등록할 검증 명령에는 `--transport wsgi`를 지정합니다. 실행 파일은 관리자가 선택한 절대 Python 경로로 교체할 수 있습니다. WSGI 검증 통과를 실제 HTTP 서버·브라우저 검증으로 해석하지 않고, HTTP 기본 명령과 브라우저 검증은 샌드박스 밖의 별도 로컬 검사로 수행합니다.

```json
{
  "argv": ["python3", "-I", "baseline_checks.py", "--transport", "wsgi", "--junit", ".aiqa-output/board-baseline/junit.xml"],
  "parser_id": "junit-xml",
  "report_path": ".aiqa-output/board-baseline/junit.xml"
}
```

## 검색 개선 완료 기준 검증

```sh
python3 -I change_checks.py --junit .aiqa-output/board-search/junit.xml
python3 -I change_checks.py --transport wsgi --junit .aiqa-output/board-search-wsgi/junit.xml
```

제목·본문 검색, 대소문자·공백 처리, 빈 검색어, 일치 결과 없음, HTML 검색어 이스케이프를 실제 HTTP 또는 WSGI 응답으로 검사합니다. 아직 미구현인 현재 코드에서는 두 방식 모두 6개 중 5개 실패와 종료 코드 1이 예상됩니다. AI Debug 프로젝트의 필수 baseline 명령은 위의 `baseline_checks.py --transport wsgi`로 유지하고, 검색 개선 후에는 이 독립 수용 검사까지 통과해야 검색 완료로 판단합니다. 수정 허용 파일은 작업 사본의 `board.py`뿐이며, 두 검사기는 읽기 전용으로 유지합니다. 두 검사 모두 통과하더라도 실제 AI 수정이 이루어졌다는 증거는 별도의 실행 기록으로 확인합니다.

## 원본 보존

이 fixture 자체를 수정 작업 대상으로 등록하지 않습니다. 별도 임시 디렉토리로 복사한 뒤 독립적인 로컬 Git 저장소의 기준 커밋을 만들고 그 복사본을 프로젝트로 등록합니다. 원격 저장소는 설정하지 않으며 push·PR·배포는 시험 범위에 없습니다. 검색 개선은 AI Debug가 만든 작업 사본에만 적용하고, 원본 기준 커밋과 이 fixture는 유지합니다.
