"""Agent Run Activity coordination over explicit ports."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest
from pydantic import SecretStr
from temporalio.exceptions import ApplicationError

from packages.application.temporal import (
    AgentRunWorkflowActivities,
    FencingTokenIssuer,
    RunExecutionRequest,
    RunRuntimeCancellationRequest,
    RunRuntimeController,
    RunRuntimeError,
    RunRuntimeExecutor,
    RunRuntimeInspectionRequest,
    RunSandboxController,
    RunSandboxProvisionRequest,
    RunSandboxReleaseRequest,
    RunSpecCompilationSource,
    RunSpecCompiler,
    RuntimeEventCandidatePublisher,
    RunWorkflowStore,
)
from packages.contracts.generated.run_event import (
    RUNTIME_EVENT_CANDIDATE_ADAPTER,
    RuntimeEventCandidate,
)
from packages.contracts.public import TenantContext
from packages.contracts.temporal import (
    AgentRunWorkflowInput,
    AssistantTextPart,
    CancelAgentRuntimeInput,
    ExecuteAgentRunInput,
    FinalizeAgentRunCancellationInput,
    FinalizeAgentRunInput,
    FinalizeAgentRunResult,
    InspectAgentRuntimeInput,
    ProvisionRunSandboxInput,
    RecoverAgentRunInput,
    ReleaseRunSandboxInput,
    ReleaseRunSandboxResult,
    RunSandboxHandle,
    RunSpecReference,
    RuntimeCancellationResult,
    RuntimeCompletion,
    RuntimeInspection,
)
from packages.domain.public import RunAttemptRecord

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
USER_ID = UUID("33333333-3333-4333-8333-333333333333")
SESSION_ID = UUID("44444444-4444-4444-8444-444444444444")
MESSAGE_ID = UUID("55555555-5555-4555-8555-555555555555")
AGENT_ID = UUID("66666666-6666-4666-8666-666666666666")
VERSION_ID = UUID("77777777-7777-4777-8777-777777777777")
SNAPSHOT_ID = UUID("88888888-8888-4888-8888-888888888888")
DEPLOYMENT_ID = UUID("99999999-9999-4999-8999-999999999999")
BUNDLE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
RUN_SPEC = RunSpecReference(
    uri=f"immutable://run-spec/{RUN_ID}/1",
    content_hash="sha256:" + "a" * 64,
    size_bytes=1024,
)


def workflow_input() -> AgentRunWorkflowInput:
    return AgentRunWorkflowInput(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        request_id="req-run",
        trace_id="trace-run",
    )


def source() -> RunSpecCompilationSource:
    return RunSpecCompilationSource(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        user_id=USER_ID,
        session_id=SESSION_ID,
        branch_id=None,
        user_message_id=MESSAGE_ID,
        user_text="hello",
        agent_id=AGENT_ID,
        agent_version_id=VERSION_ID,
        snapshot_id=SNAPSHOT_ID,
        snapshot_content={"runtime_type": "agentscope", "bindings": []},
        deployment_id=DEPLOYMENT_ID,
        bundle_id=BUNDLE_ID,
        bundle_uri="s3://bundles/run.tar",
        bundle_hash="sha256:" + "b" * 64,
        bundle_size_bytes=4096,
        bundle_compiler_version="1.0.0",
        runtime_type="agentscope",
        runtime_target_id="rt_agentscope_default",
        timeout_seconds=120,
        token_budget=2048,
        cost_budget_amount=Decimal("1.25"),
        cost_budget_currency="USD",
        idempotency_key="run-request-1",
        status="QUEUED",
        current_attempt=0,
        workflow_id=None,
    )


class StoreStub:
    def __init__(self) -> None:
        self.source = source()
        self.preparations: list[dict[str, object]] = []
        self.running: list[dict[str, object]] = []
        self.finalizations: list[FinalizeAgentRunInput] = []
        self.recoveries: list[dict[str, object]] = []
        self.cancellations: list[FinalizeAgentRunCancellationInput] = []
        self.attempt = RunAttemptRecord(
            id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab"),
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            attempt_no=1,
            fencing_token_hash="sha256:" + "1" * 64,
            worker_id="worker-1",
            runtime_handle_ref="runtime://run/1",
            status="RUNNING",
            started_at=None,
            heartbeat_at=None,
            finished_at=None,
            error_code=None,
        )

    async def load_run_spec_source(
        self, context: TenantContext, *, run_id: UUID
    ) -> RunSpecCompilationSource | None:
        assert context.tenant_id == str(TENANT_ID)
        return self.source if run_id == RUN_ID else None

    async def prepare_run(self, context: TenantContext, **kwargs: object) -> None:
        self.preparations.append(dict(kwargs))

    async def mark_run_running(self, context: TenantContext, **kwargs: object) -> None:
        self.running.append(dict(kwargs))

    async def load_run_attempt(
        self, context: TenantContext, *, run_id: UUID, execution_attempt: int
    ) -> RunAttemptRecord | None:
        return (
            self.attempt
            if (run_id, execution_attempt) == (RUN_ID, self.attempt.attempt_no)
            else None
        )

    async def prepare_recovery_attempt(
        self, context: TenantContext, **kwargs: object
    ) -> None:
        self.recoveries.append(dict(kwargs))

    async def finalize_run(
        self,
        context: TenantContext,
        *,
        input: FinalizeAgentRunInput,
        fencing_token_hash: str | None = None,
    ) -> FinalizeAgentRunResult:
        assert fencing_token_hash is not None
        self.finalizations.append(input)
        return FinalizeAgentRunResult(
            run_id=input.run_id,
            status=input.completion.status,
            assistant_message_id=(
                MESSAGE_ID if input.completion.status == "SUCCEEDED" else None
            ),
        )

    async def finalize_cancellation(
        self,
        context: TenantContext,
        *,
        input: FinalizeAgentRunCancellationInput,
        fencing_token_hash: str | None,
    ) -> FinalizeAgentRunResult:
        assert fencing_token_hash is not None
        self.cancellations.append(input)
        return FinalizeAgentRunResult(run_id=input.run_id, status="CANCELLED")


class CompilerStub:
    def __init__(self) -> None:
        self.tokens: list[str] = []

    async def compile(
        self,
        context: TenantContext,
        *,
        source: RunSpecCompilationSource,
        execution_attempt: int,
        fencing_token: SecretStr,
    ) -> RunSpecReference:
        assert source.snapshot_id == SNAPSHOT_ID
        self.tokens.append(fencing_token.get_secret_value())
        return RUN_SPEC


class IssuerStub:
    def issue(
        self, *, tenant_id: UUID, run_id: UUID, execution_attempt: int
    ) -> SecretStr:
        assert (tenant_id, run_id) == (TENANT_ID, RUN_ID)
        return SecretStr(f"stable-fencing-token-{execution_attempt:04d}")


class ExecutorStub:
    def __init__(self) -> None:
        self.requests: list[RunExecutionRequest] = []

    async def execute(
        self,
        context: TenantContext,
        *,
        request: RunExecutionRequest,
        event_publisher: RuntimeEventCandidatePublisher,
    ) -> RuntimeCompletion:
        self.requests.append(request)
        await event_publisher.publish(
            context,
            request=request,
            candidate=RUNTIME_EVENT_CANDIDATE_ADAPTER.validate_python(
                {
                    "source_event_id": "fake:text:1",
                    "event_type": "text_delta",
                    "occurred_at": datetime(2026, 8, 8, tzinfo=UTC),
                    "payload_version": "1.0",
                    "payload": {"message_id": "reply-1", "delta": "done"},
                }
            ),
        )
        return RuntimeCompletion(
            status="SUCCEEDED",
            assistant_content_parts=(AssistantTextPart(text="done"),),
            result_quality="NORMAL",
            runtime_handle_ref="runtime://run/1",
        )


class PublisherStub:
    def __init__(self) -> None:
        self.published: list[tuple[RunExecutionRequest, RuntimeEventCandidate]] = []

    async def publish(
        self,
        context: TenantContext,
        *,
        request: RunExecutionRequest,
        candidate: RuntimeEventCandidate,
    ) -> None:
        assert context.tenant_id == str(TENANT_ID)
        self.published.append((request, candidate))


class ControllerStub:
    def __init__(self) -> None:
        self.inspections: list[RunRuntimeInspectionRequest] = []
        self.cancellations: list[RunRuntimeCancellationRequest] = []

    async def inspect(
        self, context: TenantContext, *, request: RunRuntimeInspectionRequest
    ) -> RuntimeInspection:
        self.inspections.append(request)
        return RuntimeInspection(
            run_id=request.run_id,
            execution_attempt=request.execution_attempt,
            status="RUNNING",
            runtime_handle_ref=request.runtime_handle_ref,
        )

    async def cancel(
        self, context: TenantContext, *, request: RunRuntimeCancellationRequest
    ) -> RuntimeCancellationResult:
        self.cancellations.append(request)
        return RuntimeCancellationResult(
            run_id=request.run_id,
            execution_attempt=request.execution_attempt,
            status="CANCELLED",
            runtime_handle_ref=request.runtime_handle_ref,
        )


class SandboxControllerStub:
    def __init__(self) -> None:
        self.provisions: list[RunSandboxProvisionRequest] = []
        self.releases: list[RunSandboxReleaseRequest] = []

    async def provision(
        self, context: TenantContext, *, request: RunSandboxProvisionRequest
    ) -> RunSandboxHandle:
        assert context.tenant_id == str(TENANT_ID)
        self.provisions.append(request)
        return RunSandboxHandle(
            sandbox_instance_id="sandbox_001",
            lease_id="lease_001",
            workspace_uri=f"workspace://tenant/{TENANT_ID}/runs/{RUN_ID}/",
        )

    async def release(
        self, context: TenantContext, *, request: RunSandboxReleaseRequest
    ) -> ReleaseRunSandboxResult:
        assert context.tenant_id == str(TENANT_ID)
        self.releases.append(request)
        return ReleaseRunSandboxResult(
            sandbox_instance_id=request.sandbox.sandbox_instance_id,
            status="TERMINATED",
        )


def activities(
    store: StoreStub,
    compiler: CompilerStub,
    executor: ExecutorStub,
    controller: ControllerStub | None = None,
    sandbox_controller: SandboxControllerStub | None = None,
    publisher: PublisherStub | None = None,
) -> AgentRunWorkflowActivities:
    return AgentRunWorkflowActivities(
        cast(RunWorkflowStore, store),
        cast(RunSpecCompiler, compiler),
        cast(FencingTokenIssuer, IssuerStub()),
        cast(RunRuntimeExecutor, executor),
        cast(RuntimeEventCandidatePublisher, publisher or PublisherStub()),
        cast(RunRuntimeController, controller) if controller else None,
        (
            cast(RunSandboxController, sandbox_controller)
            if sandbox_controller
            else None
        ),
    )


@pytest.mark.asyncio
async def test_prepare_allocates_admitted_queued_run_then_compiles_source() -> None:
    store = StoreStub()
    compiler = CompilerStub()
    result = await activities(store, compiler, ExecutorStub()).prepare_agent_run(
        workflow_input()
    )

    assert result.run_spec == RUN_SPEC
    assert result.execution_attempt == 1
    assert compiler.tokens == ["stable-fencing-token-0001"]
    assert store.preparations[0]["workflow_id"] == f"run/{TENANT_ID}/{RUN_ID}"
    assert str(store.preparations[0]["fencing_token_hash"]).startswith("sha256:")


@pytest.mark.asyncio
async def test_execute_marks_attempt_running_before_runtime_call() -> None:
    store = StoreStub()
    executor = ExecutorStub()
    publisher = PublisherStub()
    completion = await activities(
        store, CompilerStub(), executor, publisher=publisher
    ).execute_agent_run(
        ExecuteAgentRunInput(
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            execution_attempt=1,
            run_spec=RUN_SPEC,
            timeout_seconds=120,
            runtime_type="agentscope",
            request_id="req-run",
            trace_id="trace-run",
        )
    )

    assert completion.status == "SUCCEEDED"
    assert store.running[0]["execution_attempt"] == 1
    assert executor.requests[0].run_spec == RUN_SPEC
    assert executor.requests[0].fencing_token.get_secret_value().endswith("0001")
    assert [candidate.event_type for _, candidate in publisher.published] == [
        "text_delta"
    ]


@pytest.mark.asyncio
async def test_execute_preserves_runtime_adapter_failure_code() -> None:
    class FailingExecutor(ExecutorStub):
        async def execute(self, *args: object, **kwargs: object) -> RuntimeCompletion:
            raise RunRuntimeError(
                "RUN_CANCELLING",
                "The approval was rejected.",
            )

    with pytest.raises(ApplicationError) as raised:
        await activities(
            StoreStub(), CompilerStub(), FailingExecutor()
        ).execute_agent_run(
            ExecuteAgentRunInput(
                tenant_id=TENANT_ID,
                run_id=RUN_ID,
                execution_attempt=1,
                run_spec=RUN_SPEC,
                timeout_seconds=120,
                runtime_type="agentscope",
                request_id="req-run",
                trace_id="trace-run",
            )
        )

    assert getattr(raised.value, "type", None) == "RUN_CANCELLING"


@pytest.mark.asyncio
async def test_provision_and_release_keep_fencing_secret_inside_activity() -> None:
    sandbox_controller = SandboxControllerStub()
    coordinated = activities(
        StoreStub(),
        CompilerStub(),
        ExecutorStub(),
        sandbox_controller=sandbox_controller,
    )
    handle = await coordinated.provision_run_sandbox(
        ProvisionRunSandboxInput(
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            execution_attempt=1,
            run_spec=RUN_SPEC,
            runtime_type="agentscope",
            request_id="req-sandbox",
            trace_id="trace-sandbox",
        )
    )
    released = await coordinated.release_run_sandbox(
        ReleaseRunSandboxInput(
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            execution_attempt=1,
            sandbox=handle,
            request_id="req-sandbox-release",
            trace_id="trace-sandbox-release",
        )
    )

    assert released.status == "TERMINATED"
    assert (
        sandbox_controller.provisions[0]
        .fencing_token.get_secret_value()
        .endswith("0001")
    )
    assert (
        sandbox_controller.releases[0].fencing_token.get_secret_value().endswith("0001")
    )


@pytest.mark.asyncio
async def test_finalize_delegates_one_small_terminal_summary() -> None:
    store = StoreStub()
    input = FinalizeAgentRunInput(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        execution_attempt=1,
        completion=RuntimeCompletion(
            status="FAILED",
            error_code="MODEL_TIMEOUT",
            error_message="The model timed out.",
            retryable=False,
        ),
        request_id="req-run",
        trace_id="trace-run",
    )

    result = await activities(store, CompilerStub(), ExecutorStub()).finalize_agent_run(
        input
    )

    assert result.status == "FAILED"
    assert result.assistant_message_id is None
    assert store.finalizations == [input]


@pytest.mark.asyncio
async def test_inspect_and_cancel_keep_plaintext_fencing_inside_activity() -> None:
    store = StoreStub()
    controller = ControllerStub()
    coordinated = activities(store, CompilerStub(), ExecutorStub(), controller)

    inspection = await coordinated.inspect_agent_runtime(
        InspectAgentRuntimeInput(
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            execution_attempt=1,
            request_id="req-inspect",
            trace_id="trace-inspect",
        )
    )
    cancellation = await coordinated.cancel_agent_runtime(
        CancelAgentRuntimeInput(
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            execution_attempt=1,
            request_id="req-cancel",
            trace_id="trace-cancel",
        )
    )

    assert inspection.status == "RUNNING"
    assert cancellation.status == "CANCELLED"
    assert controller.cancellations[0].fencing_token.get_secret_value().endswith("0001")


@pytest.mark.asyncio
async def test_recovery_allocates_new_attempt_and_new_fencing_token() -> None:
    store = StoreStub()
    store.source = replace(source(), status="RUNNING", current_attempt=1)
    compiler = CompilerStub()

    result = await activities(store, compiler, ExecutorStub()).recover_agent_run(
        RecoverAgentRunInput(
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            lost_execution_attempt=1,
            request_id="req-recover",
            trace_id="trace-recover",
        )
    )

    assert result.execution_attempt == 2
    assert store.recoveries[0]["execution_attempt"] == 2
    assert compiler.tokens == ["stable-fencing-token-0002"]
