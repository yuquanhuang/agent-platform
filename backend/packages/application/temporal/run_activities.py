"""Agent Run Activities over explicit persistence and Runtime ports."""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, NoReturn, Protocol
from uuid import UUID

from pydantic import JsonValue, SecretStr
from temporalio import activity
from temporalio.exceptions import ApplicationError

from packages.contracts.errors import PlatformError
from packages.contracts.generated.run_event import RuntimeEventCandidate
from packages.contracts.public import SubjectType, TenantContext
from packages.contracts.temporal import (
    AgentRunWorkflowInput,
    CancelAgentRuntimeInput,
    ExecuteAgentRunInput,
    FinalizeAgentRunCancellationInput,
    FinalizeAgentRunInput,
    FinalizeAgentRunResult,
    InspectAgentRuntimeInput,
    RecoverAgentRunInput,
    RunPreparationResult,
    RunSpecReference,
    RuntimeCancellationResult,
    RuntimeCompletion,
    RuntimeInspection,
)
from packages.domain.public import RunAttemptRecord, RunStatus


@dataclass(frozen=True, slots=True)
class RunSpecCompilationSource:
    """Immutable database facts consumed inside one preparation Activity."""

    tenant_id: UUID
    run_id: UUID
    user_id: UUID
    session_id: UUID
    branch_id: UUID | None
    user_message_id: UUID
    user_text: str
    agent_id: UUID
    agent_version_id: UUID
    snapshot_id: UUID
    snapshot_content: dict[str, JsonValue]
    deployment_id: UUID
    bundle_id: UUID
    bundle_uri: str
    bundle_hash: str
    bundle_size_bytes: int
    bundle_compiler_version: str
    runtime_type: Literal["agentscope", "codex"]
    runtime_target_id: str
    timeout_seconds: int
    token_budget: int | None
    cost_budget_amount: Decimal | None
    cost_budget_currency: str | None
    idempotency_key: str
    status: RunStatus
    current_attempt: int
    workflow_id: str | None


@dataclass(frozen=True, slots=True)
class RunExecutionRequest:
    tenant_id: UUID
    run_id: UUID
    execution_attempt: int
    run_spec: RunSpecReference
    timeout_seconds: int
    runtime_type: Literal["agentscope", "codex"]
    fencing_token: SecretStr


@dataclass(frozen=True, slots=True)
class RunRuntimeInspectionRequest:
    tenant_id: UUID
    run_id: UUID
    execution_attempt: int
    runtime_type: Literal["agentscope", "codex"]
    runtime_handle_ref: str | None


@dataclass(frozen=True, slots=True)
class RunRuntimeCancellationRequest(RunRuntimeInspectionRequest):
    fencing_token: SecretStr


class RunWorkflowStore(Protocol):
    async def load_run_spec_source(
        self, context: TenantContext, *, run_id: UUID
    ) -> RunSpecCompilationSource | None: ...

    async def prepare_run(
        self,
        context: TenantContext,
        *,
        run_id: UUID,
        workflow_id: str,
        execution_attempt: int,
        fencing_token_hash: str,
    ) -> None: ...

    async def mark_run_running(
        self,
        context: TenantContext,
        *,
        run_id: UUID,
        execution_attempt: int,
        worker_id: str,
        fencing_token_hash: str | None = None,
    ) -> None: ...

    async def load_run_attempt(
        self, context: TenantContext, *, run_id: UUID, execution_attempt: int
    ) -> RunAttemptRecord | None: ...

    async def prepare_recovery_attempt(
        self,
        context: TenantContext,
        *,
        run_id: UUID,
        lost_execution_attempt: int,
        execution_attempt: int,
        fencing_token_hash: str,
    ) -> None: ...

    async def finalize_run(
        self,
        context: TenantContext,
        *,
        input: FinalizeAgentRunInput,
        fencing_token_hash: str | None = None,
    ) -> FinalizeAgentRunResult: ...

    async def finalize_cancellation(
        self,
        context: TenantContext,
        *,
        input: FinalizeAgentRunCancellationInput,
        fencing_token_hash: str | None,
    ) -> FinalizeAgentRunResult: ...


class RunSpecCompiler(Protocol):
    """Persist a complete RunSpec and return only its immutable reference."""

    async def compile(
        self,
        context: TenantContext,
        *,
        source: RunSpecCompilationSource,
        execution_attempt: int,
        fencing_token: SecretStr,
    ) -> RunSpecReference: ...


class FencingTokenIssuer(Protocol):
    """Issue a stable token for one Run attempt without storing plaintext."""

    def issue(
        self, *, tenant_id: UUID, run_id: UUID, execution_attempt: int
    ) -> SecretStr: ...


class RuntimeEventCandidatePublisher(Protocol):
    """Publish Runtime candidates without routing high-frequency data via History."""

    async def publish(
        self,
        context: TenantContext,
        *,
        request: RunExecutionRequest,
        candidate: RuntimeEventCandidate,
    ) -> None: ...


class RunRuntimeExecutor(Protocol):
    """Execute one immutable RunSpec; event streaming stays outside Temporal History."""

    async def execute(
        self,
        context: TenantContext,
        *,
        request: RunExecutionRequest,
        event_publisher: RuntimeEventCandidatePublisher,
    ) -> RuntimeCompletion: ...


class RunRuntimeController(Protocol):
    """Inspect and cancel one attempt without replaying its user Prompt."""

    async def inspect(
        self, context: TenantContext, *, request: RunRuntimeInspectionRequest
    ) -> RuntimeInspection: ...

    async def cancel(
        self, context: TenantContext, *, request: RunRuntimeCancellationRequest
    ) -> RuntimeCancellationResult: ...


class RunStageError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


class AgentRunWorkflowActivities:
    """Idempotent Activity boundary for one Agent Run workflow."""

    def __init__(
        self,
        store: RunWorkflowStore,
        run_spec_compiler: RunSpecCompiler,
        fencing_token_issuer: FencingTokenIssuer,
        runtime_executor: RunRuntimeExecutor,
        runtime_event_publisher: RuntimeEventCandidatePublisher,
        runtime_controller: RunRuntimeController | None = None,
    ) -> None:
        self._store = store
        self._run_spec_compiler = run_spec_compiler
        self._fencing_token_issuer = fencing_token_issuer
        self._runtime_executor = runtime_executor
        self._runtime_event_publisher = runtime_event_publisher
        self._runtime_controller = runtime_controller

    @activity.defn(name="prepare_agent_run_v1")
    async def prepare_agent_run(
        self, input: AgentRunWorkflowInput
    ) -> RunPreparationResult:
        try:
            context = _context(input)
            source = await self._store.load_run_spec_source(
                context, run_id=input.run_id
            )
            if source is None:
                raise RunStageError("RUN_NOT_FOUND", "The Run is unavailable.")
            execution_attempt = _execution_attempt(source, input)
            fencing_token = self._fencing_token_issuer.issue(
                tenant_id=input.tenant_id,
                run_id=input.run_id,
                execution_attempt=execution_attempt,
            )
            token_value = fencing_token.get_secret_value()
            if not 16 <= len(token_value) <= 512:
                raise RunStageError(
                    "FENCING_TOKEN_INVALID",
                    "The Run fencing token is invalid.",
                )
            await self._store.prepare_run(
                context,
                run_id=input.run_id,
                workflow_id=_activity_workflow_id(input),
                execution_attempt=execution_attempt,
                fencing_token_hash=_sha256(token_value),
            )
            run_spec = await self._run_spec_compiler.compile(
                context,
                source=source,
                execution_attempt=execution_attempt,
                fencing_token=fencing_token,
            )
            return RunPreparationResult(
                run_id=input.run_id,
                execution_attempt=execution_attempt,
                run_spec=run_spec,
                timeout_seconds=source.timeout_seconds,
                runtime_type=source.runtime_type,
            )
        except (ApplicationError, PlatformError, RunStageError) as error:
            _raise_activity_error(error)

    @activity.defn(name="execute_agent_run_v1")
    async def execute_agent_run(self, input: ExecuteAgentRunInput) -> RuntimeCompletion:
        try:
            context = _context(input)
            fencing_token = self._fencing_token_issuer.issue(
                tenant_id=input.tenant_id,
                run_id=input.run_id,
                execution_attempt=input.execution_attempt,
            )
            await self._store.mark_run_running(
                context,
                run_id=input.run_id,
                execution_attempt=input.execution_attempt,
                worker_id=_activity_worker_id(),
                fencing_token_hash=_sha256(fencing_token.get_secret_value()),
            )
            _heartbeat(input.run_id, input.execution_attempt, "running")
            completion = await self._runtime_executor.execute(
                context,
                request=RunExecutionRequest(
                    tenant_id=input.tenant_id,
                    run_id=input.run_id,
                    execution_attempt=input.execution_attempt,
                    run_spec=input.run_spec,
                    timeout_seconds=input.timeout_seconds,
                    runtime_type=input.runtime_type,
                    fencing_token=fencing_token,
                ),
                event_publisher=self._runtime_event_publisher,
            )
            _heartbeat(input.run_id, input.execution_attempt, "completed")
            return completion
        except (ApplicationError, PlatformError, RunStageError) as error:
            _raise_activity_error(error)
        except Exception as error:
            raise ApplicationError(
                "The Runtime execution failed.",
                type="RUNTIME_EXECUTION_FAILED",
                non_retryable=True,
            ) from error

    @activity.defn(name="finalize_agent_run_v1")
    async def finalize_agent_run(
        self, input: FinalizeAgentRunInput
    ) -> FinalizeAgentRunResult:
        try:
            return await self._store.finalize_run(
                _context(input),
                input=input,
                fencing_token_hash=(
                    _fencing_token_hash(
                        self._fencing_token_issuer,
                        tenant_id=input.tenant_id,
                        run_id=input.run_id,
                        execution_attempt=input.execution_attempt,
                    )
                    if input.execution_attempt > 0
                    else None
                ),
            )
        except (ApplicationError, PlatformError, RunStageError) as error:
            _raise_activity_error(error)

    @activity.defn(name="inspect_agent_runtime_v1")
    async def inspect_agent_runtime(
        self, input: InspectAgentRuntimeInput
    ) -> RuntimeInspection:
        try:
            context = _context(input)
            source = await self._store.load_run_spec_source(
                context, run_id=input.run_id
            )
            if source is None:
                raise RunStageError("RUN_NOT_FOUND", "The Run is unavailable.")
            execution_attempt = input.execution_attempt or source.current_attempt
            if execution_attempt == 0:
                return RuntimeInspection(
                    run_id=input.run_id,
                    execution_attempt=0,
                    status="NOT_STARTED",
                )
            attempt = await self._store.load_run_attempt(
                context,
                run_id=input.run_id,
                execution_attempt=execution_attempt,
            )
            if attempt is None:
                raise RunStageError(
                    "RUN_ATTEMPT_NOT_FOUND", "The Run attempt is unavailable."
                )
            if attempt.status == "ALLOCATED":
                return RuntimeInspection(
                    run_id=input.run_id,
                    execution_attempt=execution_attempt,
                    status="NOT_STARTED",
                )
            controller = self._required_runtime_controller()
            inspection = await controller.inspect(
                context,
                request=RunRuntimeInspectionRequest(
                    tenant_id=input.tenant_id,
                    run_id=input.run_id,
                    execution_attempt=execution_attempt,
                    runtime_type=source.runtime_type,
                    runtime_handle_ref=attempt.runtime_handle_ref,
                ),
            )
            if (
                inspection.run_id != input.run_id
                or inspection.execution_attempt != execution_attempt
            ):
                raise RunStageError(
                    "RUNTIME_INSPECTION_MISMATCH",
                    "The Runtime inspection identity is invalid.",
                )
            return inspection
        except (ApplicationError, PlatformError, RunStageError) as error:
            _raise_activity_error(error)
        except Exception as error:
            raise ApplicationError(
                "The Runtime inspection failed.",
                type="RUNTIME_INSPECTION_FAILED",
                non_retryable=False,
            ) from error

    @activity.defn(name="cancel_agent_runtime_v1")
    async def cancel_agent_runtime(
        self, input: CancelAgentRuntimeInput
    ) -> RuntimeCancellationResult:
        try:
            context = _context(input)
            source = await self._store.load_run_spec_source(
                context, run_id=input.run_id
            )
            attempt = await self._store.load_run_attempt(
                context,
                run_id=input.run_id,
                execution_attempt=input.execution_attempt,
            )
            if source is None or attempt is None:
                raise RunStageError(
                    "RUN_ATTEMPT_NOT_FOUND", "The Run attempt is unavailable."
                )
            fencing_token = self._fencing_token_issuer.issue(
                tenant_id=input.tenant_id,
                run_id=input.run_id,
                execution_attempt=input.execution_attempt,
            )
            result = await self._required_runtime_controller().cancel(
                context,
                request=RunRuntimeCancellationRequest(
                    tenant_id=input.tenant_id,
                    run_id=input.run_id,
                    execution_attempt=input.execution_attempt,
                    runtime_type=source.runtime_type,
                    runtime_handle_ref=attempt.runtime_handle_ref,
                    fencing_token=fencing_token,
                ),
            )
            if (
                result.run_id != input.run_id
                or result.execution_attempt != input.execution_attempt
            ):
                raise RunStageError(
                    "RUNTIME_CANCELLATION_MISMATCH",
                    "The Runtime cancellation identity is invalid.",
                )
            return result
        except (ApplicationError, PlatformError, RunStageError) as error:
            _raise_activity_error(error)
        except Exception as error:
            raise ApplicationError(
                "The Runtime cancellation failed.",
                type="RUNTIME_CANCELLATION_FAILED",
                non_retryable=False,
            ) from error

    @activity.defn(name="recover_agent_run_v1")
    async def recover_agent_run(
        self, input: RecoverAgentRunInput
    ) -> RunPreparationResult:
        try:
            context = _context(input)
            source = await self._store.load_run_spec_source(
                context, run_id=input.run_id
            )
            if source is None:
                raise RunStageError("RUN_NOT_FOUND", "The Run is unavailable.")
            if (
                source.status != "RUNNING"
                or source.current_attempt != input.lost_execution_attempt
            ):
                raise RunStageError(
                    "RUN_RECOVERY_CONFLICT",
                    "The Run cannot be recovered from its current attempt.",
                )
            execution_attempt = input.lost_execution_attempt + 1
            fencing_token = self._fencing_token_issuer.issue(
                tenant_id=input.tenant_id,
                run_id=input.run_id,
                execution_attempt=execution_attempt,
            )
            await self._store.prepare_recovery_attempt(
                context,
                run_id=input.run_id,
                lost_execution_attempt=input.lost_execution_attempt,
                execution_attempt=execution_attempt,
                fencing_token_hash=_sha256(fencing_token.get_secret_value()),
            )
            run_spec = await self._run_spec_compiler.compile(
                context,
                source=source,
                execution_attempt=execution_attempt,
                fencing_token=fencing_token,
            )
            return RunPreparationResult(
                run_id=input.run_id,
                execution_attempt=execution_attempt,
                run_spec=run_spec,
                timeout_seconds=source.timeout_seconds,
                runtime_type=source.runtime_type,
            )
        except (ApplicationError, PlatformError, RunStageError) as error:
            _raise_activity_error(error)

    @activity.defn(name="finalize_agent_run_cancellation_v1")
    async def finalize_agent_run_cancellation(
        self, input: FinalizeAgentRunCancellationInput
    ) -> FinalizeAgentRunResult:
        try:
            return await self._store.finalize_cancellation(
                _context(input),
                input=input,
                fencing_token_hash=(
                    _fencing_token_hash(
                        self._fencing_token_issuer,
                        tenant_id=input.tenant_id,
                        run_id=input.run_id,
                        execution_attempt=input.execution_attempt,
                    )
                    if input.execution_attempt > 0
                    else None
                ),
            )
        except (ApplicationError, PlatformError, RunStageError) as error:
            _raise_activity_error(error)

    def _required_runtime_controller(self) -> RunRuntimeController:
        if self._runtime_controller is None:
            raise RunStageError(
                "RUNTIME_CONTROL_UNAVAILABLE",
                "Runtime inspection and cancellation are not configured.",
            )
        return self._runtime_controller


def _execution_attempt(
    source: RunSpecCompilationSource, input: AgentRunWorkflowInput
) -> int:
    if source.status == "CREATED":
        if source.current_attempt != 0:
            raise RunStageError(
                "RUN_ATTEMPT_INCONSISTENT",
                "The Run attempt state is inconsistent.",
            )
        return input.initial_execution_attempt
    if source.status in {"PREPARING", "RUNNING"} and source.current_attempt >= 1:
        return source.current_attempt
    raise RunStageError(
        "RUN_STATE_CONFLICT",
        "The Run cannot be prepared from its current state.",
    )


def _context(
    input: (
        AgentRunWorkflowInput
        | CancelAgentRuntimeInput
        | ExecuteAgentRunInput
        | FinalizeAgentRunCancellationInput
        | FinalizeAgentRunInput
        | InspectAgentRuntimeInput
        | RecoverAgentRunInput
    ),
) -> TenantContext:
    return TenantContext(
        tenant_id=str(input.tenant_id),
        subject_type=SubjectType.SERVICE,
        subject_id=str(input.run_id),
        auth_time=datetime.now(UTC),
        request_id=input.request_id,
        trace_id=input.trace_id,
    )


def _activity_workflow_id(input: AgentRunWorkflowInput) -> str:
    expected = f"run/{input.tenant_id}/{input.run_id}"
    try:
        actual = activity.info().workflow_id
    except RuntimeError:
        return expected
    if actual is not None and actual != expected:
        raise RunStageError(
            "WORKFLOW_ID_MISMATCH",
            "The Temporal Workflow identity does not match the Run.",
        )
    return expected


def _activity_worker_id() -> str:
    try:
        identity = activity.info().activity_id
    except RuntimeError:
        return "direct-test-worker"
    return identity or "temporal-run-worker"


def _heartbeat(run_id: UUID, execution_attempt: int, stage: str) -> None:
    try:
        activity.heartbeat(
            {
                "run_id": str(run_id),
                "execution_attempt": execution_attempt,
                "stage": stage,
            }
        )
    except RuntimeError:
        return


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _fencing_token_hash(
    issuer: FencingTokenIssuer,
    *,
    tenant_id: UUID,
    run_id: UUID,
    execution_attempt: int,
) -> str:
    return _sha256(
        issuer.issue(
            tenant_id=tenant_id,
            run_id=run_id,
            execution_attempt=execution_attempt,
        ).get_secret_value()
    )


def _raise_activity_error(error: Exception) -> NoReturn:
    if isinstance(error, ApplicationError):
        raise error
    if isinstance(error, RunStageError):
        raise ApplicationError(
            error.message,
            type=error.code,
            non_retryable=not error.retryable,
        ) from error
    if isinstance(error, PlatformError):
        raise ApplicationError(
            error.message,
            type=error.code,
            non_retryable=not error.retryable,
        ) from error
    raise ApplicationError(
        "The Run stage failed.",
        type="RUN_STAGE_FAILED",
        non_retryable=True,
    ) from error
