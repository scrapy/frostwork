import subprocess
import sys

import pytest

import frostwork
from frostwork import MetaRefresh, Navigation, Page

HTML = (
    b'<html><head><title>T</title><base href="/sub/">'
    b'<meta http-equiv="refresh" content="3;url=next"></head><body><h1>H</h1></body></html>'
)


def test_navigation():
    nav = frostwork.navigation(HTML, "http://h/p")
    assert nav == Navigation("http://h/sub/", MetaRefresh(3.0, "http://h/sub/next"))
    assert isinstance(nav.meta_refresh, MetaRefresh)
    assert (nav.meta_refresh.delay, nav.meta_refresh.url) == nav.meta_refresh == (3.0, "http://h/sub/next")


@pytest.mark.parametrize("html", [HTML, bytearray(HTML), memoryview(HTML), HTML.decode()])
def test_navigation_input_types(html):
    assert frostwork.navigation(html, "http://h/p", meta_refresh=False) == Navigation("http://h/sub/", None)


def test_navigation_serializes_the_document_url():
    assert frostwork.navigation(b"<p>x</p>", "HTTP://H/a b", meta_refresh=False).base_url == "http://h/a%20b"
    refresh = frostwork.navigation(b'<meta http-equiv="refresh" content="5">', "http://h").meta_refresh
    assert refresh == (5.0, "http://h/")


def test_navigation_scripting():
    html = b'<noscript><meta http-equiv="refresh" content="0;url=/ns"></noscript>'
    assert frostwork.navigation(html, "http://h/p").meta_refresh is None
    assert frostwork.navigation(html, "http://h/p", scripting=False).meta_refresh == (0.0, "http://h/ns")


def test_navigation_encoding():
    html = '<base href="/b/?q=é">'.encode("cp1252")
    assert frostwork.navigation(html, "http://h/p", "cp1252").base_url == "http://h/b/?q=%E9"
    assert frostwork.navigation(html, "http://h/p", "no-such-label").base_url == "http://h/b/?q=%E9"


def test_navigation_with_a_relative_url():
    html = b'<base href="/b/"><meta http-equiv="refresh" content="0;url=next">'
    assert frostwork.navigation(html, "") == Navigation("", None)
    html = b'<base href="http://h/b/"><meta http-equiv="refresh" content="0;url=next">'
    assert frostwork.navigation(html, "/p") == Navigation("http://h/b/", (0.0, "http://h/b/next"))


def test_page_extract_with_navigation():
    page = Page().field("title", "title::text").many("heads", "body", {"h": "h1::text"})
    item, nav = page.extract_with_navigation(HTML, "http://h/p", base_url=False)
    assert item.to_dict() == page.extract(HTML).to_dict() == {"title": "T", "heads": [{"h": "H"}]}
    assert nav == Navigation(None, (3.0, "http://h/sub/next"))
    assert isinstance(nav.meta_refresh, MetaRefresh)


def test_navigation_skips_the_page_object_layer():
    code = (
        "import sys, frostwork; frostwork.navigation(b'', 'http://h/');"
        " print('frostwork.page' in sys.modules)"
    )
    assert subprocess.check_output([sys.executable, "-c", code], text=True).strip() == "False"
