"""tool_nexus.process.link_server のテスト（実際に 127.0.0.1 で起動して取得する）."""

from __future__ import annotations

import http.client
import os
import sys
from pathlib import Path

import pytest

from tool_nexus.core.constants import KIND_LINK
from tool_nexus.core.models import Tool, build_tool
from tool_nexus.core.ports import pick_free_port
from tool_nexus.process import link_server
from tool_nexus.process.link_server import base_dir, link_url, resolve_request


def make_link(tool_id: int, target: str) -> Tool:
    tool = build_tool(name=f"link{tool_id}", directory="", command="", kind=KIND_LINK, target=target,
                      check_directory=False)
    tool.id = tool_id
    return tool


@pytest.fixture()
def site(tmp_path: Path) -> dict[str, Path]:
    docs = tmp_path / "docs"
    (docs / "img").mkdir(parents=True)
    (docs / "index.html").write_text("<h1>index</h1>", encoding="utf-8")
    (docs / "readme.html").write_text("<link href='style.css'><h1>readme</h1>", encoding="utf-8")
    (docs / "style.css").write_text("h1{}", encoding="utf-8")
    (docs / "img" / "a b.svg").write_text("<svg/>", encoding="utf-8")
    listing = tmp_path / "listing"
    listing.mkdir()
    (listing / "one.html").write_text("1", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    return {"docs": docs, "listing": listing, "root": tmp_path}


@pytest.fixture()
def lookup(site: dict[str, Path]):
    tools = {
        1: make_link(1, str(site["docs"] / "readme.html")),
        2: make_link(2, str(site["listing"])),
        3: make_link(3, "https://example.com/page"),
        4: make_link(4, str(site["root"] / "missing.html")),
    }
    return tools.get


class TestResolve:
    def test_file_link_serves_its_folder(self, site, lookup) -> None:
        assert resolve_request("/links/1/readme.html", lookup) == site["docs"] / "readme.html"
        assert resolve_request("/links/1/style.css", lookup) == site["docs"] / "style.css"
        assert resolve_request("/links/1/img/a%20b.svg", lookup) == site["docs"] / "img" / "a b.svg"

    def test_folder_link(self, site, lookup) -> None:
        assert resolve_request("/links/2/", lookup) == site["listing"]
        assert resolve_request("/links/2/one.html?x=1", lookup) == site["listing"] / "one.html"

    @pytest.mark.parametrize(
        "path",
        [
            "/links/1/../secret.txt",
            "/links/1/%2e%2e/secret.txt",
            "/links/1/img/../../secret.txt",
            "/links/1/..%5csecret.txt",
            "/links/1/..\\secret.txt",
            "/links/1/C:/Windows/win.ini",
            "/links/3/",             # URL のリンクは配信しない
            "/links/4/missing.html",  # 無いリンク先
            "/links/99/x",           # 未登録
            "/links/abc/x",
            "/other/1/readme.html",
        ],
    )
    def test_rejected(self, path: str, lookup) -> None:
        assert resolve_request(path, lookup) is None

    @pytest.mark.skipif(sys.platform == "win32", reason="シンボリックリンクの作成に権限が要る")
    def test_symlink_escaping_base_is_rejected(self, site, lookup) -> None:
        os.symlink(site["root"] / "secret.txt", site["docs"] / "leak.txt")
        assert resolve_request("/links/1/leak.txt", lookup) is None


class TestLinkUrl:
    def test_urls(self, site) -> None:
        assert link_url(make_link(3, "https://example.com/page")) == "https://example.com/page"
        assert link_url(make_link(1, str(site["docs"] / "readme.html")), port=8498) == (
            "http://127.0.0.1:8498/links/1/readme.html"
        )
        assert link_url(make_link(2, str(site["listing"])), port=8498) == "http://127.0.0.1:8498/links/2/"
        assert link_url(make_link(5, str(site["docs"] / "a b.html")), port=8498).endswith("/links/5/a%20b.html")

    def test_base_dir(self, site) -> None:
        assert base_dir(make_link(1, str(site["docs"] / "readme.html"))) == site["docs"]
        assert base_dir(make_link(3, "https://example.com")) is None
        assert base_dir(make_link(4, str(site["root"] / "missing.html"))) is None


@pytest.fixture()
def server(lookup):
    port = pick_free_port(set(), 22000, 22999)
    link_server.stop_server()
    assert link_server.ensure_server(lookup, port=port) is None
    yield port
    link_server.stop_server()


def get(port: int, path: str, method: str = "GET") -> tuple[int, str, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path)  # パスは正規化せずそのまま送る
    response = conn.getresponse()
    body = response.read().decode("utf-8", errors="replace")
    conn.close()
    return response.status, response.getheader("Content-Type") or "", body


class TestServer:
    def test_serves_html_and_assets(self, server) -> None:
        status, ctype, body = get(server, "/links/1/readme.html")
        assert status == 200 and "text/html" in ctype and "readme" in body
        assert get(server, "/links/1/style.css")[1].startswith("text/css")
        assert get(server, "/links/1/img/a%20b.svg")[0] == 200

    def test_folder_listing_and_index(self, server) -> None:
        status, _, body = get(server, "/links/2/")
        assert status == 200 and "one.html" in body
        assert get(server, "/links/1/")[2] == "<h1>index</h1>"

    @pytest.mark.parametrize("path", ["/links/1/../secret.txt", "/links/1/%2e%2e/secret.txt", "/links/99/x", "/"])
    def test_outside_is_404(self, server, path: str) -> None:
        status, _, body = get(server, path)
        assert status == 404 and "secret" not in body

    def test_only_get_and_head(self, server) -> None:
        assert get(server, "/links/1/readme.html", "HEAD")[0] == 200
        assert get(server, "/links/1/readme.html", "POST")[0] == 501

    def test_second_start_is_noop(self, server, lookup) -> None:
        assert link_server.ensure_server(lookup, port=server) is None

    def test_port_in_use_is_reported(self, lookup) -> None:
        import socket

        link_server.stop_server()
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            message = link_server.ensure_server(lookup, port=busy.getsockname()[1])
        assert message and "起動できませんでした" in message
