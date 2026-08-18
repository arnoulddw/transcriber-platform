import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("MYSQL_USER", "test")
os.environ.setdefault("MYSQL_PASSWORD", "test")
os.environ.setdefault("MYSQL_DB", "test_db")

from flask import Flask

from app.api import auth


def test_transcription_csrf_refresh_bypasses_browser_cache_and_asset_cache():
    script = Path("app/static/js/main_actions.js").read_text(encoding="utf-8")
    base_template = Path("app/templates/base.html").read_text(encoding="utf-8")

    assert "fetch('/api/csrf-token'" in script
    assert "cache: 'no-store'" in script
    assert "filename='js/main_actions.js', v=build_timestamp" in base_template


def test_csrf_refresh_response_disables_intermediary_caching():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"

    with app.test_request_context("/api/csrf-token"):
        with patch.object(auth, "current_user", SimpleNamespace(is_authenticated=True)):
            response = auth.csrf_token()

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store, no-cache, must-revalidate, max-age=0"
    assert response.headers["Pragma"] == "no-cache"
    assert response.headers["Expires"] == "0"
    assert response.get_json()["csrf_token"]
