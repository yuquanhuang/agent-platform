"""Temporal adapter for idempotent Agent Run control signals."""

from datetime import timedelta
from uuid import UUID

from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from packages.application.reconciliation import RunWorkflowExecution
from packages.application.temporal import AgentRunWorkflow, agent_run_workflow_id
from packages.contracts.public import dependency_unavailable
from packages.contracts.temporal import ApprovalDecidedSignal, CancelRunSignal


class TemporalRunWorkflowControl:
    """Signal an already-started deterministic Run Workflow."""

    def __init__(
        self, client: Client, *, rpc_timeout: timedelta = timedelta(seconds=10)
    ) -> None:
        self._client = client
        self._rpc_timeout = rpc_timeout

    async def signal_cancel(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        signal: CancelRunSignal,
    ) -> None:
        handle = self._client.get_workflow_handle_for(
            AgentRunWorkflow.run,
            agent_run_workflow_id(tenant_id, run_id),
        )
        try:
            await handle.signal(
                AgentRunWorkflow.cancel_run,
                signal,
                rpc_timeout=self._rpc_timeout,
            )
        except RPCError as error:
            raise dependency_unavailable(
                "The Run cancellation signal could not be delivered."
            ) from error

    async def signal_approval(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        signal: ApprovalDecidedSignal,
    ) -> None:
        handle = self._client.get_workflow_handle_for(
            AgentRunWorkflow.run,
            agent_run_workflow_id(tenant_id, run_id),
        )
        try:
            await handle.signal(
                AgentRunWorkflow.approval_decided,
                signal,
                rpc_timeout=self._rpc_timeout,
            )
        except RPCError as error:
            raise dependency_unavailable(
                "The Approval decision signal could not be delivered."
            ) from error

    async def describe_run(
        self, *, tenant_id: UUID, run_id: UUID
    ) -> RunWorkflowExecution | None:
        handle = self._client.get_workflow_handle_for(
            AgentRunWorkflow.run,
            agent_run_workflow_id(tenant_id, run_id),
        )
        try:
            description = await handle.describe(rpc_timeout=self._rpc_timeout)
        except RPCError as error:
            if error.status is RPCStatusCode.NOT_FOUND:
                return None
            raise dependency_unavailable(
                "The Run Workflow state could not be inspected."
            ) from error
        return RunWorkflowExecution(
            workflow_id=description.id,
            temporal_run_id=description.run_id,
            status=description.status.name if description.status else "UNKNOWN",
        )
