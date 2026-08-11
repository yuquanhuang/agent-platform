import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createMemoryHistory, createRouter } from 'vue-router';

import type { CurrentIdentity, Run } from '@/api/generated/core-models';
import type { RunEvent, RunEventEnvelope } from '@/api/generated/run-event';
import { identityService } from '@/services/identity';
import { runService } from '@/services/runs';
import RunDetailView from '@/views/runs/RunDetailView.vue';

vi.mock('@/services/identity', () => ({
  identityService: { getCurrent: vi.fn() },
}));

vi.mock('@/services/runs', () => ({
  runService: {
    get: vi.fn(),
    listEvents: vi.fn(),
    openEventStream: vi.fn(),
  },
}));

const identity: CurrentIdentity = {
  user_id: 'user-001',
  external_subject: 'subject-001',
  display_name: 'Operator',
  active_tenant_id: 'tenant-001',
  memberships: [
    {
      tenant_id: 'tenant-001',
      tenant_name: 'Tenant',
      status: 'ACTIVE',
      role_ids: ['role-001'],
      membership_version: 1,
    },
  ],
  auth_time: '2026-08-09T08:00:00Z',
};

const run: Run = {
  id: 'run-001',
  session_id: 'session-001',
  snapshot_id: 'snapshot-001',
  deployment_id: 'deployment-001',
  status: 'RUNNING',
  created_at: '2026-08-09T08:00:00Z',
};

function envelope(sequenceNo: number): RunEventEnvelope {
  return {
    schema_version: '1.0',
    event_id: `event-${sequenceNo}`,
    source_event_id: `source-${sequenceNo}`,
    tenant_id: 'tenant-001',
    run_id: run.id,
    session_id: run.session_id,
    sequence_no: sequenceNo,
    occurred_at: '2026-08-09T08:00:00Z',
    recorded_at: '2026-08-09T08:00:01Z',
    trace_id: 'trace-001',
    execution_attempt: 1,
    payload_version: '1.0',
  };
}

function initialEvent(): RunEvent {
  return {
    ...envelope(1),
    event_type: 'run_created',
    payload: { deployment_id: run.deployment_id, snapshot_id: run.snapshot_id },
  };
}

function liveEvents(): ReadonlyArray<RunEvent> {
  return [
    {
      ...envelope(2),
      event_type: 'text_message_start',
      payload: { message_id: 'message-001', role: 'assistant' },
    },
    {
      ...envelope(3),
      event_type: 'text_delta',
      payload: { message_id: 'message-001', delta: '<img src=x onerror=alert(1)>' },
    },
    {
      ...envelope(4),
      event_type: 'text_message_end',
      payload: { message_id: 'message-001', finish_reason: 'stop' },
    },
    {
      ...envelope(5),
      event_type: 'warning',
      payload: { code: 'MODEL_SLOW', message: 'Model response was slow.' },
    },
    {
      ...envelope(6),
      event_type: 'run_succeeded',
      payload: {
        result_message_id: 'message-001',
        usage: { input_tokens: 5, output_tokens: 8, estimated: false },
        warnings: ['MODEL_SLOW'],
      },
    },
  ];
}

function sseResponse(events: ReadonlyArray<RunEvent>): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const event of events) {
          controller.enqueue(
            encoder.encode(
              `event: run_event\nid: ${event.sequence_no}\ndata: ${JSON.stringify(event)}\n\n`,
            ),
          );
        }
        controller.close();
      },
    }),
    { headers: { 'Content-Type': 'text/event-stream' } },
  );
}

function queryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

async function mountView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/runs/:id', component: RunDetailView }],
  });
  await router.push(`/runs/${run.id}`);
  await router.isReady();
  return mount(RunDetailView, {
    global: {
      plugins: [createPinia(), router, [VueQueryPlugin, { queryClient: queryClient() }]],
    },
  });
}

describe('RunDetailView', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(identityService.getCurrent).mockResolvedValue(identity);
    vi.mocked(runService.get).mockResolvedValue(run);
    vi.mocked(runService.listEvents)
      .mockResolvedValueOnce({ items: [initialEvent()], latest_sequence_no: 1, has_more: false })
      .mockResolvedValueOnce({ items: [], latest_sequence_no: 6, has_more: false });
    vi.mocked(runService.openEventStream).mockResolvedValue(sseResponse(liveEvents()));
  });

  it('renders validated history and SSE projections as safe text through terminal close', async () => {
    const wrapper = await mountView();
    expect(wrapper.find('.el-skeleton').exists()).toBe(true);

    await vi.waitFor(() => expect(wrapper.text()).toContain('已追平关闭'));

    expect(wrapper.text()).toContain('<img src=x onerror=alert(1)>');
    expect(wrapper.find('img').exists()).toBe(false);
    expect(wrapper.text()).toContain('Model response was slow.');
    expect(wrapper.text()).toContain('6');
    expect(runService.openEventStream).toHaveBeenCalledWith(
      run.id,
      expect.objectContaining({ lastSequenceNo: 1 }),
    );
  });

  it('fails closed on a malformed SSE frame and offers a reconnect action', async () => {
    vi.mocked(runService.openEventStream).mockResolvedValue(
      new Response('event: unexpected\nid: 2\ndata: {}\n\n'),
    );

    const wrapper = await mountView();
    await vi.waitFor(() => expect(wrapper.text()).toContain('SSE event name must be run_event.'));

    expect(wrapper.text()).toContain('INVALID_EVENT_NAME');
    expect(wrapper.get('button').text()).toContain('重新连接');
  });

  it('aborts only the browser subscription when the page unmounts', async () => {
    let streamSignal: AbortSignal | undefined;
    vi.mocked(runService.openEventStream).mockImplementation(async (_runId, input) => {
      streamSignal = input?.signal;
      return new Response(
        new ReadableStream<Uint8Array>({
          pull() {
            return new Promise<void>(() => undefined);
          },
        }),
      );
    });

    const wrapper = await mountView();
    await vi.waitFor(() => expect(streamSignal).toBeDefined());
    wrapper.unmount();
    await flushPromises();

    expect(streamSignal?.aborted).toBe(true);
  });

  it('does not subscribe without an active tenant context', async () => {
    vi.mocked(identityService.getCurrent).mockResolvedValue({
      ...identity,
      active_tenant_id: null,
    });

    const wrapper = await mountView();
    await flushPromises();

    expect(wrapper.text()).toContain('尚未选择活动租户');
    expect(runService.listEvents).not.toHaveBeenCalled();
    expect(runService.openEventStream).not.toHaveBeenCalled();
  });
});
