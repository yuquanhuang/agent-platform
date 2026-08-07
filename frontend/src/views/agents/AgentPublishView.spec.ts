import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query';
import { flushPromises, mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Agent, PublishAgentPreview } from '@/api/generated/core-models';
import { agentService } from '@/services/agents';
import { releaseService } from '@/services/releases';
import { ElPopconfirm } from '@/ui/element-plus';
import AgentPublishView from '@/views/agents/AgentPublishView.vue';

vi.mock('@/services/agents', () => ({
  agentService: { get: vi.fn() },
}));

vi.mock('@/services/releases', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/services/releases')>();
  return {
    ...original,
    releaseService: {
      preview: vi.fn(),
      publish: vi.fn(),
      rollback: vi.fn(),
      get: vi.fn(),
      getDeployment: vi.fn(),
      listVersions: vi.fn(),
      getVersion: vi.fn(),
      diff: vi.fn(),
    },
  };
});

const agent: Agent = {
  id: '44444444-4444-4444-8444-444444444444',
  code: 'support_agent',
  name: 'Support Agent',
  runtime_type: 'agentscope',
  visibility: 'tenant',
  tags: [],
  bindings: [],
  status: 'DRAFT',
  resource_version: 3,
  created_at: '2026-08-07T00:00:00Z',
  updated_at: '2026-08-07T00:00:00Z',
};
const preview: PublishAgentPreview = {
  agent_id: agent.id,
  expected_agent_version: 3,
  preview_snapshot_hash: `sha256:${'a'.repeat(64)}`,
  resolved_bindings: [
    {
      resource_type: 'model',
      resource_id: 'model-1',
      version_id: 'model-version-1',
      version_no: 1,
      content_hash: `sha256:${'b'.repeat(64)}`,
      binding_role: 'primary',
    },
  ],
  targets: [
    {
      runtime_target_id: 'rt_agentscope_default',
      current_deployment_id: null,
      current_snapshot_id: null,
      changes: [
        {
          category: 'model',
          path: '/bindings/0',
          change_type: 'added',
          after: { type: 'object', hash: `sha256:${'c'.repeat(64)}` },
          sensitive: false,
        },
      ],
    },
  ],
  ready_to_publish: true,
};

function queryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

async function mountView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/agents/:id/publish', component: AgentPublishView },
      { path: '/agents/:id/edit', name: 'agent-edit', component: { template: '<div />' } },
      { path: '/agents', name: 'agent-list', component: { template: '<div />' } },
    ],
  });
  await router.push(`/agents/${agent.id}/publish`);
  await router.isReady();
  return mount(AgentPublishView, {
    global: { plugins: [router, [VueQueryPlugin, { queryClient: queryClient() }]] },
  });
}

describe('AgentPublishView', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(agentService.get).mockResolvedValue(agent);
    vi.mocked(releaseService.listVersions).mockResolvedValue({
      items: [],
      has_more: false,
    });
    vi.mocked(releaseService.preview).mockResolvedValue(preview);
    vi.mocked(releaseService.publish).mockResolvedValue({
      release_id: 'release-1',
      workflow_id: 'publish/tenant/release-1',
      status: 'REQUESTED',
      status_url: '/api/v1/releases/release-1',
    });
    vi.mocked(releaseService.rollback).mockResolvedValue({
      release_id: 'release-rollback',
      workflow_id: 'publish/tenant/release-rollback',
      status: 'REQUESTED',
      status_url: '/api/v1/releases/release-rollback',
    });
    vi.mocked(releaseService.get).mockResolvedValue({
      id: 'release-1',
      agent_id: agent.id,
      status: 'SUCCEEDED',
      workflow_id: 'publish/tenant/release-1',
      snapshot_id: 'snapshot-1',
      deployment_ids: ['deployment-1'],
      created_at: '2026-08-07T00:00:00Z',
    });
    vi.mocked(releaseService.getDeployment).mockResolvedValue({
      id: 'deployment-1',
      agent_id: agent.id,
      snapshot_id: 'snapshot-1',
      bundle_id: 'bundle-1',
      runtime_target_id: 'rt_agentscope_default',
      status: 'ACTIVE',
      compatibility_hash: `sha256:${'d'.repeat(64)}`,
      created_at: '2026-08-07T00:00:00Z',
    });
  });

  it('requires a current preview, publishes once and renders the effective Deployment', async () => {
    const wrapper = await mountView();
    await flushPromises();

    const publishButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('提交发布'));
    expect(publishButton?.attributes('disabled')).toBeDefined();

    const previewButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('生成发布预览'));
    await previewButton?.trigger('click');
    await flushPromises();

    expect(wrapper.text()).toContain('首次发布');
    expect(wrapper.text()).toContain('/bindings/0');
    await wrapper.get('textarea').setValue('Initial production release');
    expect(publishButton?.attributes('disabled')).toBeUndefined();
    await publishButton?.trigger('click');
    await flushPromises();

    expect(releaseService.publish).toHaveBeenCalledTimes(1);
    expect(releaseService.publish).toHaveBeenCalledWith(agent.id, {
      expected_agent_version: 3,
      runtime_targets: ['rt_agentscope_default'],
      release_note: 'Initial production release',
      run_smoke_test: true,
      activate_on_success: true,
    });
    expect(wrapper.text()).toContain('发布成功');
    expect(wrapper.text()).toContain('deployment-1');
  });

  it('invalidates the preview when Runtime Targets change', async () => {
    const wrapper = await mountView();
    await flushPromises();
    const previewButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('生成发布预览'));
    await previewButton?.trigger('click');
    await flushPromises();
    await wrapper.get('textarea').setValue('Release');

    const targetInput = wrapper
      .findAll('input')
      .find((input) => input.element.value.includes('rt_'));
    await targetInput?.setValue('rt_agentscope_other');
    await flushPromises();

    const publishButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('提交发布'));
    expect(wrapper.text()).toContain('需要预览');
    expect(publishButton?.attributes('disabled')).toBeDefined();
  });

  it('blocks a preview that is missing required publish bindings', async () => {
    vi.mocked(releaseService.preview).mockResolvedValue({
      ...preview,
      resolved_bindings: [],
      ready_to_publish: false,
    });
    const wrapper = await mountView();
    await flushPromises();
    const previewButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('生成发布预览'));
    await previewButton?.trigger('click');
    await flushPromises();
    await wrapper.get('textarea').setValue('Release');

    const publishButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('提交发布'));
    expect(wrapper.text()).toContain('缺少已发布 Sandbox Profile 绑定');
    expect(wrapper.text()).toContain('AgentScope 缺少已发布 ModelConfig 绑定');
    expect(publishButton?.attributes('disabled')).toBeDefined();
  });

  it('creates a new rollback Release from a selected historical Snapshot', async () => {
    vi.mocked(releaseService.listVersions).mockResolvedValue({
      items: [
        {
          id: 'version-1',
          agent_id: agent.id,
          version_no: 1,
          snapshot_id: 'snapshot-1',
          content_hash: `sha256:${'e'.repeat(64)}`,
          release_note: 'Initial',
          created_at: '2026-08-07T00:00:00Z',
        },
      ],
      has_more: false,
    });
    vi.mocked(releaseService.get).mockImplementation(async (releaseId) => ({
      id: releaseId,
      agent_id: agent.id,
      status: 'SUCCEEDED',
      workflow_id: `publish/tenant/${releaseId}`,
      snapshot_id: 'snapshot-1',
      deployment_ids: ['deployment-1'],
      created_at: '2026-08-07T00:00:00Z',
    }));
    const wrapper = await mountView();
    await flushPromises();

    wrapper.findComponent(ElPopconfirm).vm.$emit('confirm', new MouseEvent('click'));
    await flushPromises();

    expect(releaseService.rollback).toHaveBeenCalledWith(agent.id, {
      snapshot_id: 'snapshot-1',
      runtime_targets: ['rt_agentscope_default'],
      release_note: '回滚到 Agent v1',
    });
    expect(wrapper.text()).toContain('回滚成功');
  });
});
