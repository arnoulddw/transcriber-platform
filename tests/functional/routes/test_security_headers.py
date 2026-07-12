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
