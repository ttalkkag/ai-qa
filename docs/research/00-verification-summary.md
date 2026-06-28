# 00. 리서치 검증 요약

이 문서는 `docs/research/` 자료를 2026-06-27 기준으로 재검증하며 확인한 수정 사항과 판단 기준을 기록합니다. 사용자가 지정한 출발점은 [agentic-workflows 프로덕션 사례 분석](https://github.com/kimchanhyung98/agentic-workflows#%ED%94%84%EB%A1%9C%EB%8D%95%EC%85%98-%EC%82%AC%EB%A1%80-%EB%B6%84%EC%84%9D)이며, 해당 저장소는 참고 자료로만 사용하고 개별 사실은 공식 문서·벤더 원문·공개 GitHub README를 우선 확인했습니다.

## 검증 결론

devauto의 목적은 현재 코드와 외부 사례 모두에서 일관됩니다.

> 로컬 PC에서 비개발자 QA 팀원이 브라우저로 AI에게 QA·버그 수정 초안을 맡기고, 하네스가 범위·권한·검증·승인을 통제해 결과를 사람이 확인한 뒤 발행하는 도구.

이 포지션은 기존 시장과 겹치지 않습니다. 비개발자 AI 앱 빌더는 주로 신규 앱 생성·클라우드 실행에 강하고, 자율 SWE 에이전트는 개발자 검토를 전제로 PR까지 진행하며, 로컬 우선 코딩 에이전트는 개발자 CLI/IDE 사용자를 전제로 합니다. devauto의 차별점은 모델 성능이 아니라 **로컬 우선, 비개발자 UX, 결정론적 게이트, 단계적 승인, 감사 가능한 artifact**입니다.

## 정정한 내용

| 항목 | 기존 서술 | 검증 후 정정 |
|------|-----------|--------------|
| v0 | "프런트엔드만 생성(DB·인증 없음)" | 현재 v0는 Vercel 플랫폼 위에서 full-stack 앱·통합·배포까지 다루는 방향으로 확장됐습니다. "범위가 프런트엔드라 안전하다"는 근거로 쓰면 안 됩니다. |
| GitHub Spark | "Copilot Pro+ 대상"만 언급 | 공식 페이지 기준 Spark는 full-stack 앱 생성, GitHub auth, 호스팅/배포를 제공하며, 현재 요금/가용성은 Copilot Pro+·Enterprise 중심입니다. |
| Replit Agent 사고 후 기능 | "dev/prod DB 분리"를 일반 사양처럼 서술 | 공식 문서로는 Plan 모드, 체크포인트/롤백, 배포·DB 관리 문서를 확인했습니다. 사고 대응 세부사항은 2차 보도/CEO 발언 기반이므로 일반 사양처럼 단정하지 않습니다. |
| Lovable 사고 날짜 | 2025-05 | 공개 보도 기준 Lovable 앱 다수의 개인정보 노출 문제는 2026-04에 보도된 사건으로 정정했습니다. |
| Open SWE 구조 | Manager/Planner/Programmer/Reviewer 고정 역할 | 현재 공개 README는 LangGraph + Deep Agents 기반, cloud sandbox, Slack/Linear/GitHub invocation, subagents, prompt-driven validation을 강조합니다. 고정 역할 구조로 단정하지 않습니다. |
| OpenAI Agents SDK sandbox | "OpenAI Agents SDK가 Daytona/E2B/Modal/Cloudflare/Vercel sandbox provider를 직접 지원" | 공식 OpenAI Codex 문서로 확인되는 것은 sandboxing, approvals, network controls입니다. 외부 sandbox provider 비교는 별도 벤더/2차 자료로 분리했습니다. |
| AI 리뷰 벤치마크 | 검출률을 절대 수치처럼 사용 | Greptile 등 벤더 벤치마크는 방향성 참고만 가능하며 PR gate 설계 상수로 쓰면 안 됩니다. |

## 출처 신뢰도 기준

- 1순위: 공식 문서, 공식 블로그, 공개 GitHub README.
- 2순위: 보안 연구자 글, 기업 엔지니어링 블로그, 벤더 보고서.
- 3순위: 비교 블로그·뉴스·집계 자료. 숫자나 사양은 본문에서 "벤더 주장", "2차 출처", "추정"으로 표시합니다.

## 설계 판단

- AI 리뷰는 보조 신호입니다. PR 검증·보안 검증에서 차단 권한은 `test/build`, secret scan, SAST, SCA 같은 결정론적 gate가 가져야 합니다.
- 비개발자 QA가 승인 주체이므로, "PR 열고 개발자가 리뷰"하는 일반 SWE 에이전트 UX를 그대로 복제하면 제품 목적과 어긋납니다.
- 클라우드 진화는 가능합니다. 다만 실행 위치만 바꾸는 것이 아니라 tenant isolation, egress control, secret vault, audit log, job queue, human approval 모델을 함께 옮겨야 합니다.

## 주요 검증 출처

- GitHub Spark: <https://github.com/features/spark>
- GitHub Copilot cloud agent: <https://docs.github.com/en/copilot/concepts/agents/cloud-agent/about-cloud-agent>
- GitHub Copilot responsible use: <https://docs.github.com/en/copilot/responsible-use/agents>
- OpenAI Codex security: <https://developers.openai.com/codex/agent-approvals-security>
- LangChain Open SWE: <https://github.com/langchain-ai/open-swe>
- Anthropic Building Effective Agents: <https://www.anthropic.com/research/building-effective-agents>
- Stripe Minions: <https://stripe.dev/blog/minions-stripes-one-shot-end-to-end-coding-agents>
- OWASP Top 10 for LLM Applications: <https://owasp.org/www-project-top-10-for-large-language-model-applications/>
- OWASP LLM01 Prompt Injection: <https://genai.owasp.org/llmrisk/llm01-prompt-injection/>
- Veracode GenAI Code Security Report: <https://www.veracode.com/resources/analyst-reports/2025-genai-code-security-report/>
- CVE-2025-53773 write-up: <https://embracethered.com/blog/posts/2025/github-copilot-remote-code-execution-via-prompt-injection/>
