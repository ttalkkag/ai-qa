.PHONY: help check init e2e e2e-docker

.DEFAULT_GOAL := help

help: ## 사용 가능한 명령어 목록 출력
	@awk 'BEGIN {FS = ":.*##"; printf "\n사용법:\n  make \033[36m<target>\033[0m\n\n명령어:\n"} /^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

check: ## Python 테스트 실행
	@.venv/bin/python -m pytest -q

e2e: ## 설치된 Playwright 브라우저로 웹 E2E 실행 (실제 AI 미사용)
	@npm run test:e2e

e2e-docker: ## 격리된 Docker에서 3개 브라우저 웹 E2E 실행 (실제 AI 미사용)
	@sh tests/e2e/run-docker.sh

init: ## 프로젝트 환경 설정
	@uv venv --allow-existing
	@uv pip install --python .venv/bin/python -e '.[dev]'
	@npm install
