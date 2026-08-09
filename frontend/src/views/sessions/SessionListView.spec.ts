import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query';
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Session } from '@/api/generated/core-models';
import { sessionService } from '@/services/sessions';
import SessionListView from '@/views/sessions/SessionListView.vue';

vi.mock('@/services/sessions', () => ({
  sessionService: {
    list: vi.fn(),
    get: vi.fn(),
    listMessages: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    archive: vi.fn(),
    delete: vi.fn(),
  },
}));

vi.mock('@/services/operations', () => ({
  operationService: { getByAccepted: vi.fn() },
}));

const session: Session = {
  id: '11111111-1111-4111-8111-111111111111',
  agent_id: '22222222-2222-4222-8222-222222222222',
  user_id: '33333333-3333-4333-8333-333333333333',
  default_deployment_id: '44444444-4444-4444-8444-444444444444',
  title: 'Support conversation',
  status: 'ACTIVE',
  resource_version: 3,
  created_at: '2026-08-08T00:00:00Z',
  updated_at: '2026-08-08T00:00:00Z',
};

function queryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

describe('SessionListView', () => {
  beforeEach(() => {
    vi.mocked(sessionService.list).mockResolvedValue({ items: [session], has_more: false });
  });

  it('shows the immutable Deployment binding and archive-before-delete flow', async () => {
    const wrapper = mount(SessionListView, {
      global: {
        plugins: [[VueQueryPlugin, { queryClient: queryClient() }]],
        stubs: { RouterLink: RouterLinkStub },
      },
    });
    await flushPromises();

    expect(wrapper.text()).toContain('Support conversation');
    expect(wrapper.text()).toContain(session.default_deployment_id);
    expect(wrapper.text()).toContain('归档');
    expect(wrapper.text()).toContain('查看消息');
    expect(wrapper.text()).not.toContain('删除将进入延迟删除状态');
  });

  it('offers deletion only after a Session is archived', async () => {
    vi.mocked(sessionService.list).mockResolvedValue({
      items: [{ ...session, status: 'ARCHIVED', resource_version: 4 }],
      has_more: false,
    });
    const wrapper = mount(SessionListView, {
      global: {
        plugins: [[VueQueryPlugin, { queryClient: queryClient() }]],
        stubs: { RouterLink: RouterLinkStub },
      },
    });
    await flushPromises();

    expect(wrapper.text()).toContain('删除');
    expect(wrapper.text()).not.toContain('归档后仍可查看');
  });
});
