"""/c/<id> carries the counter's facts and links in its HTML, and names one
canonical URL whatever alias it was reached on."""
import re
import urllib.request

from test_social import _run_server


def _page(base: str, path: str, host: str | None = None) -> str:
    req = urllib.request.Request(base + path, headers={"Host": host} if host else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        assert r.status == 200
        return r.read().decode()


def test_counter_page_is_readable_without_script():
    httpd, _cfg, base = _run_server()
    try:
        page = _page(base, "/c/1")
        assert "<h1>Counter #1 · SMALLGIF</h1>" in page
        assert "<dt>inscription id</dt>" in page and f"<dd>{1:064x}i0</dd>" in page
        assert 'href="/content/1"' in page and 'href="/counter/1"' in page
        assert f'href="https://explore.block.space/tx/{1:064x}"' in page
        # It opens on the detail view, as the router would.
        assert '<main id="view-detail">' in page
        assert '<main id="view-explore" class="hidden">' in page
        markup = re.sub(r"<script.*?</script>", "", page, flags=re.DOTALL)
        assert markup.count("<h1>") == 1
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_counter_page_by_name_names_the_number():
    httpd, _cfg, base = _run_server()
    try:
        page = _page(base, "/c/SMALLGIF", host="counters.gallery")
        assert '<link rel="canonical" href="https://counters.gallery/c/1">' in page
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_www_alias_names_the_apex():
    httpd, _cfg, base = _run_server()
    try:
        page = _page(base, "/c/1", host="www.counters.gallery")
        canon = re.search(r'<link rel="canonical" href="([^"]+)">', page).group(1)
        assert canon == "https://counters.gallery/c/1"
        assert "www." not in re.search(r'property="og:url" content="([^"]+)"', page).group(1)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_unknown_counter_keeps_the_plain_shell():
    httpd, _cfg, base = _run_server()
    try:
        page = _page(base, "/c/999999")
        assert '<div class="dwrap" id="detail"></div>' in page
        assert '<main id="view-detail" class="hidden">' in page
    finally:
        httpd.shutdown()
        httpd.server_close()
