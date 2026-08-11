import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query';
import { flushPromises, mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/api/generated';
import type { Approval } from '@/api/generated/core-models';
import { approvalService } from '@/services/approvals';
import ApprovalCenterView from '@/views/approvals/ApprovalCenterView.vue';

vi.mock('@/services/approvals', () => ({
  approvalService: {
    list: vi.fn(),
    decide: vi.fn(),
  },
}));

const pendingApproval: Approval = {
  id: '11111111-1111-4111-8111-111111111111',
  run_id: '22222222-2222-4222-8222-222222222222',
  tool_name: 'production.write',
  parameter_digest: `sha256:${'a'.repeat(64)}`,
  status: 'PENDING',
  expires_at: '2099-08-10T10:30:00Z',
  resource_version: 1,
};

function queryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

async function mountView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/approvals', component: ApprovalCenterView },
      { path: '/runs/:id', name: 'run-detail', component: { template: '<div />' } },
    ],
  });
  await router.push('/approvals');
  await router.isReady();
  return mount(ApprovalCenterView, {
    global: { plugins: [router, [VueQueryPlugin, { queryClient: queryClient() }]] },
  });
}

describe('ApprovalCenterView', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(approvalService.list).mockResolvedValue({
      items: [pendingApproval],
      has_more: false,
    });
    vi.mocked(approvalService.decide).mockResolvedValue({
      ...pendingApproval,
      status: 'APPROVED',
      resource_version: 2,
    });
  });

  it('renders pending context and submits an approved decision once', async () => {
    const wrapper = await mountView();
    await flushPromises();

    expect(wrapper.text()).toContain('production.write');
    expect(wrapper.text()).toContain(pendingApproval.run_id);
    const approveButton = wrapper.findAll('button').find((button) => button.text() === '批准');
    await approveButton?.trigger('click');
    await flushPromises();
    const confirmButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('确认批准'));
    await confirmButton?.trigger('click');
    await flushPromises();

    expect(approvalService.decide).toHaveBeenCalledTimes(1);
    expect(approvalService.decide).toHaveBeenCalledWith(
      pendingApproval,
      { decision: 'APPROVED' },
      expect.stringMatching(/^approval-/),
    );
  });

  it('shows the self-approval denial and keeps the dialog retryable', async () => {
    vi.mocked(approvalService.decide).mockRejectedValue(
      new ApiError(403, { error: { code: 'PERMISSION_DENIED' } }),
    );
    const wrapper = await mountView();
    await flushPromises();

    const approveButton = wrapper.findAll('button').find((button) => button.text() === '批准');
    await approveButton?.trigger('click');
    await flushPromises();
    const confirmButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('确认批准'));
    await confirmButton?.trigger('click');
    await flushPromises();

    expect(wrapper.text()).toContain('自审批阻断');
    expect(wrapper.text()).toContain('批准审批');
  });

  it('disables decisions for locally observed expiry', async () => {
    vi.mocked(approvalService.list).mockResolvedValue({
      items: [{ ...pendingApproval, expires_at: '2020-01-01T00:00:00Z' }],
      has_more: false,
    });
    const wrapper = await mountView();
    await flushPromises();

    expect(wrapper.text()).toContain('服务端将拒绝决策');
    const approveButton = wrapper.findAll('button').find((button) => button.text() === '批准');
    expect(approveButton?.attributes('disabled')).toBeDefined();
  });
});
