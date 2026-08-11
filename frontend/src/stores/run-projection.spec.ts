import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import type { RunEventPage } from '@/api/generated/core-models';
import type { RunEvent } from '@/api/generated/run-event';
import {
  canCloseRunProjection,
  createRunProjection,
  isRunProjectionCaughtUp,
  reduceRunEvent,
  reduceRunEventPage,
  RunProjectionIdentityError,
  RunProjectionSequenceError,
  type RunProjection,
  type RunProjectionKey,
} from '@/stores/run-projection';
import { useRunProjectionsStore } from '@/stores/run-projections';

const KEY: RunProjectionKey = {
  tenantId: 'tenant-1',
  sessionId: 'session-1',
  runId: 'run-1',
};

function event<T extends RunEvent['event_type']>(
  eventType: T,
  sequenceNo: number,
  payload: Extract<RunEvent, { event_type: T }>['payload'],
  key: RunProjectionKey = KEY,
): Extract<RunEvent, { event_type: T }> {
  return {
    schema_version: '1.0',
    event_id: `event-${sequenceNo}`,
    source_event_id: `source-${sequenceNo}`,
    tenant_id: key.tenantId,
    run_id: key.runId,
    session_id: key.sessionId,
    sequence_no: sequenceNo,
    event_type: eventType,
    occurred_at: '2026-08-09T00:00:00Z',
    recorded_at: '2026-08-09T00:00:01Z',
    trace_id: 'trace-1',
    execution_attempt: 1,
    payload_version: '1.0',
    payload,
  } as Extract<RunEvent, { event_type: T }>;
}

function reduceEvents(
  events: ReadonlyArray<RunEvent>,
  options: { canViewThinking?: boolean } = {},
): RunProjection {
  return events.reduce(
    (projection, current) => reduceRunEvent(projection, current, options),
    createRunProjection(KEY),
  );
}

describe('RunEvent reducer', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('projects ordered message events and ignores duplicates and older events', () => {
    const created = reduceRunEvent(
      createRunProjection(KEY),
      event('run_created', 1, {
        deployment_id: 'deployment-1',
        snapshot_id: 'snapshot-1',
      }),
    );
    const started = reduceRunEvent(
      created,
      event('text_message_start', 2, { message_id: 'message-1', role: 'assistant' }),
    );
    const withText = reduceRunEvent(
      started,
      event('text_delta', 3, { message_id: 'message-1', delta: 'hello' }),
    );
    const duplicate = reduceRunEvent(
      withText,
      event('text_delta', 3, { message_id: 'message-1', delta: 'duplicated' }),
    );
    const older = reduceRunEvent(
      duplicate,
      event('text_message_start', 2, { message_id: 'message-1', role: 'tool' }),
    );
    const completed = reduceRunEvent(
      older,
      event('text_message_end', 4, { message_id: 'message-1', finish_reason: 'stop' }),
    );

    expect(duplicate).toBe(withText);
    expect(older).toBe(withText);
    expect(completed.messages['message-1']).toEqual({
      messageId: 'message-1',
      role: 'assistant',
      content: 'hello',
      completed: true,
      finishReason: 'stop',
    });
    expect(completed.appliedEventCount).toBe(4);
  });

  it('pauses on a sequence gap and resumes only after the missing fact arrives', () => {
    const first = reduceRunEvent(
      createRunProjection(KEY),
      event('run_created', 1, {
        deployment_id: 'deployment-1',
        snapshot_id: 'snapshot-1',
      }),
    );
    const gap = reduceRunEvent(
      first,
      event('run_started', 3, {
        runtime_type: 'agentscope',
        runtime_target_id: 'runtime-1',
      }),
    );

    expect(gap.lastSequenceNo).toBe(1);
    expect(gap.runtime).toBeNull();
    expect(gap.gap).toEqual({ expectedSequenceNo: 2, observedSequenceNo: 3 });

    const filled = reduceRunEvent(
      gap,
      event('run_queued', 2, { queue: 'default', queued_at: '2026-08-09T00:00:00Z' }),
    );
    const replayed = reduceRunEvent(
      filled,
      event('run_started', 3, {
        runtime_type: 'agentscope',
        runtime_target_id: 'runtime-1',
      }),
    );

    expect(filled.gap).toBeNull();
    expect(replayed.lastSequenceNo).toBe(3);
    expect(replayed.runtime?.runtime_target_id).toBe('runtime-1');
  });

  it('projects plans, tools, approvals, tasks, artifacts and warnings by stable ids', () => {
    const projection = reduceEvents(
      [
        event('run_created', 1, {
          deployment_id: 'deployment-1',
          snapshot_id: 'snapshot-1',
        }),
        event('run_queued', 2, { queue: 'default', queued_at: '2026-08-09T00:00:00Z' }),
        event('run_started', 3, {
          runtime_type: 'agentscope',
          runtime_target_id: 'runtime-1',
        }),
        event('thinking_delta', 4, {
          message_id: 'thinking-1',
          delta: 'authorized thought',
          visibility: 'authorized_user',
        }),
        event('plan_updated', 5, {
          plan_id: 'plan-1',
          steps: [{ step_id: 'step-1', title: 'Inspect', status: 'in_progress' }],
        }),
        event('tool_call_start', 6, {
          tool_call_id: 'tool-1',
          tool_name: 'search',
          arguments_summary: 'query metadata',
        }),
        event('tool_call_args', 7, {
          tool_call_id: 'tool-1',
          arguments_patch: '{"query":',
        }),
        event('tool_call_args', 8, {
          tool_call_id: 'tool-1',
          arguments_patch: '"agent"}',
        }),
        event('tool_call_result', 9, {
          tool_call_id: 'tool-1',
          status: 'succeeded',
          result_summary: 'one result',
          artifact_refs: [{ artifact_id: 'artifact-1', name: 'report' }],
        }),
        event('approval_required', 10, {
          approval_id: 'approval-1',
          tool_name: 'deploy',
          parameter_digest: 'sha256:approval',
          expires_at: '2026-08-09T01:00:00Z',
        }),
        event('approval_resolved', 11, {
          approval_id: 'approval-1',
          decision: 'APPROVED',
          decided_by: 'user-1',
        }),
        event('task_progress', 12, {
          task_id: 'task-1',
          current: 1,
          total: 2,
          message: 'halfway',
        }),
        event('artifact_created', 13, {
          artifact_id: 'artifact-1',
          name: 'report.txt',
          content_type: 'text/plain',
          size: 12,
        }),
        event('warning', 14, { code: 'PARTIAL', message: 'partial result' }),
      ],
      { canViewThinking: true },
    );

    expect(projection.status).toBe('RUNNING');
    expect(projection.thinkingByMessageId['thinking-1']).toBe('authorized thought');
    expect(projection.plans['plan-1']?.steps[0]?.status).toBe('in_progress');
    expect(projection.toolCalls['tool-1']).toMatchObject({
      toolName: 'search',
      argumentsPatch: '{"query":"agent"}',
      status: 'succeeded',
      resultSummary: 'one result',
    });
    expect(projection.approvals['approval-1']).toMatchObject({
      status: 'APPROVED',
      decidedBy: 'user-1',
    });
    expect(projection.tasks['task-1']?.current).toBe(1);
    expect(projection.artifacts['artifact-1']?.name).toBe('report.txt');
    expect(projection.warnings).toEqual([{ code: 'PARTIAL', message: 'partial result' }]);
  });

  it('does not append thinking content without explicit viewing permission', () => {
    const projection = reduceRunEvent(
      createRunProjection(KEY),
      event('thinking_delta', 1, {
        message_id: 'thinking-1',
        delta: 'Sensitive thinking content was redacted.',
        visibility: 'debug_only',
      }),
    );

    expect(projection.lastSequenceNo).toBe(1);
    expect(projection.thinkingByMessageId).toEqual({});
    expect(projection.appliedEventCount).toBe(1);
  });

  it.each([
    ['APPROVED', 'RUNNING'],
    ['REJECTED', 'CANCELLING'],
    ['CANCELLED', 'CANCELLING'],
    ['EXPIRED', 'TIMEOUT'],
  ] as const)('maps approval decision %s to Run status %s', (decision, status) => {
    const waiting = reduceRunEvent(
      createRunProjection(KEY),
      event('approval_required', 1, {
        approval_id: 'approval-1',
        tool_name: 'deploy',
        parameter_digest: 'sha256:approval',
        expires_at: '2026-08-09T01:00:00Z',
      }),
    );
    const resolved = reduceRunEvent(
      waiting,
      event('approval_resolved', 2, {
        approval_id: 'approval-1',
        decision,
        decided_by: 'user-1',
      }),
    );

    expect(waiting.status).toBe('WAITING_APPROVAL');
    expect(resolved.status).toBe(status);
  });

  it.each([
    ['run_succeeded', 'SUCCEEDED'],
    ['run_failed', 'FAILED'],
    ['run_cancelled', 'CANCELLED'],
    ['run_timeout', 'TIMEOUT'],
  ] as const)('maps %s to the unique %s terminal', (eventType, status) => {
    const terminalPayloads = {
      run_succeeded: {
        result_message_id: 'message-1',
        usage: { input_tokens: 1, output_tokens: 2, estimated: false },
        warnings: [],
        result_quality: 'NORMAL',
      },
      run_failed: { error_code: 'MODEL_ERROR', message: 'failed', retryable: false },
      run_cancelled: { reason: 'user request', cancelled_by: 'user-1' },
      run_timeout: { timeout_seconds: 30, stage: 'runtime' },
    } as const;
    const terminalEvent = event(
      eventType,
      1,
      terminalPayloads[eventType] as Extract<RunEvent, { event_type: typeof eventType }>['payload'],
    );
    const projection = reduceRunEvent(createRunProjection(KEY), terminalEvent);

    expect(projection.status).toBe(status);
    expect(projection.terminal?.event_type).toBe(eventType);
  });

  it('records but does not apply facts observed after a terminal event', () => {
    const terminal = reduceRunEvent(
      createRunProjection(KEY),
      event('run_failed', 1, {
        error_code: 'MODEL_ERROR',
        message: 'failed',
        retryable: false,
      }),
    );
    const conflicted = reduceRunEvent(
      terminal,
      event('warning', 2, { code: 'LATE_EVENT', message: 'must not apply' }),
    );

    expect(conflicted.lastSequenceNo).toBe(1);
    expect(conflicted.warnings).toEqual([]);
    expect(conflicted.terminalConflict).toEqual({
      terminalSequenceNo: 1,
      observedSequenceNo: 2,
      observedEventType: 'warning',
    });
  });

  it('uses the page high-water mark before allowing a terminal stream to close', () => {
    const terminalEvent = event('run_succeeded', 2, {
      result_message_id: 'message-1',
      usage: { input_tokens: 1, output_tokens: 2, estimated: false },
      warnings: [],
      result_quality: 'NORMAL',
    });
    const page: RunEventPage = {
      items: [
        event('run_created', 1, {
          deployment_id: 'deployment-1',
          snapshot_id: 'snapshot-1',
        }),
        terminalEvent,
      ],
      has_more: false,
      latest_sequence_no: 2,
    };
    const complete = reduceRunEventPage(createRunProjection(KEY), page);

    expect(isRunProjectionCaughtUp(complete)).toBe(true);
    expect(canCloseRunProjection(complete)).toBe(true);

    const incomplete = reduceRunEventPage(createRunProjection(KEY), {
      items: page.items,
      has_more: false,
      latest_sequence_no: 3,
    });
    expect(incomplete.gap).toEqual({ expectedSequenceNo: 3, observedSequenceNo: null });
    expect(canCloseRunProjection(incomplete)).toBe(false);
  });

  it('fails closed for cross-run identity and invalid sequence values', () => {
    const wrongRun = event(
      'run_created',
      1,
      { deployment_id: 'deployment-1', snapshot_id: 'snapshot-1' },
      { ...KEY, runId: 'run-2' },
    );

    expect(() => reduceRunEvent(createRunProjection(KEY), wrongRun)).toThrow(
      RunProjectionIdentityError,
    );
    expect(() =>
      reduceRunEvent(createRunProjection(KEY), {
        ...wrongRun,
        run_id: KEY.runId,
        sequence_no: Number.NaN,
      }),
    ).toThrow(RunProjectionSequenceError);
  });

  it('isolates projections by tenant, session and run and supports tenant cleanup', () => {
    const store = useRunProjectionsStore();
    const otherKey = { ...KEY, tenantId: 'tenant-2' };

    store.applyEvent(
      event('run_created', 1, {
        deployment_id: 'deployment-1',
        snapshot_id: 'snapshot-1',
      }),
    );
    store.applyEvent(
      event(
        'run_created',
        1,
        { deployment_id: 'deployment-2', snapshot_id: 'snapshot-2' },
        otherKey,
      ),
    );

    expect(store.projectionFor(KEY)?.deploymentId).toBe('deployment-1');
    expect(store.projectionFor(otherKey)?.deploymentId).toBe('deployment-2');

    store.clearTenant(KEY.tenantId);

    expect(store.projectionFor(KEY)).toBeUndefined();
    expect(store.projectionFor(otherKey)?.deploymentId).toBe('deployment-2');
  });
});
