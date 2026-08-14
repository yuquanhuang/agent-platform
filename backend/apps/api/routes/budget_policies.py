"""Tenant periodic model budget policy administration routes."""

# pyright: reportUnusedFunction=false

from typing import Annotated, cast

from fastapi import APIRouter, Header, Query, Request, Response, status

from packages.application.policy.budgets import BudgetPolicyManagementService
from packages.application.public import RequestMetadata
from packages.contracts.generated.resources_models import (
    ActionRequest,
    BudgetPolicy,
    BudgetPolicyCreateRequest,
    BudgetPolicyPage,
    BudgetPolicyUpdateRequest,
    BudgetPolicyVersionPage,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    IdentityProvider,
    dependency_unavailable,
)


def create_budget_policy_router(
    identity_provider: IdentityProvider | None,
    service: BudgetPolicyManagementService | None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["BudgetPolicies"])
    configured_service = cast(BudgetPolicyManagementService, service)

    def metadata(request: Request) -> RequestMetadata:
        trace_id = getattr(request.state, "trace_id", None)
        request_id = getattr(request.state, "request_id", None)
        if not isinstance(trace_id, str) or not isinstance(request_id, str):
            raise TypeError("request correlation context is not installed")
        return RequestMetadata(request_id=request_id, trace_id=trace_id)

    def authenticate(authorization: str | None) -> AuthenticatedPrincipal:
        if identity_provider is None or service is None:
            raise dependency_unavailable("BudgetPolicy service is not configured.")
        return identity_provider.authenticate(authorization)

    @router.get(
        "/budget-policies",
        operation_id="listBudgetPolicies",
        response_model=BudgetPolicyPage,
    )
    async def list_budget_policies(
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
    ) -> BudgetPolicyPage:
        return await configured_service.list_policies(
            authenticate(authorization),
            limit=limit,
            cursor=cursor,
            metadata=metadata(request),
        )

    @router.post(
        "/budget-policies",
        operation_id="createBudgetPolicy",
        response_model=BudgetPolicy,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_budget_policy(
        request: Request,
        response: Response,
        body: BudgetPolicyCreateRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> BudgetPolicy:
        result, etag = await configured_service.create_policy(
            authenticate(authorization),
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.get(
        "/budget-policies/{budget_policy_id}",
        operation_id="getBudgetPolicy",
        response_model=BudgetPolicy,
    )
    async def get_budget_policy(
        request: Request,
        budget_policy_id: str,
        response: Response,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> BudgetPolicy:
        result, etag = await configured_service.get_policy(
            authenticate(authorization),
            policy_id=budget_policy_id,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.patch(
        "/budget-policies/{budget_policy_id}",
        operation_id="updateBudgetPolicy",
        response_model=BudgetPolicy,
    )
    async def update_budget_policy(
        request: Request,
        budget_policy_id: str,
        response: Response,
        body: BudgetPolicyUpdateRequest,
        if_match: Annotated[
            str, Header(alias="If-Match", pattern=r'^"rv:[1-9][0-9]*"$')
        ],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> BudgetPolicy:
        result, etag = await configured_service.update_policy(
            authenticate(authorization),
            policy_id=budget_policy_id,
            if_match=if_match,
            request=body,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    async def set_enabled(
        *,
        request: Request,
        budget_policy_id: str,
        response: Response,
        if_match: str,
        idempotency_key: str,
        body: ActionRequest | None,
        authorization: str | None,
        enabled: bool,
    ) -> BudgetPolicy:
        result, etag = await configured_service.set_policy_enabled(
            authenticate(authorization),
            policy_id=budget_policy_id,
            if_match=if_match,
            enabled=enabled,
            request=body,
            idempotency_key=idempotency_key,
            metadata=metadata(request),
        )
        response.headers["ETag"] = etag
        return result

    @router.post(
        "/budget-policies/{budget_policy_id}/disable",
        operation_id="disableBudgetPolicy",
        response_model=BudgetPolicy,
    )
    async def disable_budget_policy(
        request: Request,
        budget_policy_id: str,
        response: Response,
        if_match: Annotated[
            str, Header(alias="If-Match", pattern=r'^"rv:[1-9][0-9]*"$')
        ],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        body: ActionRequest | None = None,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> BudgetPolicy:
        return await set_enabled(
            request=request,
            budget_policy_id=budget_policy_id,
            response=response,
            if_match=if_match,
            idempotency_key=idempotency_key,
            body=body,
            authorization=authorization,
            enabled=False,
        )

    @router.post(
        "/budget-policies/{budget_policy_id}/enable",
        operation_id="enableBudgetPolicy",
        response_model=BudgetPolicy,
    )
    async def enable_budget_policy(
        request: Request,
        budget_policy_id: str,
        response: Response,
        if_match: Annotated[
            str, Header(alias="If-Match", pattern=r'^"rv:[1-9][0-9]*"$')
        ],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        body: ActionRequest | None = None,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> BudgetPolicy:
        return await set_enabled(
            request=request,
            budget_policy_id=budget_policy_id,
            response=response,
            if_match=if_match,
            idempotency_key=idempotency_key,
            body=body,
            authorization=authorization,
            enabled=True,
        )

    @router.get(
        "/budget-policies/{budget_policy_id}/versions",
        operation_id="listBudgetPolicyVersions",
        response_model=BudgetPolicyVersionPage,
    )
    async def list_budget_policy_versions(
        request: Request,
        budget_policy_id: str,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        cursor: Annotated[str | None, Query()] = None,
    ) -> BudgetPolicyVersionPage:
        return await configured_service.list_versions(
            authenticate(authorization),
            policy_id=budget_policy_id,
            limit=limit,
            cursor=cursor,
            metadata=metadata(request),
        )

    return router
