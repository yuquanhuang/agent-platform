"""High-entropy Artifact Download Gateway bearer credentials."""

import hashlib
import re
import secrets
from collections.abc import Callable
from uuid import UUID, uuid4

from pydantic import SecretStr

from packages.application.artifacts.downloads import ArtifactDownloadCredential

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43,128}$")


class RandomArtifactDownloadCredentialIssuer:
    """Generate an opaque token and persist only its SHA-256 digest."""

    def __init__(
        self,
        *,
        grant_id_factory: Callable[[], UUID] = uuid4,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._grant_id_factory = grant_id_factory
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))

    def issue(self) -> ArtifactDownloadCredential:
        token = self._token_factory()
        if _TOKEN_PATTERN.fullmatch(token) is None:
            raise RuntimeError(
                "Artifact download token factory returned an unsafe token"
            )
        return ArtifactDownloadCredential(
            grant_id=self._grant_id_factory(),
            token=SecretStr(token),
            token_hash="sha256:" + hashlib.sha256(token.encode()).hexdigest(),
        )
