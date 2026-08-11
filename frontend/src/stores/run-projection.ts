import type { RunEventPage, RunStatus } from '@/api/generated/core-models';
import type {
  ApprovalResolvedPayload,
  ArtifactCreatedPayload,
  ArtifactRef,
  PlanUpdatedPayload,
  RunEvent,
  RunStartedPayload,
  TaskProgressPayload,
  TextMessageEndPayload,
  ToolCallResultPayload,
  WarningPayload,
} from '@/api/generated/run-event';

export interface RunProjectionKey {
  readonly tenantId: string;
  readonly sessionId: string;
  readonly runId: string;
}

export interface RunProjectionGap {
  readonly expectedSequenceNo: number;
  readonly observedSequenceNo: number | null;
}

export interface RunProjectionTerminalConflict {
  readonly terminalSequenceNo: number;
  readonly observedSequenceNo: number;
  readonly observedEventType: RunEvent['event_type'];
}

export interface RunMessageProjection {
  readonly messageId: string;
  readonly role: 'assistant' | 'tool' | 'unknown';
  readonly content: string;
  readonly completed: boolean;
  readonly finishReason: TextMessageEndPayload['finish_reason'] | null;
}

export interface RunToolCallProjection {
  readonly toolCallId: string;
  readonly toolName: string | null;
  readonly argumentsSummary: string;
  readonly argumentsPatch: string;
  readonly status: 'pending' | 'streaming' | ToolCallResultPayload['status'];
  readonly resultSummary: string;
  readonly artifactRefs: ReadonlyArray<ArtifactRef>;
  readonly errorCode: string | null;
}

export interface RunApprovalProjection {
  readonly approvalId: string;
  readonly toolName: string | null;
  readonly parameterDigest: string | null;
  readonly expiresAt: string | null;
  readonly status: 'PENDING' | ApprovalResolvedPayload['decision'];
  readonly decidedBy: string | null;
}

export type TerminalRunEvent = Extract<
  RunEvent,
  {
    event_type: 'run_succeeded' | 'run_failed' | 'run_cancelled' | 'run_timeout';
  }
>;

export interface RunProjection {
  readonly key: RunProjectionKey;
  readonly status: RunStatus | 'UNKNOWN';
  readonly lastSequenceNo: number;
  readonly appliedEventCount: number;
  readonly knownLatestSequenceNo: number | null;
  readonly gap: RunProjectionGap | null;
  readonly terminalConflict: RunProjectionTerminalConflict | null;
  readonly deploymentId: string | null;
  readonly snapshotId: string | null;
  readonly queue: string | null;
  readonly queuedAt: string | null;
  readonly runtime: RunStartedPayload | null;
  readonly messages: Readonly<Record<string, RunMessageProjection>>;
  readonly messageOrder: ReadonlyArray<string>;
  readonly thinkingByMessageId: Readonly<Record<string, string>>;
  readonly plans: Readonly<Record<string, PlanUpdatedPayload>>;
  readonly activePlanId: string | null;
  readonly toolCalls: Readonly<Record<string, RunToolCallProjection>>;
  readonly toolCallOrder: ReadonlyArray<string>;
  readonly approvals: Readonly<Record<string, RunApprovalProjection>>;
  readonly approvalOrder: ReadonlyArray<string>;
  readonly tasks: Readonly<Record<string, TaskProgressPayload>>;
  readonly artifacts: Readonly<Record<string, ArtifactCreatedPayload>>;
  readonly artifactOrder: ReadonlyArray<string>;
  readonly warnings: ReadonlyArray<WarningPayload>;
  readonly terminal: TerminalRunEvent | null;
}

export interface RunProjectionOptions {
  readonly canViewThinking?: boolean;
}

export class RunProjectionIdentityError extends Error {
  constructor() {
    super('RunEvent identity does not match the target projection.');
    this.name = 'RunProjectionIdentityError';
  }
}

export class RunProjectionSequenceError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'RunProjectionSequenceError';
  }
}

export function createRunProjection(key: RunProjectionKey, lastSequenceNo = 0): RunProjection {
  assertSequenceNo(lastSequenceNo, true);
  return {
    key,
    status: 'UNKNOWN',
    lastSequenceNo,
    appliedEventCount: 0,
    knownLatestSequenceNo: null,
    gap: null,
    terminalConflict: null,
    deploymentId: null,
    snapshotId: null,
    queue: null,
    queuedAt: null,
    runtime: null,
    messages: {},
    messageOrder: [],
    thinkingByMessageId: {},
    plans: {},
    activePlanId: null,
    toolCalls: {},
    toolCallOrder: [],
    approvals: {},
    approvalOrder: [],
    tasks: {},
    artifacts: {},
    artifactOrder: [],
    warnings: [],
    terminal: null,
  };
}

export function runProjectionKeyFromEvent(event: RunEvent): RunProjectionKey {
  return {
    tenantId: event.tenant_id,
    sessionId: event.session_id,
    runId: event.run_id,
  };
}

export function runProjectionKeyId(key: RunProjectionKey): string {
  return JSON.stringify([key.tenantId, key.sessionId, key.runId]);
}

export function reduceRunEvent(
  state: RunProjection,
  event: RunEvent,
  options: RunProjectionOptions = {},
): RunProjection {
  assertIdentity(state.key, event);
  assertSequenceNo(event.sequence_no, false);

  if (event.sequence_no <= state.lastSequenceNo) return state;

  const expectedSequenceNo = state.lastSequenceNo + 1;
  if (event.sequence_no !== expectedSequenceNo) {
    if (
      state.gap?.expectedSequenceNo === expectedSequenceNo &&
      state.gap.observedSequenceNo === event.sequence_no
    ) {
      return state;
    }
    return {
      ...state,
      gap: { expectedSequenceNo, observedSequenceNo: event.sequence_no },
    };
  }

  if (state.terminal !== null) {
    return {
      ...state,
      terminalConflict: {
        terminalSequenceNo: state.terminal.sequence_no,
        observedSequenceNo: event.sequence_no,
        observedEventType: event.event_type,
      },
    };
  }

  const projected = projectRunEvent(state, event, options);
  return {
    ...projected,
    lastSequenceNo: event.sequence_no,
    appliedEventCount: state.appliedEventCount + 1,
    gap: null,
  };
}

export function reduceRunEventPage(
  state: RunProjection,
  page: RunEventPage,
  options: RunProjectionOptions = {},
): RunProjection {
  assertSequenceNo(page.latest_sequence_no, true);
  let projected: RunProjection = {
    ...state,
    knownLatestSequenceNo: Math.max(state.knownLatestSequenceNo ?? 0, page.latest_sequence_no),
  };
  for (const event of page.items) {
    projected = reduceRunEvent(projected, event, options);
  }
  if (
    !page.has_more &&
    projected.gap === null &&
    projected.lastSequenceNo < page.latest_sequence_no
  ) {
    return {
      ...projected,
      gap: {
        expectedSequenceNo: projected.lastSequenceNo + 1,
        observedSequenceNo: null,
      },
    };
  }
  return projected;
}

export function isRunProjectionCaughtUp(state: RunProjection): boolean {
  return (
    state.gap === null &&
    state.knownLatestSequenceNo !== null &&
    state.lastSequenceNo >= state.knownLatestSequenceNo
  );
}

export function canCloseRunProjection(state: RunProjection): boolean {
  return (
    state.terminal !== null && state.terminalConflict === null && isRunProjectionCaughtUp(state)
  );
}

function projectRunEvent(
  state: RunProjection,
  event: RunEvent,
  options: RunProjectionOptions,
): RunProjection {
  switch (event.event_type) {
    case 'run_created':
      return {
        ...state,
        status: 'CREATED',
        deploymentId: event.payload.deployment_id,
        snapshotId: event.payload.snapshot_id,
      };
    case 'run_queued':
      return {
        ...state,
        status: 'QUEUED',
        queue: event.payload.queue,
        queuedAt: event.payload.queued_at,
      };
    case 'run_started':
      return { ...state, status: 'RUNNING', runtime: event.payload };
    case 'text_message_start':
      return updateMessage(state, event.payload.message_id, (message) => ({
        ...message,
        role: event.payload.role,
      }));
    case 'text_delta':
      return updateMessage(state, event.payload.message_id, (message) => ({
        ...message,
        content: message.content + event.payload.delta,
      }));
    case 'text_message_end':
      return updateMessage(state, event.payload.message_id, (message) => ({
        ...message,
        completed: true,
        finishReason: event.payload.finish_reason,
      }));
    case 'thinking_delta':
      if (options.canViewThinking !== true) return state;
      return {
        ...state,
        thinkingByMessageId: {
          ...state.thinkingByMessageId,
          [event.payload.message_id]:
            (state.thinkingByMessageId[event.payload.message_id] ?? '') + event.payload.delta,
        },
      };
    case 'plan_updated':
      return {
        ...state,
        plans: { ...state.plans, [event.payload.plan_id]: event.payload },
        activePlanId: event.payload.plan_id,
      };
    case 'tool_call_start':
      return updateToolCall(state, event.payload.tool_call_id, (toolCall) => ({
        ...toolCall,
        toolName: event.payload.tool_name,
        argumentsSummary: event.payload.arguments_summary,
        status: 'pending',
      }));
    case 'tool_call_args':
      return updateToolCall(state, event.payload.tool_call_id, (toolCall) => ({
        ...toolCall,
        argumentsPatch: toolCall.argumentsPatch + event.payload.arguments_patch,
        status: 'streaming',
      }));
    case 'tool_call_result':
      return updateToolCall(state, event.payload.tool_call_id, (toolCall) => ({
        ...toolCall,
        status: event.payload.status,
        resultSummary: event.payload.result_summary,
        artifactRefs: event.payload.artifact_refs,
        errorCode: event.payload.error_code ?? null,
      }));
    case 'approval_required':
      return updateApproval(
        { ...state, status: 'WAITING_APPROVAL' },
        event.payload.approval_id,
        (approval) => ({
          ...approval,
          toolName: event.payload.tool_name,
          parameterDigest: event.payload.parameter_digest,
          expiresAt: event.payload.expires_at,
          status: 'PENDING',
        }),
      );
    case 'approval_resolved':
      return updateApproval(
        { ...state, status: statusAfterApproval(event.payload.decision) },
        event.payload.approval_id,
        (approval) => ({
          ...approval,
          status: event.payload.decision,
          decidedBy: event.payload.decided_by,
        }),
      );
    case 'task_progress':
      return {
        ...state,
        tasks: { ...state.tasks, [event.payload.task_id]: event.payload },
      };
    case 'artifact_created':
      return {
        ...state,
        artifacts: {
          ...state.artifacts,
          [event.payload.artifact_id]: event.payload,
        },
        artifactOrder:
          state.artifacts[event.payload.artifact_id] === undefined
            ? [...state.artifactOrder, event.payload.artifact_id]
            : state.artifactOrder,
      };
    case 'warning':
      return { ...state, warnings: [...state.warnings, event.payload] };
    case 'run_succeeded':
      return { ...state, status: 'SUCCEEDED', terminal: event };
    case 'run_failed':
      return { ...state, status: 'FAILED', terminal: event };
    case 'run_cancelled':
      return { ...state, status: 'CANCELLED', terminal: event };
    case 'run_timeout':
      return { ...state, status: 'TIMEOUT', terminal: event };
    default:
      return assertNever(event);
  }
}

function updateMessage(
  state: RunProjection,
  messageId: string,
  update: (message: RunMessageProjection) => RunMessageProjection,
): RunProjection {
  const existing = state.messages[messageId];
  const message = update(
    existing ?? {
      messageId,
      role: 'unknown',
      content: '',
      completed: false,
      finishReason: null,
    },
  );
  return {
    ...state,
    messages: { ...state.messages, [messageId]: message },
    messageOrder: existing === undefined ? [...state.messageOrder, messageId] : state.messageOrder,
  };
}

function updateToolCall(
  state: RunProjection,
  toolCallId: string,
  update: (toolCall: RunToolCallProjection) => RunToolCallProjection,
): RunProjection {
  const existing = state.toolCalls[toolCallId];
  const toolCall = update(
    existing ?? {
      toolCallId,
      toolName: null,
      argumentsSummary: '',
      argumentsPatch: '',
      status: 'pending',
      resultSummary: '',
      artifactRefs: [],
      errorCode: null,
    },
  );
  return {
    ...state,
    toolCalls: { ...state.toolCalls, [toolCallId]: toolCall },
    toolCallOrder:
      existing === undefined ? [...state.toolCallOrder, toolCallId] : state.toolCallOrder,
  };
}

function updateApproval(
  state: RunProjection,
  approvalId: string,
  update: (approval: RunApprovalProjection) => RunApprovalProjection,
): RunProjection {
  const existing = state.approvals[approvalId];
  const approval = update(
    existing ?? {
      approvalId,
      toolName: null,
      parameterDigest: null,
      expiresAt: null,
      status: 'PENDING',
      decidedBy: null,
    },
  );
  return {
    ...state,
    approvals: { ...state.approvals, [approvalId]: approval },
    approvalOrder:
      existing === undefined ? [...state.approvalOrder, approvalId] : state.approvalOrder,
  };
}

function assertIdentity(key: RunProjectionKey, event: RunEvent): void {
  if (
    key.tenantId !== event.tenant_id ||
    key.sessionId !== event.session_id ||
    key.runId !== event.run_id
  ) {
    throw new RunProjectionIdentityError();
  }
}

function statusAfterApproval(decision: ApprovalResolvedPayload['decision']): RunStatus {
  switch (decision) {
    case 'APPROVED':
      return 'RUNNING';
    case 'REJECTED':
    case 'CANCELLED':
      return 'CANCELLING';
    case 'EXPIRED':
      return 'TIMEOUT';
  }
}

function assertSequenceNo(sequenceNo: number, allowZero: boolean): void {
  const minimum = allowZero ? 0 : 1;
  if (!Number.isSafeInteger(sequenceNo) || sequenceNo < minimum) {
    throw new RunProjectionSequenceError(
      `RunEvent sequence must be a safe integer greater than or equal to ${minimum}.`,
    );
  }
}

function assertNever(value: never): never {
  void value;
  throw new Error('Unsupported RunEvent type.');
}
