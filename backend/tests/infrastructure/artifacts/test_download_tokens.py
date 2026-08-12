"""Artifact Download Gateway token issuance tests."""

from uuid import UUID

import pytest

from packages.infrastructure.artifacts import RandomArtifactDownloadCredentialIssuer


def test_download_issuer_returns_only_hash_as_persistable_fact() -> None:
    issuer = RandomArtifactDownloadCredentialIssuer(
        grant_id_factory=lambda: UUID("11111111-1111-4111-8111-111111111111"),
        token_factory=lambda: "t" * 43,
    )

    credential = issuer.issue()

    assert credential.grant_id == UUID("11111111-1111-4111-8111-111111111111")
    assert credential.token.get_secret_value() == "t" * 43
    assert credential.token_hash.startswith("sha256:")
    assert credential.token.get_secret_value() not in repr(credential)


def test_download_issuer_rejects_unsafe_token_factory() -> None:
    issuer = RandomArtifactDownloadCredentialIssuer(token_factory=lambda: "short")

    with pytest.raises(RuntimeError, match="unsafe token"):
        issuer.issue()
