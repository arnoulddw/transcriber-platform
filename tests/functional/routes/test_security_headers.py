import re


def test_csp_uses_fresh_nonce_without_inline_script_exception(client):
    first = client.get('/login')
    second = client.get('/login')

    first_csp = first.headers['Content-Security-Policy']
    second_csp = second.headers['Content-Security-Policy']
    first_nonce = re.search(r"script-src[^;]*'nonce-([^']+)'", first_csp)
    second_nonce = re.search(r"script-src[^;]*'nonce-([^']+)'", second_csp)

    assert first_nonce
    assert second_nonce
    assert first_nonce.group(1) != second_nonce.group(1)
    assert "'unsafe-inline'" not in first_csp.split('style-src', 1)[0]
    assert f'nonce="{first_nonce.group(1)}"'.encode() in first.data


def test_csp_allows_jsdelivr_source_map_fetches(client):
    csp = client.get('/login').headers['Content-Security-Policy']

    # base_bootstrap.html loads marked/dompurify from cdn.jsdelivr.net; their
    # minified bundles reference .map files, which browsers fetch under
    # connect-src when DevTools is open.
    script_src = re.search(r"script-src[^;]*", csp)
    connect_src = re.search(r"connect-src[^;]*", csp)
    assert script_src and connect_src

    assert "https://cdn.jsdelivr.net" in script_src.group(0)
    assert "https://cdn.jsdelivr.net" in connect_src.group(0)
