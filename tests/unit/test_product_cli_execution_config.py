from __future__ import annotations

from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.api.zyra_api import main
from apps.api.zyra_api.provider_backend_api import ProviderBackendApiResponse


def _catalog(*models: dict[str, object]) -> ProviderBackendApiResponse:
    return ProviderBackendApiResponse(
        status=HTTPStatus.OK,
        body={"result": list(models)},
        headers={},
    )


def test_product_execution_config_is_catalog_bound() -> None:
    with patch.object(
        main.ProviderBackendApi,
        "handle_get",
        return_value=_catalog(
            {"providerId": "deepseek", "modelId": "deepseek-v4-flash"}
        ),
    ):
        selected = main._task_product_execution_config(
            {
                "execution_config": {
                    "provider_id": "deepseek",
                    "model_id": "deepseek-v4-flash",
                }
            }
        )
        assert selected == {
            "schema": "zyra.product-execution-config/v1",
            "provider_id": "deepseek",
            "model_id": "deepseek-v4-flash",
            "source": "product_cli",
        }
        with pytest.raises(ValueError, match="not currently available"):
            main._task_product_execution_config(
                {
                    "execution_config": {
                        "provider_id": "deepseek",
                        "model_id": "missing",
                    }
                }
            )


def test_physical_dispatch_prefers_task_bound_model_without_ui_only_state() -> None:
    state = SimpleNamespace(
        metadata={
            "product_execution_config": {
                "provider_id": "zai",
                "model_id": "glm-5",
            }
        }
    )
    assert main._task_preferred_provider(state) == ("zai", "glm-5")
    with patch.object(
        main,
        "_preferred_configured_provider",
        return_value=("deepseek", "deepseek-v4-flash"),
    ):
        assert main._task_preferred_provider(SimpleNamespace(metadata={})) == (
            "deepseek",
            "deepseek-v4-flash",
        )
