from flask import Flask

from app.services import llm_service


def test_generate_text_releases_database_before_provider_call(monkeypatch):
    app = Flask(__name__)
    app.config.update(DEPLOYMENT_MODE="single")
    events = []

    class FakeClient:
        def generate_text(self, _prompt, **_kwargs):
            events.append("provider")
            return "Generated text"

    monkeypatch.setattr(llm_service, "close_db", lambda: events.append("released"))
    monkeypatch.setattr(
        llm_service,
        "get_llm_client",
        lambda _provider, _api_key, _config: FakeClient(),
    )

    with app.app_context():
        result = llm_service.generate_text_via_llm(
            "gemini",
            "Prompt",
            api_key="test-key",
        )

    assert result == "Generated text"
    assert events == ["released", "provider"]
