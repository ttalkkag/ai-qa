import argparse
from html import escape
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server


POSTS = (
    {"id": 1, "title": "함께 쓰는 게시판", "body": "누구나 확인할 수 있는 공지와 운영 안내입니다."},
    {"id": 2, "title": "주말 도서 모임", "body": "토요일 오전에 함께 책을 읽습니다. 처음 오시는 분도 환영합니다."},
    {"id": 3, "title": "안전한 HTML 표시", "body": '<script>alert("게시판")</script> & <b>강조</b>도 글자 그대로 보여야 합니다.'},
)


class LocalBoardServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def application(environ, start_response):
    status = "200 OK"
    headers = [("Content-Type", "text/html; charset=utf-8")]
    post = next((post for post in POSTS if environ["PATH_INFO"] == f'/posts/{post["id"]}/'), None)
    if environ["REQUEST_METHOD"] != "GET":
        status = "405 Method Not Allowed"
        headers.append(("Allow", "GET"))
        content = "<h1>읽기 전용 게시판입니다</h1>"
    elif post:
        content = f'<h1>{escape(post["title"])}</h1><p>{escape(post["body"])}</p><a href="/">목록으로</a>'
    elif environ["PATH_INFO"] == "/":
        items = "".join(
            f'<li><a href="/posts/{post["id"]}/">{escape(post["title"])}</a></li>'
            for post in POSTS
        )
        content = f"<h1>동네 게시판</h1><ul>{items}</ul>"
    else:
        status = "404 Not Found"
        content = '<h1>게시글을 찾을 수 없습니다</h1><a href="/">목록으로</a>'
    body = f'<!doctype html><html lang="ko"><meta charset="utf-8"><title>동네 게시판</title><body>{content}</body></html>'.encode("utf-8")
    headers.append(("Content-Length", str(len(body))))
    start_response(status, headers)
    return [body]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="로컬 검증용 읽기 전용 게시판")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port는 0부터 65535 사이여야 합니다")
    with make_server("127.0.0.1", args.port, application, server_class=LocalBoardServer) as server:
        print(f"게시판: http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
