# 결과물 도메인

사용자가 무엇이 바뀌었고 무엇을 실제로 확인했는지 이해하며, 관리자가 정확히 그 결과만 승인할 수 있도록 증거를 보관합니다. 아래는 새 Django 구현 계약이며 기존 결과 파일 저장 코드가 충족했다는 뜻이 아닙니다.

현재 봉인·다운로드·첨부·만료 서비스는 구현했으나 모든 초기 실패의 결과 묶음, inline 이미지 미리보기, 정리 명령/스케줄러·90일 메타데이터 보관은 미완료입니다. 계약을 완료로 오인하지 않도록 [현재 검증 기록](../operations/verification.md)을 함께 확인합니다.

## 1. 사용자에게 보이는 결과

결과 화면의 기본 순서는 `요청 요약 → 변경 요약 → 검증 결과 → 남은 문제 → 파일별 변경 → 패치 내려받기`입니다. 기술 설정·원본 로그·해시는 상세 보기로 둡니다.

- 요약은 요청 revision과 실제 변경 파일을 가리킵니다. AI의 설명과 실제 diff가 다르면 diff·검증 증거가 우선입니다.
- 성공·실패·취소·중단 모두 가능한 범위의 결과를 제공합니다. 실패 보고서가 있다는 이유로 수정 완료라고 표시하지 않습니다.
- `로컬 결과 완료`와 `발행됨`은 별개입니다. push·PR 상태는 [승인·발행](../approvals/README.md)의 실제 결과를 읽어 표시합니다.
- 별도 범용 파일 관리·공개 공유 기능은 만들지 않습니다. 대화에 붙이는 화면 자료와 실행에서 생성된 변경·보고서·로그만 다룹니다.

## 2. 책임과 데이터

결과물 모듈은 안전한 수집, 비밀 제거, 불변 파일 저장, 해시 결속, 권한 있는 조회·다운로드·만료 처리를 소유합니다. [실행](../runs/README.md)은 성공 판정, 승인은 발행 가능 여부를 소유합니다.

| 객체 | 필수 필드·규칙 |
|---|---|
| `ResultBundle` | UUID, run_id, version, request_revision_id, baseline_commit_sha, project_config_version, created_at |
| 결속 정보 | manifest_sha256, result_tree_sha256, completeness, visibility, expires_at, sealed_at |
| `Artifact` | UUID, project_id, owner_id, conversation_id 또는 bundle_id, kind, storage_key, sha256, size_bytes, mime_type, display_name, created_at, expires_at; 첨부와 결과 소속은 구별 |
| `ChangedFile` | bundle_id, 상대 path, ADD/MODIFY/DELETE, 이전/이후 mode와 content hash, binary 여부 |
| `ArtifactAccessEvent` | actor_id, project_id, artifact_id, 조회/다운로드/거부, 시각, 사유; 파일 본문은 기록 안 함 |

- `completeness`는 COMPLETE/INCOMPLETE, `visibility`는 NORMAL/RESTRICTED입니다. RESTRICTED에는 원문 대신 차단 이유를 보여줍니다.
- 파일의 ID나 해시는 접근권한이 아닙니다. UUID를 아는 다른 계정도 [계정 권한](../accounts/README.md)을 통과해야 합니다.
- `version`은 run 안에서 증가하는 결과 묶음 번호입니다. 기존 버전은 덮어쓰지 않습니다. 새 코드 수정·검증은 새 Run이 원칙입니다.
- 봉인 이후 내용을 바꿔 새 버전을 만들면 이전 승인과 연결하지 않습니다. presentation 요약 변경도 새 결과 hash를 만들며 자동 승인 승계가 없습니다.

### 대화 첨부

매니저가 문제 설명에 스크린샷 또는 짧은 로그·재현 설명 파일을 붙일 수 있습니다. `POST /conversations/<id>/attachments/`는 로그인·CSRF·소유권을 검사하고 성공한 비공개 Artifact ID만 반환합니다. 먼저 만든 대화에만 업로드하며 사용자 제공 서버 경로·외부 URL 다운로드·압축 해제는 지원하지 않습니다.

- 초기 허용은 PNG/JPEG 이미지와 UTF-8 `.txt/.log/.md`입니다. 파일당 5 MiB, 한 메시지 3개, 대화 전체 30 MiB입니다. 제한은 저장 전에 streaming 크기 검사로 강제합니다.
- 확장자뿐 아니라 실제 타입을 검사합니다. 이미지 decoder로 유효성과 20 megapixel 상한을 확인하고 EXIF를 제거한 안전한 표시용 이미지를 별도로 만듭니다. SVG·HTML·PDF·문서 매크로·압축·실행 파일은 초기 지원하지 않습니다.
- 텍스트의 비밀 후보와 이미지의 개인정보를 확인하도록 업로드 UI에서 안내합니다. 자동 검사는 모든 비밀을 알아낼 수 없으며 비밀 포함 자료를 입력하지 않는 것이 원칙입니다.
- 파일은 `uploads/<artifact_uuid>`에 불변 저장합니다. 일반 다운로드도 결과 파일과 같은 ACL·no-store·attachment 정책을 따르고 이미지 미리보기는 검증·재인코딩한 데이터만 제공합니다.
- `INPUT_ATTACHMENT`의 ID·hash는 메시지와 RequestRevision에 결속합니다. 같은 ID의 파일 교체는 없고 수정한 자료는 새 업로드입니다. AI 전달 전에도 소속·hash·제한 상태를 다시 확인합니다.
- 일반 대화 첨부는 대화 마지막 활동 후 30일을 기본으로 합니다. 확정 revision/활성 Run/보존 결과가 참조하면 그 기록의 보관 기간까지 pin하고, 만료되면 재실행 전에 새 자료와 새 revision 확인을 요구합니다. 삭제를 과거 명세의 조용한 변경으로 처리하지 않습니다.
- AI가 이미지를 받을 때는 해당 호출용 읽기 사본과 지원되는 CLI 이미지 입력만 사용합니다. 모델이 지원하지 않는 형식이면 설명 텍스트를 요청하고 이미지를 읽었다고 주장하지 않습니다.

## 3. 묶음에 포함할 파일

| kind | 내용 | 사용자 공개 |
|---|---|---|
| `SUMMARY` | 명세·실제 변경·검증·한계의 읽기 쉬운 설명 | 기본 화면 |
| `MANIFEST` | 기준·변경 tree·각 파일·검증 payload의 해시 목록 | 상세 보기 |
| `CANONICAL_PATCH` | 정확한 결과 재구성용 정규 패치 | 안전 검사 통과한 경우 다운로드 |
| `SOURCE_BASELINE` | 원본 committed SHA에 해당하는 독립 기준 tree | trusted publisher 내부 사용, 통째 다운로드 불가 |
| `CHANGED_FILES` | 추가·수정·삭제 경로와 이전/이후 내용 hash | 파일별 변경 |
| `CHECK_REPORT` | BASELINE/AFTER 검사, 실행 여부·개수·상태·제한 | 기본 요약 및 상세 |
| `AI_REVIEW` | 명시적으로 수행한 AI의 보조 의견 | 수행한 경우만 표시 |
| `REDACTED_LOG` | 비밀·제어문자 제거한 단계별 출력 | 권한 있는 상세 조회 |
| `FAILURE_REPORT` | 실패·취소·중단 원인과 확보한 증거 | 실패 결과 화면 |

수행하지 않은 AI 검토·테스트 보고서를 그럴듯하게 생성하지 않습니다. 다운로드 이름은 서버가 만든 `run-<short-id>-v<version>.patch` 형태이며 사용자 입력 경로를 파일명으로 사용하지 않습니다.

## 4. 저장 위치와 신뢰 영역

기본 runtime root는 macOS의 `~/Library/Application Support/AI QA`이며 `AIQA_DATA_DIR`로 변경할 수 있습니다. 여기서 `~`는 앱을 실행하는 OS 사용자의 홈입니다. 실제 앱은 절대 경로로 해석·검사합니다.

```text
<AIQA_DATA_DIR>/
├── db.sqlite3
├── workspaces/<run-id>/             # Codex 수정 사본; 이 아래만 작업용
├── baseline-checks/<run-id>/        # 같은 SHA의 별도 baseline 검사 사본
├── after-checks/<run-id>/           # 최종 후보를 읽기 전용으로 검사하는 사본
├── uploads/<artifact-id>/           # 비공개 대화 첨부
├── artifacts/<run-id>/<version>/    # 봉인된 결과; Codex 접근 금지
│   ├── manifest.json
│   ├── summary.md
│   ├── canonical.patch
│   ├── checks.json
│   ├── baseline/                   # 독립 보존 tree; Git object 공유 없음
│   └── logs/
└── staging/<server-generated-id>/   # trusted 수집 중 임시 결과
```

DB·로그·결과물·자격증명은 수정 사본 안에 두지 않습니다. runtime root 전체를 Codex의 writable root로 주지 않습니다. 디렉터리 분리만으로 읽기·쓰기 차단이 입증되지는 않으므로 [AI 실행 보호](../ai/README.md)의 기술 검증을 통과해야 합니다.

runtime root는 원본 저장소 안쪽·상위의 광범위 경로·symlink 경로를 허용하지 않습니다. 디렉터리 권한 기본값 0700, 파일 0600이며 Django는 비공개 파일을 `STATIC_ROOT`·공개 media URL로 배포하지 않습니다.

## 5. 불변 결과 생성 절차

1. Codex·검사 프로세스 전체가 종료했다는 실행 모듈의 증거를 확인합니다. 살아 있는 프로세스가 쓰는 파일을 최종 결과로 봉인하지 않습니다.
2. 신뢰된 수집기가 실행 사본의 실제 경로·파일 종류·크기를 검사합니다. symlink를 따라가지 않고 탈출 경로·특수 파일·허용되지 않은 변경은 차단합니다. 기준에 있던 허용 symlink는 target 문자열이 불변인지 검사합니다.
3. 최종 tree는 전체 보존 baseline에 허용된 변경만 적용한 tree입니다. 읽기 제외·보호 파일은 baseline 내용으로 보존하며 AI 사본에 없다는 이유로 삭제하지 않습니다. AFTER 검사 전에 고정한 후보 및 CheckResult.checked_source_tree_sha256과 수집 tree가 일치해야 합니다. 추가된 파일도 목록에 포함하며 `.git`·runtime·관리자 지정 생성물 제외 규칙은 실행 전 설정 버전으로 고정합니다.
4. 정규 패치, 변경 파일 목록, 기준 tree, 검사 결과 payload를 staging에 기록합니다. 원본 Git hooks·외부 diff·textconv·credential helper를 실행하지 않습니다.
5. 각 파일의 SHA-256과 최종 tree hash를 계산하고 manifest로 결속합니다. 비밀 탐지·크기·완전성 검사를 수행합니다.
6. 쓰기를 마친 파일을 flush하고 같은 파일시스템에서 새 version 경로로 원자적으로 이동합니다. 경로가 이미 있으면 덮어쓰지 않고 충돌로 실패합니다.
7. 짧은 DB 트랜잭션에서 새 ResultBundle·Artifact 목록과 Run 연결을 저장합니다. DB 저장 실패 시 기존 결과를 수정하지 않고 staging/orphan을 내부 정리 대상으로 표시합니다.

manifest는 UTF-8, 고정 키 순서·정규 JSON 인코딩으로 직렬화하며 SHA-256 계산에는 manifest 자신의 hash 필드를 넣지 않습니다. 생성 시각·난수 등 비결정 값은 별도 메타데이터로 두어 같은 증거의 결속을 재검증할 수 있게 합니다.

manifest의 파일 항목에는 `required_for_publication`과 봉인 시점의 보관 정책을 포함합니다. 기준 tree·정규 패치·변경 목록·검증 payload·요약은 결과 보관 기간 동안 필수이고, redacted log 본문은 만료 가능한 보조 자료입니다. 정상 로그 만료는 별도 `ArtifactExpiry`에 artifact ID·원래 hash·크기·예정/실제 만료 시각·사유를 기록하며 봉인된 manifest를 고치지 않습니다. 검증은 필수 파일 전체와 남아 있는 보조 자료의 hash를 확인하고, 보조 파일 부재는 정상 만료 기록이 있을 때만 허용합니다. 조기 유실·변조·필수 파일 누락은 손상입니다.

최종 tree hash는 정렬된 `(상대 경로, 파일 종류, mode, 내용 SHA-256)` 목록의 hash입니다. 허용된 기존 symlink는 대상 파일이 아니라 link target 문자열의 hash를 사용합니다. 정규 패치는 baseline에 적용했을 때 이 tree를 재현해야 합니다. 삭제·실행 비트·binary 변경도 빠뜨리지 않습니다. 지원하지 않는 파일 형식·변경 유형은 조용히 누락하지 않고 INCOMPLETE로 차단합니다.

## 6. 원문과 표시용 데이터 구분

- `canonical.patch`는 재구성 가능한 정확한 내용입니다. 표시용 diff에 적용한 비밀 마스킹·생략을 정규 패치에 적용하여 다른 코드를 발행하지 않습니다.
- 비밀이 의심되는 정규 패치·추가 파일은 RESTRICTED로 분류하고 다운로드·발행을 차단합니다. 관리자도 위험한 원문을 브라우저에서 확인하는 우회 버튼은 제공하지 않습니다.
- 문제를 해결하려면 비밀을 포함하지 않는 새 실행 결과가 필요합니다. 기존 결과의 문자열만 바꾸어 승인 hash를 유지하지 않습니다.
- 로그는 저장 전에 토큰·Authorization·Cookie·인증 URL·알려진 비밀 키 값을 제거합니다. 원문 로그를 별도 “디버그용” 파일로 저장하지 않습니다.
- streaming chunk 경계에 걸린 비밀도 처리하도록 겹침 버퍼 또는 완성된 논리 레코드 단위로 처리합니다. 단순 chunk별 정규식만 통과했다고 안전 완료로 보지 않습니다.
- 비밀 탐지는 완전한 보장이 아닙니다. 실행 사본에 비밀을 넣지 않는 입력 제한과 파일·자격증명 접근 차단이 선행 조건입니다.
- HTML·Markdown은 스크립트·raw HTML을 실행하지 않는 렌더러로 표시합니다. 로그 ANSI escape·제어문자를 제거하고 소스는 HTML escape합니다.
- AI가 출력한 링크·파일 경로를 서버 파일 열기나 명령 실행으로 연결하지 않습니다. 애플리케이션이 검증한 Artifact ID만 다운로드 대상으로 사용합니다.

## 7. 크기·보관 기본값

| 항목 | 기본 한도/기간 | 초과·만료 처리 |
|---|---|---|
| 화면 diff | 묶음 1 MiB, 파일 200 KiB | 생략 표시와 안전한 전체 패치 다운로드; 전체 검토했다고 표시하지 않음 |
| 정규 패치 | 10 MiB, 변경 파일 500개 | INCOMPLETE, 성공·발행 금지; 요청 범위를 줄여 새 실행 |
| 단계별 redacted log | 10 MiB | 잘린 위치·전체 크기 확인 불가 표시, 관련 검증 증거는 UNVERIFIED |
| 실행 전체 log | 50 MiB | 추가 출력 수집 중단 사유, 증거 불완전으로 성공 금지 |
| 수정·baseline·AFTER 검사 사본 | 실행 종료 후 7일 | 활성 프로세스 없음 확인 후 명시된 정책으로 정리 |
| redacted log | 실행 종료 후 7일 | 본문 제거, ID·hash·크기·만료 사유 보존 |
| 결과 묶음·보존 baseline | 종료 후 30일 | 승인 대기·발행 중이면 보류, 아니면 만료 처리 후 본문 정리 |
| 감사·결과 메타데이터 | 90일 | 내용 없는 감사 기록 보관, 설정된 정리 주기에 삭제 |

보관 정책은 관리자 설정 화면과 결과의 만료일로 공개하고 설정 버전을 기록합니다. 임의로 더 오래 숨겨 보관하지 않습니다. 승인 대기·미해결 발행은 정책에 따라 pin하며, PUBLISHED/REJECTED/INVALIDATED에는 미해결 작업이 없는지 확인하고 pin을 해제합니다. PARTIAL은 승인 도메인의 남은 단계 완료 또는 명시적 포기 기록이 필요하고 UNKNOWN은 원격 사실이 확인되기 전에는 해제하지 않습니다. pin 해제는 즉시 삭제가 아니라 기존 만료 정책으로 복귀하는 동작입니다.

결과 보관 중 로그가 먼저 만료되어도 봉인 시점의 검증 payload·hash와 `로그 만료` 상태는 남습니다. 로그를 다시 읽어야만 증명 가능한 불완전 검증은 최초부터 발행 가능한 결과가 아닙니다. 저장 공간 부족은 파일을 잘라 성공 처리하지 않고 수집 실패로 보고합니다.

## 8. Interface와 다운로드 권한

구현은 `apps/artifacts/`에 둡니다. 파일 저장 경로나 임의 읽기 기능을 view·모델 프롬프트에 노출하지 않습니다.

| Interface | 책임 |
|---|---|
| `seal_result(run_id, evidence)` | worker가 전달한 증거와 실제 파일을 검증하여 새 불변 bundle 생성 |
| `store_attachment(actor, conversation_id, uploaded_file)` | 종류·크기·소유권 검사 후 불변 입력 첨부 ID 반환 |
| `get_result(actor, bundle_id)` | 현재 계정·프로젝트·요청 접근권한을 검사한 요약 반환 |
| `open_artifact(actor, artifact_id)` | 권한·visibility·만료·경로·hash 확인 후 제한된 파일 스트림 반환 |
| `verify_bundle(bundle_id, expected_hash)` | trusted 발행 경로가 manifest·필수 내용·기준과 보조 자료의 hash/정상 만료 기록을 재검증; 사용자 권한 검사는 발행 도메인이 별도로 수행 |
| `expire_results(now)` | 정해진 보관 정책·pin·프로세스 종료 확인 후 만료 처리 |

- `GET /results/<bundle-id>/`와 `GET /artifacts/<artifact-id>/download/`는 매번 로그인 상태와 객체 권한을 검사합니다. 목록에서 숨기는 것만으로 보호하지 않습니다.
- 요청 소유자 또는 해당 프로젝트에서 허용된 관리자가 접근합니다. 같은 네트워크·기기·OS 사용자 여부는 권한에 영향을 주지 않습니다.
- 계정 비활성화·membership 회수는 이미 알고 있는 다운로드 URL에도 즉시 적용합니다. 비인가 객체는 존재 여부를 드러내지 않는 404로 처리합니다.
- 다운로드 응답은 `Content-Disposition: attachment`, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`를 사용합니다. 미디어 MIME 추측으로 HTML 실행을 허용하지 않습니다.
- 서버가 저장한 상대 storage_key만 해석합니다. run ID·파일명에 `..`, 절대 경로, 제어문자, symlink를 끼워 넣어 다른 파일을 읽을 수 없어야 합니다.
- 열기 직전에 no-follow 방식과 실제 파일 descriptor의 종류·경로를 검증합니다. 먼저 resolve하고 나중에 여는 사이에 파일을 바꾸는 경합도 거부해야 합니다.
- 파일 hash가 DB·manifest와 다르면 다운로드·발행을 차단하고 손상 사건을 기록합니다. 화면에 이전 성공 badge만 남기지 않고 “결과 손상, 사용 불가”를 표시합니다.

## 9. 사용자 결과 확인과 관리자 로컬 승인

`ResultBundle`은 봉인된 불변 증거이며, `ResultReview`는 별도 변경 가능한 검토 상태입니다. AI 최종 작업 보고와 앱이 수집한 실제 검사 결과를 구분하여 표시합니다.

- `POST /results/<bundle_id>/review/`: `accept`, `rework`, `approve`, `reject`
- 본인 결과의 `accept` → `AWAITING_ADMIN`, 본인 `rework` → `CHANGES_REQUESTED`와 새 초안
- 관리자 `approve` → `APPROVED`, 관리자 `reject` → `REJECTED` (보완 사유 필수)
- 관리자도 요청자를 대신해 완료 확인할 수 없습니다. 같은 프로젝트의 다른 매니저도 결정할 수 없습니다
- 결과 해시, 최신 실행/초안, COMPLETE·NORMAL·SUCCEEDED, 파일 보존/무결성을 서버에서 확인합니다
- 검토 해시·계정·시각·사유를 DB에 기록합니다. 동일 결정 재전송은 중복 초안을 만들지 않습니다
- 이전 결과/명세는 덮어쓰지 않습니다. 새 초안이나 실행이 생기면 과거 결과를 새로 승인할 수 없습니다
- 로컬 승인은 Job이나 원격 작업을 만들지 않습니다. 결과 보존은 기존 만료일을 따르며 만료 결과는 승인할 수 없습니다

## 10. 선택적 원격 발행과 연결

사용자 확인과 관리자 로컬 승인을 마친 최신 결과만 발행 검토를 준비할 수 있습니다. 발행 worker도 이 조건을 다시 확인합니다.

승인은 `result_bundle_id + version + manifest_sha256 + baseline_commit_sha + project_config_version`에 결속됩니다. COMPLETE·NORMAL이며 [실행](../runs/README.md)의 필수 검사 기준을 충족한 SUCCEEDED 결과만 발행 후보입니다.

발행은 수정 사본을 믿고 push하지 않습니다. trusted 발행 경로가 보존 baseline과 정규 패치로 새 tree를 구성하고 result_tree_sha256을 대조합니다. 원격 기준 branch SHA가 baseline과 다르면 기존 승인을 무효화하고 새 기준으로 요청을 확인합니다. 자동 rebase·원본 미발행 커밋 동반 전송은 없습니다.

결과 수정·손상·만료·비밀 탐지·권한 회수·발행 대상 변경은 승인 재사용을 금지합니다. 같은 승인으로 재전송한 요청의 중복 부작용 방지는 승인 도메인의 idempotency와 실제 원격 결과 확인으로 처리합니다.

## 10. 결정 사항

- **ART-01:** 결과는 run과 버전에 묶인 불변 증거입니다. DB 행이나 같은 파일명을 덮어써 이전 승인을 유지하지 않습니다.
- **ART-02:** canonical 데이터와 표시용 데이터는 별개입니다. 생략·마스킹한 화면을 정확한 패치·전체 검증 증거라고 주장하지 않습니다.
- **ART-03:** 소스·패치·로그는 비공개이며 계정·프로젝트·요청 권한을 매번 검사합니다. 공개 media 경로는 사용하지 않습니다.
- **ART-04:** 결과 원문에 비밀이 의심되면 다운로드·발행을 차단합니다. 원문 로그를 별도로 보존하지 않습니다.
- **ART-05:** 보관기간·크기는 유한하며 만료·잘림을 표시합니다. 활성 실행·승인·발행의 증거를 자동 정리하지 않습니다.

## 11. 인수 조건

- [ ] **ART-AC-01:** 결과 파일 하나·검증 payload 하나·baseline 하나의 변조도 manifest 검증에서 탐지되고 다운로드·발행이 거부된다.
- [ ] **ART-AC-02:** 삭제·추가·binary·실행 비트 변경을 포함한 정규 패치를 독립 baseline에 적용하면 결과 tree hash가 동일하다.
- [ ] **ART-AC-03:** 파일과 DB 저장 사이 crash·디스크 부족·동일 version 경합에서 불완전 bundle을 성공 결과로 노출하지 않는다.
- [ ] **ART-AC-04:** 다른 매니저, membership 회수, 비활성 계정의 ID 추측·직접 다운로드가 거부되며 파일 본문이 유출되지 않는다.
- [ ] **ART-AC-05:** path traversal·symlink 교체·특수 파일·악성 파일명·HTML/ANSI 로그로 파일 읽기·스크립트 실행이 발생하지 않는다.
- [ ] **ART-AC-06:** 토큰·인증 URL·Cookie·chunk 경계 비밀이 로그에 남지 않고 canonical 비밀 탐지 시 다운로드·발행을 막는다.
- [ ] **ART-AC-07:** 화면 생략·로그 잘림·unknown 테스트 개수·미실행을 완료된 전체 증거로 표시하지 않는다.
- [ ] **ART-AC-08:** 7일/30일/90일 보관 정책, pin, 만료 URL, 실행 중 정리 거부를 시간 고정 테스트로 검증한다.
- [ ] **ART-AC-09:** 결과 버전·대상·권한 변경 후 과거 승인을 재사용할 수 없고 현재 수정 사본의 사후 변경이 발행 내용에 섞이지 않는다.
- [ ] **ART-AC-10:** 악성·과대·타인 첨부를 거부하고, 정상 스크린샷·텍스트의 업로드→대화→확정 revision→AI 입력 hash가 동일하게 연결된다. 첨부 만료·pin도 검증한다.
- [ ] **ART-AC-11:** 제한된 읽기 사본의 비노출 파일은 최종 tree에 그대로 보존되고, AFTER 검사 hash와 다른 후보는 봉인할 수 없다.
- [ ] **ART-AC-12:** 8일차의 정상 보조 로그 만료와 조기 유실을 구별한다. 전자는 필수 payload가 온전하면 30일 결과 검증을 유지하고, 후자는 다운로드·발행을 차단한다.

## 12. 기존 코드와의 관계

`devauto/runner/artifacts.py`의 파일명 검사·경로 검사·비밀 제거는 참고할 수 있습니다. 현재 같은 경로에 다시 쓰는 동작, 정규식만의 비밀 보장, hash·버전·계정 접근검사 없는 저장을 새 계약으로 인정하지 않습니다. 기존 `GateResult`의 exit code만으로는 실제 테스트 수·미실행·증거 완전성을 표현할 수 없으므로 위 데이터와 Interface 검증이 필요합니다.
