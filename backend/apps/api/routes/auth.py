"""Authentication and current identity endpoints."""

from typing import Annotated

from fastapi import APIRouter, Header

from packages.application.public import CurrentIdentityService
from packages.contracts.generated.core_models import CurrentIdentity
from packages.contracts.public import IdentityProvider, dependency_unavailable


def create_auth_router(
    identity_provider: IdentityProvider | None,
    identity_service: CurrentIdentityService | None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["Auth"])

    async def get_current_identity(
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> CurrentIdentity:
        if identity_provider is None or identity_service is None:
            raise dependency_unavailable("Identity service is not configured.")
        principal = identity_provider.authenticate(authorization)
        return await identity_service.get_current_identity(principal)

    router.add_api_route(
        "/me",
        get_current_identity,
        methods=["GET"],
        operation_id="getCurrentIdentity",
        response_model=CurrentIdentity,
    )
    return router
