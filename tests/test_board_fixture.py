from pathlib import Path
import selectors
import socket
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import urlopen
from xml.etree import ElementTree

import pytest

from tests.e2e.board_fixture import prepare_board


def run_board_check(tmp_path, script, transport, *, damage=False):
    fixture = prepare_board(tmp_path / "source")
    source = Path(fixture["source"])
    if damage:
        board = source / "board.py"
        board.write_text(board.read_text(encoding="utf-8").replace("함께 쓰는 게시판", "의도적으로 손상한 제목"), encoding="utf-8")
    report = tmp_path / "evidence" / "report.xml"
    result = subprocess.run(
        [sys.executable, "-I", script, "--transport", transport, "--junit", str(report)],
        cwd=source, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "empty-home")},
        text=True, capture_output=True, timeout=30,
    )
    assert report.exists(), result.stderr
    return result, ElementTree.parse(report).getroot()


@pytest.mark.parametrize("transport", ["http", "wsgi"])
def test_board_baseline_measures_existing_functionality(tmp_path, transport):
    result, suite = run_board_check(tmp_path, "baseline_checks.py", transport)
    assert result.returncode == 0, result.stderr
    assert int(suite.attrib["tests"]) == len(suite.findall("testcase")) == 6
    assert int(suite.attrib["failures"]) == int(suite.attrib["errors"]) == int(suite.attrib["skipped"]) == 0


@pytest.mark.parametrize("transport", ["http", "wsgi"])
def test_board_baseline_catches_a_damaged_copy(tmp_path, transport):
    result, suite = run_board_check(tmp_path, "baseline_checks.py", transport, damage=True)
    assert result.returncode == 1
    assert int(suite.attrib["tests"]) == 6
    assert int(suite.attrib["failures"]) == len(suite.findall("testcase/failure")) == 1
    assert "AssertionError" in suite.find("testcase/failure").text


@pytest.mark.parametrize("transport", ["http", "wsgi"])
def test_search_acceptance_detects_the_intentionally_missing_feature(tmp_path, transport):
    result, suite = run_board_check(tmp_path, "change_checks.py", transport)
    assert result.returncode == 1
    assert int(suite.attrib["tests"]) == 6
    assert int(suite.attrib["failures"]) == len(suite.findall("testcase/failure")) > 0
    assert int(suite.attrib["errors"]) == int(suite.attrib["skipped"]) == 0


def test_board_fixture_never_overwrites_an_existing_directory(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_text("user-owned", encoding="utf-8")
    with pytest.raises(FileExistsError):
        prepare_board(existing)
    assert marker.read_text(encoding="utf-8") == "user-owned"


def test_board_cli_serves_requests_while_another_connection_is_idle(tmp_path):
    fixture = prepare_board(tmp_path / "source")
    process = subprocess.Popen(
        [sys.executable, "-I", "board.py", "--port", "0"],
        cwd=fixture["source"], env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "empty-home")},
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5), "게시판 CLI가 시작 URL을 출력하지 않았습니다"
        origin = process.stdout.readline().split("게시판: ", 1)[1].strip()
        location = urlsplit(origin)
        assert location.hostname == "127.0.0.1"
        with socket.create_connection((location.hostname, location.port), timeout=3):
            with urlopen(origin + "posts/2/", timeout=3) as response:
                assert response.status == 200
                assert "주말 도서 모임" in response.read().decode("utf-8")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
