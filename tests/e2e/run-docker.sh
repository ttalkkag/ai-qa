#!/bin/sh
set -eu

e2e_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
e2e_container=
cleanup() {
    if [ -n "$e2e_container" ]; then
        docker rm --force "$e2e_container" >/dev/null
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

cd "$e2e_root"
docker build --file tests/e2e/Dockerfile --tag ai-qa-e2e:local .
mkdir -p test-results/e2e
e2e_output=$(mktemp -d "$e2e_root/test-results/e2e/docker-XXXXXXXX")
e2e_container=$(docker create --init --network none --cpus 2 --memory 3g \
    --pids-limit 512 --shm-size 512m --security-opt no-new-privileges:true \
    ai-qa-e2e:local)

e2e_start_status=0
docker start --attach "$e2e_container" || e2e_start_status=$?
e2e_status=$(docker inspect --format '{{.State.ExitCode}}' "$e2e_container")
if [ "$e2e_status" -eq 0 ] && [ "$e2e_start_status" -ne 0 ]; then
    e2e_status=$e2e_start_status
fi
if ! docker cp "$e2e_container:/app/test-results/e2e/." "$e2e_output/"; then
    printf '%s\n' 'E2E 보고서를 복사하지 못했습니다.' >&2
    e2e_status=1
fi
printf '\nE2E 보고서: %s\n' "$e2e_output"
exit "$e2e_status"
