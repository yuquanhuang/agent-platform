import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query';
import { flushPromises, mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { AuditRecord } from '@/api/generated/resources-models';
import { auditService } from '@/services/audit';
import AuditLogView from '@/views/admin/AuditLogView.vue';

vi.mock('@/services/audit', () => ({
  auditService: {
    list: vi.fn(),
  },
}));

const record: AuditRecord = {
  event_id: '11111111-1111-4111-8111-111111111111',
  occurred_at: '2026-08-10T08:00:00Z',
  actor_id: '22222222-2222-4222-8222-222222222222',
  action: 'tool.execute',
  resource_type: 'tool',
  resource_id: '33333333-3333-4333-8333-333333333333',
  result: 'SUCCESS',
  trace_id: 'trace-audit-001',
};

function queryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function mountView() {
  return mount(AuditLogView, {
    global: { plugins: [[VueQueryPlugin, { queryClient: queryClient() }]] },
  });
}

describe('AuditLogView', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(auditService.list).mockResolvedValue({
      items: [record],
      next_cursor: 'next-audit-page',
      has_more: true,
    });
  });

  it('renders only the frozen minimal audit projection and its privacy boundary', async () => {
    const wrapper = mountView();
    await flushPromises();

    expect(wrapper.text()).toContain('tool.execute');
    expect(wrapper.text()).toContain(record.actor_id);
    expect(wrapper.text()).toContain(record.trace_id);
    expect(wrapper.text()).toContain('Secret、Ticket nonce、完整参数和原始内容不会返回');
    expect(wrapper.text()).not.toContain('metadata_json');
  });

  it('submits filters and advances using the server cursor', async () => {
    const wrapper = mountView();
    await flushPromises();

    await wrapper.get('input[aria-label="动作"]').setValue('  tool.execute  ');
    await wrapper.get('input[aria-label="资源类型"]').setValue(' tool ');
    await wrapper.get('input[aria-label="操作者 ID"]').setValue(record.actor_id);
    await wrapper.get('input[aria-label="Run ID"]').setValue(record.resource_id);
    const searchButton = wrapper.findAll('button').find((button) => button.text() === '查询');
    await searchButton?.trigger('click');
    await flushPromises();

    expect(auditService.list).toHaveBeenLastCalledWith(
      expect.objectContaining({
        limit: 100,
        action: 'tool.execute',
        resourceType: 'tool',
        actorId: record.actor_id,
        runId: record.resource_id,
      }),
    );

    const nextButton = wrapper.findAll('button').find((button) => button.text() === '下一页');
    await nextButton?.trigger('click');
    await flushPromises();

    expect(auditService.list).toHaveBeenLastCalledWith(
      expect.objectContaining({ cursor: 'next-audit-page' }),
    );
  });

  it('shows an actionable error state without exposing backend details', async () => {
    vi.mocked(auditService.list).mockRejectedValue(new Error('database credentials leaked'));
    const wrapper = mountView();
    await flushPromises();

    expect(wrapper.text()).toContain('审计日志加载失败');
    expect(wrapper.text()).toContain('重新加载');
    expect(wrapper.text()).not.toContain('database credentials leaked');
  });
});
