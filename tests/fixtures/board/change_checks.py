from html.parser import HTMLParser
import importlib.util
from pathlib import Path
import re
import sys
from urllib.parse import urlencode


spec = importlib.util.spec_from_file_location("board_baseline_checks", Path(__file__).with_name("baseline_checks.py"))
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


class SearchPage(HTMLParser):
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.posts = []
        self.tags = []
        self.inputs = []
        self.buttons = []
        self.active_button = None
        self.labels = {}
        self.label_for = None
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if tag == "a" and re.fullmatch(r"/posts/[0-9]+/", attributes.get("href", "")):
            self.posts.append(attributes["href"])
        if tag == "input" and attributes.get("name") == "q":
            self.inputs.append(attributes)
        if tag == "button":
            self.active_button = {"attributes": attributes, "text": ""}
            self.buttons.append(self.active_button)
        if tag == "label":
            self.label_for = attributes.get("for")
            self.labels[self.label_for] = ""

    def handle_data(self, data):
        if self.label_for is not None:
            self.labels[self.label_for] += data
        if self.active_button is not None:
            self.active_button["text"] += data

    def handle_endtag(self, tag):
        if tag == "label":
            self.label_for = None
        if tag == "button":
            self.active_button = None


class BoardSearchChecks(baseline.BoardHTTPChecks):
    def search(self, query):
        with self.request("/?" + urlencode({"q": query})) as response:
            self.assertEqual(response.status, 200)
            body = response.read().decode("utf-8")
        return body, SearchPage(body)

    def test_search_matches_title_and_keeps_labeled_input(self):
        body, page = self.search("도서")
        self.assertEqual(page.posts, ["/posts/2/"])
        self.assertEqual(len(page.inputs), 1)
        self.assertEqual(page.inputs[0].get("value"), "도서")
        self.assertEqual(page.labels.get(page.inputs[0].get("id"), "").strip(), "검색어")
        self.assertTrue(any(button["text"].strip() == "검색" and button["attributes"].get("type", "submit") == "submit" for button in page.buttons))

    def test_search_matches_body(self):
        _, page = self.search("토요일")
        self.assertEqual(page.posts, ["/posts/2/"])

    def test_search_ignores_case_and_outer_whitespace(self):
        for query, expected in ((" hTmL ", "/posts/3/"), (" 도서 ", "/posts/2/")):
            _, page = self.search(query)
            self.assertEqual(page.posts, [expected], query)

    def test_blank_query_keeps_all_posts(self):
        for query in ("", "   "):
            _, page = self.search(query)
            self.assertEqual(page.posts, ["/posts/1/", "/posts/2/", "/posts/3/"])

    def test_no_match_has_notice_without_post_links(self):
        body, page = self.search("존재하지않는검색어")
        self.assertEqual(page.posts, [])
        self.assertIn("검색 결과가 없습니다.", body)

    def test_query_stays_literal_without_executable_html(self):
        query = '\"><script>alert(1)</script><img src=x onerror=alert(2)>'
        body, page = self.search(query)
        self.assertEqual(len(page.inputs), 1)
        self.assertEqual(page.inputs[0].get("value"), query)
        self.assertNotIn(query, body)
        self.assertFalse(any(tag in ("script", "img") for tag, _ in page.tags))
        self.assertFalse(any(name.startswith("on") for _, attrs in page.tags for name in attrs))


if __name__ == "__main__":
    sys.exit(baseline.run_checks(BoardSearchChecks, "board-search"))
