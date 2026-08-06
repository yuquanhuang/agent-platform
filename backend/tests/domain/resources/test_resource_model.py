"""Resource registry domain behavior."""

from packages.contracts.generated.resource_content import ResourceContentPrompt
from packages.domain.resources import canonical_content_hash, validate_content_type


def prompt_content(template: str = "Hello {{ name }}") -> ResourceContentPrompt:
    return ResourceContentPrompt(
        resource_type="prompt",
        template=template,
        variables=[],
        language="en",
        compiler_policy_version="1",
    )


def test_canonical_content_hash_is_stable_for_equivalent_content() -> None:
    first = prompt_content()
    second = ResourceContentPrompt.model_validate(
        {
            "compiler_policy_version": "1",
            "language": "en",
            "variables": [],
            "template": "Hello {{ name }}",
            "resource_type": "prompt",
        }
    )

    assert canonical_content_hash(first) == canonical_content_hash(second)
    assert canonical_content_hash(first).startswith("sha256:")


def test_content_type_must_match_resource_collection() -> None:
    validate_content_type("prompt", prompt_content())

    try:
        validate_content_type("model_config", prompt_content())  # type: ignore[arg-type]
    except ValueError as error:
        assert "must match" in str(error)
    else:
        raise AssertionError("mismatched resource content must fail")
