import argparse
from email.message import Message
import importlib.util
from io import BytesIO
from pathlib import Path
import sys
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.response import addinfourl
from wsgiref.simple_server import make_server
from wsgiref.util import setup_testing_defaults
from xml.etree import ElementTree


class BoardHTTPChecks(unittest.TestCase):
    transport = "http"

    @classmethod
    def setUpClass(cls):
        module_path = Path(__file__).resolve().with_name("board.py")
        spec = importlib.util.spec_from_file_location("board", module_path)
        board = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(board)
        cls.application = staticmethod(board.application)
        cls.origin = "http://127.0.0.1"
        if cls.transport == "http":
            cls.server = make_server("127.0.0.1", 0, board.application)
            cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
            cls.thread.start()
            cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        if cls.transport == "http":
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join(timeout=5)

    def request(self, path, method="GET", data=None):
        url = self.origin + path
        if self.transport == "http":
            return urlopen(Request(url, data=data, method=method), timeout=5)

        location = urlsplit(path)
        environ = {}
        setup_testing_defaults(environ)
        environ.update({
            "REQUEST_METHOD": method,
            "PATH_INFO": location.path,
            "QUERY_STRING": location.query,
            "CONTENT_LENGTH": str(len(data or b"")),
            "wsgi.input": BytesIO(data or b""),
        })
        response = {}
        body_parts = []

        def start_response(status, headers, exc_info=None):
            if exc_info:
                raise exc_info[1].with_traceback(exc_info[2])
            response["status"] = status
            response["headers"] = headers
            return body_parts.append

        chunks = self.application(environ, start_response)
        try:
            body_parts.extend(chunks)
        finally:
            if hasattr(chunks, "close"):
                chunks.close()
        body = b"".join(body_parts)
        status, _, reason = response["status"].partition(" ")
        headers = Message()
        for name, value in response["headers"]:
            headers[name] = value
        if int(status) >= 400:
            raise HTTPError(url, int(status), reason, headers, BytesIO(body))
        return addinfourl(BytesIO(body), headers, url, int(status))


class BoardBaselineChecks(BoardHTTPChecks):
    def test_list_contains_three_posts(self):
        with self.request("/") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_charset(), "utf-8")
            body = response.read().decode("utf-8")
        self.assertIn("함께 쓰는 게시판", body)
        for post_id in (1, 2, 3):
            self.assertIn(f'href="/posts/{post_id}/"', body)

    def test_post_detail_contains_title_body_and_list_link(self):
        with self.request("/posts/2/") as response:
            body = response.read().decode("utf-8")
        self.assertIn("주말 도서 모임", body)
        self.assertIn("토요일 오전에 함께 책을 읽습니다.", body)
        self.assertIn('href="/"', body)

    def test_missing_post_returns_404(self):
        with self.assertRaises(HTTPError) as raised:
            self.request("/posts/999/")
        with raised.exception as response:
            self.assertEqual(response.code, 404)
            self.assertIn("게시글을 찾을 수 없습니다", response.read().decode("utf-8"))

    def test_board_rejects_writes(self):
        with self.assertRaises(HTTPError) as raised:
            self.request("/", data=b"title=unexpected", method="POST")
        with raised.exception as response:
            self.assertEqual(response.code, 405)
            self.assertEqual(response.headers["Allow"], "GET")

    def test_unknown_route_returns_404(self):
        with self.assertRaises(HTTPError) as raised:
            self.request("/unknown/")
        with raised.exception as response:
            self.assertEqual(response.code, 404)

    def test_post_body_is_escaped(self):
        with self.request("/posts/3/") as response:
            body = response.read().decode("utf-8")
        self.assertIn("&lt;script&gt;alert(&quot;게시판&quot;)&lt;/script&gt;", body)
        self.assertIn("&amp; &lt;b&gt;강조&lt;/b&gt;", body)
        self.assertNotIn("<script>", body)


class JUnitResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = {}

    def startTest(self, test):
        self.cases[test.id()] = {"started": time.monotonic(), "elapsed": 0.0}
        super().startTest(test)

    def stopTest(self, test):
        case = self.cases[test.id()]
        case["elapsed"] = time.monotonic() - case["started"]
        super().stopTest(test)

    def addError(self, test, err):
        self.cases.setdefault(test.id(), {"elapsed": 0.0})
        super().addError(test, err)

    def write_junit(self, report_path, suite_name="board-baseline"):
        suite = ElementTree.Element("testsuite", {
            "name": suite_name,
            "tests": str(len(self.cases)),
            "failures": str(len(self.failures)),
            "errors": str(len(self.errors)),
            "skipped": str(len(self.skipped)),
            "time": f'{sum(case["elapsed"] for case in self.cases.values()):.6f}',
        })
        failures = {test.id(): traceback for test, traceback in self.failures}
        errors = {test.id(): traceback for test, traceback in self.errors}
        skipped = {test.id(): reason for test, reason in self.skipped}
        for test_id, result in self.cases.items():
            classname, _, name = test_id.rpartition(".")
            case = ElementTree.SubElement(suite, "testcase", {
                "classname": classname,
                "name": name or test_id,
                "time": f'{result["elapsed"]:.6f}',
            })
            for outcome, messages in (("failure", failures), ("error", errors), ("skipped", skipped)):
                if test_id in messages:
                    ElementTree.SubElement(case, outcome).text = messages[test_id]
        destination = Path(report_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        ElementTree.ElementTree(suite).write(destination, encoding="utf-8", xml_declaration=True)


def run_checks(test_case, suite_name, argv=None):
    parser = argparse.ArgumentParser(description="게시판 기능을 실제 HTTP 또는 WSGI 인터페이스로 검증합니다")
    parser.add_argument("--junit", help="실제 검사 결과를 저장할 JUnit XML 경로")
    parser.add_argument("--transport", choices=("http", "wsgi"), default="http")
    args = parser.parse_args(argv)
    test_case.transport = args.transport
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(test_case)
    result = unittest.TextTestRunner(verbosity=2, resultclass=JUnitResult).run(suite)
    if args.junit:
        result.write_junit(args.junit, suite_name)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run_checks(BoardBaselineChecks, "board-baseline"))
