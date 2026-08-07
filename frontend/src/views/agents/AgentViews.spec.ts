import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query';
import { flushPromises, mount } from '@vue/test-utils';
import { RouterView, createMemoryHistory, createRouter } from 'vue-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/api/generated';
import type { Agent } from '@/api/generated/core-models';
import type { Resource } from '@/api/generated/resources-models';
import { agentService } from '@/services/agents';
import { modelConfigService } from '@/services/models';
import { promptService } from '@/services/prompts';
import AgentEditorView from '@/views/agents/AgentEditorView.vue';
import AgentListView from '@/views/agents/AgentListView.vue';

vi.mock('@/services/agents', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/services/agents')>();
  return {
    ...original,
    agentService: {
      list: vi.fn(),
      get: vi.fn(),
      create: vi.fn(),
      update: vi.fn(),
      copy: vi.fn(),
      disable: vi.fn(),
      references: vi.fn(),
      delete: vi.fn(),
    },
  };
});

vi.mock('@/services/prompts', () => ({
  promptService: {
    list: vi.fn(),
    versions: vi.fn(),
  },
}));

vi.mock('@/services/models', () => ({
  modelConfigService: {
    list: vi.fn(),
    versions: vi.fn(),
  },
}));

const prompt: Resource = {
  id: '11111111-1111-4111-8111-111111111111',
  resource_type: 'prompt',
  code: 'support_prompt',
  name: 'Support Prompt',
  visibility: 'tenant',
  content_schema_version: '1.0',
  content: {
    resource_type: 'prompt',
    template: 'Help the user',
    variables: [],
    language: 'zh-CN',
    compiler_policy_version: '1',
  },
  status: 'ACTIVE',
  resource_version: 2,
  created_at: '2026-08-07T00:00:00Z',
  updated_at: '2026-08-07T00:00:00Z',
};

const modelConfig: Resource = {
  id: '22222222-2222-4222-8222-222222222222',
  resource_type: 'model_config',
  code: 'primary_model',
  name: 'Primary Model',
  visibility: 'tenant',
  content_schema_version: '1.0',
  content: {
    resource_type: 'model_config',
    provider_id: '33333333-3333-4333-8333-333333333333',
    model_id: 'gpt-5-mini',
    capabilities: ['stream', 'tools'],
    default_parameters: {},
    max_context_tokens: 128000,
    rate_limit_rpm: 60,
  },
  status: 'ACTIVE',
  resource_version: 2,
  created_at: '2026-08-07T00:00:00Z',
  updated_at: '2026-08-07T00:00:00Z',
};

const agent: Agent = {
  id: '44444444-4444-4444-8444-444444444444',
  code: 'support_agent',
  name: 'Support Agent',
  description: 'Customer support',
  runtime_type: 'agentscope',
  visibility: 'tenant',
  tags: ['support'],
  bindings: [
    {
      resource_type: 'prompt',
      resource_id: prompt.id,
      version_policy: 'fixed',
      version_id: 'prompt-version-1',
    },
    {
      resource_type: 'model',
      resource_id: modelConfig.id,
      version_policy: 'fixed',
      version_id: 'model-version-1',
      binding_role: 'primary',
    },
  ],
  status: 'DRAFT',
  resource_version: 2,
  created_at: '2026-08-07T00:00:00Z',
  updated_at: '2026-08-07T00:00:00Z',
};

function queryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function testRouter(path: string, component: typeof AgentListView | typeof AgentEditorView) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path, component }],
  });
  return router;
}

describe('Agent views', () => {
  beforeEach(() => {
    vi.mocked(agentService.list).mockResolvedValue({ items: [agent], has_more: false });
    vi.mocked(agentService.get).mockResolvedValue(agent);
    vi.mocked(agentService.references).mockResolvedValue({
      items: [
        {
          resource_type: 'agent',
          resource_id: '55555555-5555-4555-8555-555555555555',
          reference_type: 'child_agent',
        },
      ],
      has_more: false,
    });
    vi.mocked(promptService.list).mockResolvedValue({ items: [prompt], has_more: false });
    vi.mocked(promptService.versions).mockResolvedValue({
      items: [
        {
          id: 'prompt-version-1',
          definition_id: prompt.id,
          version_no: 1,
          content_hash: `sha256:${'a'.repeat(64)}`,
          published_at: '2026-08-07T00:00:00Z',
        },
      ],
      has_more: false,
    });
    vi.mocked(modelConfigService.list).mockResolvedValue({
      items: [modelConfig],
      has_more: false,
    });
    vi.mocked(modelConfigService.versions).mockResolvedValue({
      items: [
        {
          id: 'model-version-1',
          definition_id: modelConfig.id,
          version_no: 1,
          content_hash: `sha256:${'b'.repeat(64)}`,
          published_at: '2026-08-07T00:00:00Z',
        },
      ],
      has_more: false,
    });
  });

  it('renders Agent cards and blocks delete when references exist', async () => {
    const router = testRouter('/agents', AgentListView);
    await router.push('/agents');
    await router.isReady();
    const wrapper = mount(AgentListView, {
      global: { plugins: [router, [VueQueryPlugin, { queryClient: queryClient() }]] },
    });
    await flushPromises();

    expect(wrapper.text()).toContain('Support Agent');
    const deleteButton = wrapper.findAll('button').find((button) => button.text() === '删除');
    await deleteButton?.trigger('click');
    await flushPromises();

    expect(wrapper.text()).toContain('该 Agent 仍被引用，不能删除');
    expect(wrapper.text()).toContain('child_agent');
  });

  it('keeps local Agent changes visible after an ETag conflict', async () => {
    vi.mocked(agentService.update).mockRejectedValue(
      new ApiError(412, { error: { code: 'RESOURCE_VERSION_CONFLICT' } }),
    );
    const router = testRouter('/agents/:id/edit', AgentEditorView);
    await router.push(`/agents/${agent.id}/edit`);
    await router.isReady();
    const wrapper = mount(RouterView, {
      global: { plugins: [router, [VueQueryPlugin, { queryClient: queryClient() }]] },
    });
    await flushPromises();

    await wrapper.get('input[maxlength="100"]').setValue('Locally Updated Agent');
    const saveButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('保存草稿'));
    await saveButton?.trigger('click');
    await flushPromises();

    expect(wrapper.text()).toContain('当前修改未覆盖服务端内容');
    expect(wrapper.text()).toContain('重新加载最新版本');
    expect(wrapper.get('input[maxlength="100"]').element).toHaveProperty(
      'value',
      'Locally Updated Agent',
    );
  });

  it('disables saving when the Agent draft cannot be loaded', async () => {
    vi.mocked(agentService.get).mockRejectedValue(new Error('network unavailable'));
    const router = testRouter('/agents/:id/edit', AgentEditorView);
    await router.push(`/agents/${agent.id}/edit`);
    await router.isReady();
    const wrapper = mount(RouterView, {
      global: { plugins: [router, [VueQueryPlugin, { queryClient: queryClient() }]] },
    });
    await flushPromises();

    expect(wrapper.text()).toContain('加载失败');
    expect(wrapper.text()).toContain('Agent 草稿加载失败');
    const saveButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('保存草稿'));
    expect(saveButton?.attributes('disabled')).toBeDefined();
  });

  it('blocks saving when a required resource query fails', async () => {
    vi.mocked(promptService.list).mockRejectedValue(new Error('prompt registry unavailable'));
    const router = testRouter('/agents/:id/edit', AgentEditorView);
    await router.push(`/agents/${agent.id}/edit`);
    await router.isReady();
    const wrapper = mount(RouterView, {
      global: { plugins: [router, [VueQueryPlugin, { queryClient: queryClient() }]] },
    });
    await flushPromises();

    expect(wrapper.text()).toContain('依赖加载失败');
    const saveButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('保存草稿'));
    expect(saveButton?.attributes('disabled')).toBeDefined();
  });
});
