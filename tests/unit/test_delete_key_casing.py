"""Service-name casing consistency between save and delete key paths."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import user_service


def test_delete_user_api_key_accepts_mixed_case_service():
    """save_user_api_key lowercases before validating; delete must match."""
    with patch.object(
        user_service.user_model, "get_user_by_id",
        return_value=SimpleNamespace(id=7),
    ), patch.object(
        user_service.user_api_key_model, "delete_api_key", return_value=True,
    ) as delete_key:
        user_service.delete_user_api_key(7, "OpenAI")

    assert delete_key.call_args.args[:2] == (7, "openai")


def test_delete_user_api_key_still_rejects_unknown_services():
    with pytest.raises(ValueError, match="Invalid service"):
        user_service.delete_user_api_key(7, "not-a-provider")
