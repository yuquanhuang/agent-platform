import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { RunEventPage } from '@/api/generated/core-models';
import type { RunEvent } from '@/api/generated/run-event';
import { RunEventCoordinator } from '@/services/run-event-coordinator';
import type { RunProjectionKey } from '@/stores/run-projection';
import { useRunProjectionsStore } from '@/stores/run-projections';

const KEY: RunProjectionKey = {
  tenantId: 'tenant-001',
  sessionId: 'session-001',
  runId: 'run-001',
};

function event(sequenceNo: number, eventType: RunEvent['event_type'] = 'run_started'): RunEvent {
  const base = {
    schema_version: '1.0' as const,
    event_id: `event-${sequenceNo}`,
    source_event_id: `source-${sequenceNo}`,
    tenant_id: KEY.tenantId,
    run_id: KEY.runId,
    session_id: KEY.sessionId,
    sequence_no: sequenceNo,
    occurred_at: '2026-08-09T08:00:00Z',
    recorded_at: '2026-08-09T08:00:01Z',
    trace_id: 'trace-001',
    execution_attempt: 1,
    payload_version: '1.0' as const,
  };
  if (eventType === 'run_succeeded') {
    return {
      ...base,
      event_type: eventType,
      payload: {
        result_message_id: 'message-001',
        usage: { input_tokens: 1, output_tokens: 1, estimated: false },
        warnings: [],
      },
    };
  }
  if (eventType === 'warning') {
    return { ...base, event_type: eventType, payload: { code: 'NOTICE', message: 'notice' } };
  }
  return {
    ...base,
    event_type: 'run_started',
    payload: { runtime_type: 'agentscope', runtime_target_id: 'runtime-001' },
  };
}

function page(items: ReadonlyArray<RunEvent>, latestSequenceNo: number): RunEventPage {
  return { items, latest_sequence_no: latestSequenceNo, has_more: false };
}

function stream(...events: ReadonlyArray<RunEvent>) {
  return async function* (): AsyncGenerator<RunEvent> {
    for (const item of events) yield item;
  };
}

describe('RunEventCoordinator', () => {
  beforeEach(() => setActivePinia(createPinia()));

  it('replays history before opening SSE and closes only after terminal high-water catch-up', async () => {
    const calls: string[] = [];
    const listEvents = vi
      .fn()
      .mockImplementationOnce(async () => {
        calls.push('history:0');
        return page([event(1)], 1);
      })
      .mockImplementationOnce(async () => {
        calls.push('history:2');
        return page([], 2);
      });
    const openEventStream = vi.fn(async (_runId, input) => {
      calls.push(`stream:${input.lastSequenceNo}`);
      return new Response();
    });
    const coordinator = new RunEventCoordinator(
      KEY,
      useRunProjectionsStore(),
      { listEvents, openEventStream, readStream: stream(event(2, 'run_succeeded')) },
      { initialRetryDelayMs: 1, maxRetryDelayMs: 2 },
    );

    await coordinator.start();

    expect(calls).toEqual(['history:0', 'stream:1', 'history:2']);
    expect(coordinator.state.status).toBe('closed');
    expect(coordinator.state.projection.lastSequenceNo).toBe(2);
  });

  it('closes a gapped stream and fills the missing sequence from persisted history', async () => {
    const listEvents = vi
      .fn()
      .mockResolvedValueOnce(page([event(1)], 1))
      .mockResolvedValueOnce(page([event(2, 'warning'), event(3, 'run_succeeded')], 3));
    const coordinator = new RunEventCoordinator(KEY, useRunProjectionsStore(), {
      listEvents,
      openEventStream: vi.fn(async () => new Response()),
      readStream: stream(event(3, 'run_succeeded')),
    });

    await coordinator.start();

    expect(listEvents).toHaveBeenNthCalledWith(2, KEY.runId, expect.objectContaining({ after: 1 }));
    expect(coordinator.state.status).toBe('closed');
    expect(coordinator.state.projection.lastSequenceNo).toBe(3);
    expect(coordinator.state.projection.warnings).toHaveLength(1);
  });

  it('reconnects with Last-Event-ID and exponential backoff after disconnects', async () => {
    const delays: number[] = [];
    const lastSequenceNumbers: number[] = [];
    const listEvents = vi
      .fn()
      .mockResolvedValueOnce(page([event(1)], 1))
      .mockResolvedValueOnce(page([], 1))
      .mockResolvedValueOnce(page([], 1))
      .mockResolvedValueOnce(page([], 2));
    const readStream = vi
      .fn()
      .mockImplementationOnce(stream())
      .mockImplementationOnce(stream())
      .mockImplementationOnce(stream(event(2, 'run_succeeded')));
    const coordinator = new RunEventCoordinator(
      KEY,
      useRunProjectionsStore(),
      {
        listEvents,
        openEventStream: vi.fn(async (_runId, input) => {
          lastSequenceNumbers.push(input.lastSequenceNo ?? -1);
          return new Response();
        }),
        readStream,
        delay: async (milliseconds) => {
          delays.push(milliseconds);
        },
      },
      { initialRetryDelayMs: 10, maxRetryDelayMs: 40 },
    );

    await coordinator.start();

    expect(delays).toEqual([10, 20]);
    expect(lastSequenceNumbers).toEqual([1, 1, 1]);
    expect(coordinator.state.status).toBe('closed');
  });

  it('fails closed when persisted history contains a terminal conflict', async () => {
    const coordinator = new RunEventCoordinator(KEY, useRunProjectionsStore(), {
      listEvents: vi
        .fn()
        .mockResolvedValueOnce(page([event(1)], 1))
        .mockResolvedValueOnce(page([event(2, 'run_succeeded'), event(3, 'warning')], 3)),
      openEventStream: vi.fn(async () => new Response()),
      readStream: stream(event(2, 'run_succeeded')),
    });

    await coordinator.start();

    expect(coordinator.state.status).toBe('failed');
    expect(coordinator.state.errorCode).toBe('TERMINAL_CONFLICT');
    expect(coordinator.state.projection.terminal?.sequence_no).toBe(2);
  });

  it('stops the client subscription without changing the server Run', async () => {
    let observedSignal: AbortSignal | undefined;
    const coordinator = new RunEventCoordinator(KEY, useRunProjectionsStore(), {
      listEvents: vi.fn(async () => page([event(1)], 1)),
      openEventStream: vi.fn(async (_runId, input) => {
        observedSignal = input.signal;
        return new Response();
      }),
      readStream: async function* (_response, options) {
        yield* [] as RunEvent[];
        await new Promise<void>((resolve) => {
          options?.signal?.addEventListener('abort', () => resolve(), { once: true });
        });
      },
    });

    const running = coordinator.start();
    await vi.waitFor(() => expect(observedSignal).toBeDefined());
    coordinator.stop();
    await running;

    expect(observedSignal?.aborted).toBe(true);
    expect(coordinator.state.status).toBe('stopped');
  });
});
