"""Model Provider and Model Config boundary validation tests."""

import pytest

from packages.application.resources.models import (
    validate_model_config_content,
    validate_model_provider_content,
)
from packages.contracts.generated.resource_content import (
    ResourceContentModelConfig,
    ResourceContentModelProvider,
)
from packages.contracts.public import PlatformError


def provider_content(
    *,
    provider_type: str = "openai",
    base_url: str = "https://api.openai.com/v1",
    secret_ref: str = "secret://tenant/tenant-a/model/openai",
) -> ResourceContentModelProvider:
    return ResourceContentModelProvider(
        resource_type="model_provider",
        provider_type=provider_type,
        base_url=base_url,
        secret_ref=secret_ref,
        timeout_seconds=30,
        data_retention_policy=None,
    )


@pytest.mark.parametrize("provider_type", ["openai", "qwen", "deepseek"])
def test_supported_provider_types_accept_safe_secret_references(
    provider_type: str,
) -> None:
    validate_model_provider_content(provider_content(provider_type=provider_type))


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.example.com/v1",
        "https://user:password@api.example.com/v1",
        "https://api.example.com/v1?token=secret",
        "file:///tmp/provider",
    ],
)
def test_provider_rejects_unsafe_base_urls(base_url: str) -> None:
    with pytest.raises(PlatformError) as error:
        validate_model_provider_content(provider_content(base_url=base_url))

    assert error.value.code == "VALIDATION_ERROR"
    assert base_url not in error.value.message


@pytest.mark.parametrize(
    "secret_ref",
    [
        "sk-plain-secret",
        "secret://tenant/../model/openai",
        "secret://global/tenant-a/model/openai",
        "secret://tenant/tenant-a/model/openai?version=1",
    ],
)
def test_provider_rejects_plain_or_unsafe_secret_references(secret_ref: str) -> None:
    with pytest.raises(PlatformError) as error:
        validate_model_provider_content(provider_content(secret_ref=secret_ref))

    assert error.value.code == "VALIDATION_ERROR"
    assert secret_ref not in error.value.message


def test_model_config_requires_uuid_provider_nonempty_model_and_unique_capabilities() -> (
    None
):
    with pytest.raises(PlatformError):
        validate_model_config_content(
            ResourceContentModelConfig(
                resource_type="model_config",
                provider_id="tenant/provider",
                model_id="gpt-5-mini",
                capabilities=["stream"],
                default_parameters={},
                max_context_tokens=None,
                rate_limit_rpm=None,
                max_output_tokens=None,
                max_reasoning_tokens=None,
                counter_profile_id=None,
                counter_profile_version=None,
                counter_profile_hash=None,
                billing_semantics_version=None,
            )
        )

    with pytest.raises(PlatformError):
        validate_model_config_content(
            ResourceContentModelConfig(
                resource_type="model_config",
                provider_id="11111111-1111-4111-8111-111111111111",
                model_id="   ",
                capabilities=["stream"],
                default_parameters={},
                max_context_tokens=None,
                rate_limit_rpm=None,
                max_output_tokens=None,
                max_reasoning_tokens=None,
                counter_profile_id=None,
                counter_profile_version=None,
                counter_profile_hash=None,
                billing_semantics_version=None,
            )
        )

    with pytest.raises(PlatformError):
        validate_model_config_content(
            ResourceContentModelConfig(
                resource_type="model_config",
                provider_id="11111111-1111-4111-8111-111111111111",
                model_id="gpt-5-mini",
                capabilities=["stream", "stream"],
                default_parameters={},
                max_context_tokens=None,
                rate_limit_rpm=None,
                max_output_tokens=None,
                max_reasoning_tokens=None,
                counter_profile_id=None,
                counter_profile_version=None,
                counter_profile_hash=None,
                billing_semantics_version=None,
            )
        )
