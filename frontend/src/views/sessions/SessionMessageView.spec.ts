import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query';
import { flushPromises, mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createMemoryHistory, createRouter } from 'vue-router';

import type { MessagePage, Session } from '@/api/generated/core-models';
import { sessionService } from '@/services/sessions';
import SessionMessageView from '@/views/sessions/SessionMessageView.vue';

vi.mock('@/services/sessions', () => ({
  sessionService: {
    get: vi.fn(),
    listMessages: vi.fn(),
    listRuns: vi.fn(),
  },
}));

const session: Session = {
  id: '11111111-1111-4111-8111-111111111111',
  agent_id: '22222222-2222-4222-8222-222222222222',
  user_id: '33333333-3333-4333-8333-333333333333',
  default_deployment_id: '44444444-4444-4444-8444-444444444444',
  title: 'Branched conversation',
  status: 'ACTIVE',
  resource_version: 3,
  created_at: '2026-08-08T00:00:00Z',
  updated_at: '2026-08-08T00:00:00Z',
};

const messagePage: MessagePage = {
  items: [
    {
      id: '55555555-5555-4555-8555-555555555555',
      session_id: session.id,
      role: 'USER',
      content_parts: [
        { type: 'text', text: '<img src=x onerror=alert(1)>' },
        { type: 'artifact_reference', artifact_id: 'artifact-1' },
        { type: 'tool_reference', tool_call_id: 'tool-call-1' },
        { type: 'error_notice', error_code: 'MODEL_TIMEOUT', text: 'Retry later' },
      ],
      created_at: '2026-08-08T00:00:00Z',
    },
  ],
  next_cursor: 'next-cursor',
  has_more: true,
};

function queryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

async function mountView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/sessions/:id/messages', component: SessionMessageView },
      { path: '/runs/:id', name: 'run-detail', component: { template: '<div />' } },
    ],
  });
  await router.push(`/sessions/${session.id}/messages`);
  await router.isReady();
  return mount(SessionMessageView, {
    global: { plugins: [router, [VueQueryPlugin, { queryClient: queryClient() }]] },
  });
}

describe('SessionMessageView', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(sessionService.get).mockResolvedValue(session);
    vi.mocked(sessionService.listMessages).mockResolvedValue(messagePage);
    vi.mocked(sessionService.listRuns).mockResolvedValue({
      items: [],
      next_cursor: null,
      has_more: false,
    });
  });

  it('renders immutable content parts as safe text and supports branch filtering', async () => {
    const wrapper = await mountView();
    await flushPromises();

    expect(wrapper.text()).toContain('Branched conversation');
    expect(wrapper.text()).toContain('Artifact: artifact-1');
    expect(wrapper.text()).toContain('Tool call: tool-call-1');
    expect(wrapper.text()).toContain('MODEL_TIMEOUT: Retry later');
    expect(wrapper.find('img').exists()).toBe(false);

    await wrapper.get('input[aria-label="按分支 ID 筛选"]').setValue('branch-1');
    await wrapper.get('button').trigger('click');
    await flushPromises();

    expect(sessionService.listMessages).toHaveBeenLastCalledWith(
      session.id,
      expect.objectContaining({ branchId: 'branch-1' }),
    );
  });

  it('does not request message bodies for a deleted Session', async () => {
    vi.mocked(sessionService.get).mockResolvedValue({ ...session, status: 'DELETED' });

    const wrapper = await mountView();
    await flushPromises();

    expect(wrapper.text()).toContain('已删除 Session 不提供消息正文');
    expect(sessionService.listMessages).not.toHaveBeenCalled();
    expect(sessionService.listRuns).not.toHaveBeenCalled();
  });

  it('links persisted Session runs to the independent Run detail route', async () => {
    vi.mocked(sessionService.listRuns).mockResolvedValue({
      items: [
        {
          id: 'run-001',
          session_id: session.id,
          snapshot_id: 'snapshot-001',
          deployment_id: session.default_deployment_id,
          status: 'RUNNING',
          created_at: '2026-08-09T08:00:00Z',
        },
      ],
      next_cursor: null,
      has_more: false,
    });

    const wrapper = await mountView();
    await flushPromises();

    expect(wrapper.get('a[href="/runs/run-001"]').text()).toContain('run-001');
  });
});
