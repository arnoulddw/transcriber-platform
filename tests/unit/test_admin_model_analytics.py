from unittest.mock import MagicMock, patch

from app.models.transcription_utils import admin_analytics
from app.services import display_mapping_service


MODEL_ROWS = [
    {
        "code": "gpt-transcribe",
        "model_key": "openai:gpt-transcribe",
        "provider_code": "openai",
        "display_name": "OpenAI GPT Transcribe",
    },
    {
        "code": "universal",
        "model_key": "assemblyai:universal",
        "provider_code": "assemblyai",
        "display_name": "AssemblyAI Universal",
    },
    {
        "code": "qwen/qwen3-asr-1.7b",
        "model_key": "openrouter:qwen/qwen3-asr-1.7b",
        "provider_code": "openrouter",
        "display_name": "qwen/qwen3-asr-1.7b",
    },
    {
        "code": "x-ai/grok-stt-1.0",
        "model_key": "openrouter:x-ai/grok-stt-1.0",
        "provider_code": "openrouter",
        "display_name": "x-ai/grok-stt-1.0",
    },
]


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed_sql = []

    def execute(self, sql, params=()):
        self.executed_sql.append((sql, params))

    def fetchall(self):
        return self.rows


def test_transcription_display_map_and_aliases_use_one_canonical_key_per_model():
    with patch.object(
        display_mapping_service.transcription_catalog_model,
        "get_active_models",
        return_value=MODEL_ROWS,
    ), patch.object(
        display_mapping_service.transcription_catalog_model,
        "get_live_models",
        return_value=[],
    ):
        display_map = display_mapping_service.get_transcription_display_map()
        aliases = display_mapping_service.get_transcription_model_aliases()

    assert display_map == {
        "assemblyai:universal": "AssemblyAI Universal",
        "openai:gpt-transcribe": "OpenAI GPT Transcribe",
        "openrouter:qwen/qwen3-asr-1.7b": "qwen/qwen3-asr-1.7b",
        "openrouter:x-ai/grok-stt-1.0": "x-ai/grok-stt-1.0",
    }
    assert aliases["gpt-transcribe"] == "openai:gpt-transcribe"
    assert aliases["assemblyai"] == "assemblyai:universal"
    assert "openrouter" not in aliases
    assert display_mapping_service.resolve_transcription_model_key(
        "openrouter",
        "qwen/qwen3-asr-1.7b",
        aliases,
    ) == "openrouter:qwen/qwen3-asr-1.7b"


def test_api_usage_distribution_merges_legacy_and_canonical_rows():
    cursor = FakeCursor([
        {"api_used": "gpt-transcribe", "api_model": None, "total_value": 35},
        {"api_used": "openai:gpt-transcribe", "api_model": "gpt-transcribe", "total_value": 1},
        {"api_used": "openrouter", "api_model": "qwen/qwen3-asr-1.7b", "total_value": 2},
    ])
    aliases = {
        "gpt-transcribe": "openai:gpt-transcribe",
        "openai:gpt-transcribe": "openai:gpt-transcribe",
        "openrouter:qwen/qwen3-asr-1.7b": "openrouter:qwen/qwen3-asr-1.7b",
    }

    with patch.object(admin_analytics, "get_cursor", return_value=cursor), patch.object(
        admin_analytics.display_mapping_service,
        "get_transcription_model_aliases",
        return_value=aliases,
    ):
        distribution = admin_analytics.get_api_distribution_in_range()

    assert distribution == {
        "openai:gpt-transcribe": 36,
        "openrouter:qwen/qwen3-asr-1.7b": 2,
    }
    assert "GROUP BY api_used, api_model" in cursor.executed_sql[0][0]


def test_api_minutes_distribution_merges_legacy_and_canonical_rows():
    cursor = FakeCursor([
        {"api_used": "gpt-transcribe", "api_model": None, "total_value": 1.25},
        {
            "api_used": "openai:gpt-transcribe",
            "api_model": "gpt-transcribe",
            "total_value": 2.75,
        },
    ])
    aliases = {
        "gpt-transcribe": "openai:gpt-transcribe",
        "openai:gpt-transcribe": "openai:gpt-transcribe",
    }

    with patch.object(admin_analytics, "get_cursor", return_value=cursor), patch.object(
        admin_analytics.display_mapping_service,
        "get_transcription_model_aliases",
        return_value=aliases,
    ):
        distribution = admin_analytics.get_api_distribution_in_range(aggregate_minutes=True)

    assert distribution == {"openai:gpt-transcribe": 4.0}


def test_api_error_rate_distribution_merges_aliases_before_calculating_rate():
    cursor = FakeCursor([
        {
            "api_used": "gpt-transcribe",
            "api_model": None,
            "total_count": 35,
            "error_count": 1,
        },
        {
            "api_used": "openai:gpt-transcribe",
            "api_model": "gpt-transcribe",
            "total_count": 1,
            "error_count": 1,
        },
    ])
    aliases = {
        "gpt-transcribe": "openai:gpt-transcribe",
        "openai:gpt-transcribe": "openai:gpt-transcribe",
    }

    with patch.object(admin_analytics, "get_cursor", return_value=cursor), patch.object(
        admin_analytics.display_mapping_service,
        "get_transcription_model_aliases",
        return_value=aliases,
    ):
        rates = admin_analytics.get_api_error_rate_distribution_in_range()

    assert rates == {"openai:gpt-transcribe": 5.56}
    assert "GROUP BY api_used, api_model" in cursor.executed_sql[0][0]


def test_workflow_count_can_be_scoped_to_user():
    cursor = MagicMock()
    cursor.fetchone.return_value = {"count": 3}

    with patch.object(admin_analytics, "get_cursor", return_value=cursor):
        count = admin_analytics.count_workflow_jobs_with_filters(
            llm_operation_status="finished",
            user_id=42,
        )

    assert count == 3
    sql, params = cursor.execute.call_args.args
    assert "lo.operation_type = 'workflow'" in sql
    assert "lo.status = %s" in sql
    assert "t.user_id = %s" in sql
    assert params == ("finished", 42)
