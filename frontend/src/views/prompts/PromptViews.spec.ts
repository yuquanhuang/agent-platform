import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query';
import { flushPromises, mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/api/generated';
import type { Resource } from '@/api/generated/resources-models';
import { promptService } from '@/services/prompts';
import PromptEditorView from '@/views/prompts/PromptEditorView.vue';
import PromptListView from '@/views/prompts/PromptListView.vue';

vi.mock('@/services/prompts', () => ({
  promptService: {
    list: vi.fn(),
    get: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
    publish: vi.fn(),
    copy: vi.fn(),
    setEnabled: vi.fn(),
    rollback: vi.fn(),
    versions: vi.fn(),
    diff: vi.fn(),
    references: vi.fn(),
  },
}));

const resource: Resource = {
  id: '11111111-1111-4111-8111-111111111111',
  resource_type: 'prompt',
  code: 'welcome_prompt',
  name: 'Welcome Prompt',
  description: 'Greeting',
  visibility: 'tenant',
  content_schema_version: '1.0',
  content: {
    resource_type: 'prompt',
    template: 'Hello {{ name }}',
    variables: [
      {
        name: 'name',
        type: 'string',
        required: true,
        sensitive: false,
        max_length: 100,
      },
    ],
    language: 'en',
    compiler_policy_version: '1',
  },
  status: 'DRAFT',
  resource_version: 2,
  created_at: '2026-08-06T00:00:00Z',
  updated_at: '2026-08-06T00:00:00Z',
};

function queryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

describe('Prompt views', () => {
  beforeEach(() => {
    vi.mocked(promptService.list).mockResolvedValue({
      items: [resource],
      has_more: false,
    });
    vi.mocked(promptService.get).mockResolvedValue(resource);
    vi.mocked(promptService.versions).mockResolvedValue({ items: [], has_more: false });
    vi.mocked(promptService.references).mockResolvedValue({ items: [], has_more: false });
  });

  it('renders Prompt resources from Vue Query without copying them into Pinia', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/prompts', component: PromptListView }],
    });
    await router.push('/prompts');
    await router.isReady();
    const wrapper = mount(PromptListView, {
      global: { plugins: [router, [VueQueryPlugin, { queryClient: queryClient() }]] },
    });

    await flushPromises();

    expect(wrapper.text()).toContain('Welcome Prompt');
    expect(wrapper.text()).toContain('welcome_prompt');
  });

  it('keeps a version conflict visible and offers a reload action', async () => {
    vi.mocked(promptService.update).mockRejectedValue(
      new ApiError(409, { error: { code: 'RESOURCE_VERSION_CONFLICT' } }),
    );
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/prompts/:id/edit', component: PromptEditorView }],
    });
    await router.push(`/prompts/${resource.id}/edit`);
    await router.isReady();
    const wrapper = mount(PromptEditorView, {
      global: { plugins: [router, [VueQueryPlugin, { queryClient: queryClient() }]] },
    });
    await flushPromises();

    const textareas = wrapper.findAll('textarea');
    await textareas.at(-1)?.setValue('Changed template');
    const saveButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('保存草稿'));
    await saveButton?.trigger('click');
    await flushPromises();

    expect(wrapper.text()).toContain('当前修改未覆盖服务端内容');
    expect(wrapper.text()).toContain('重新加载最新版本');
  });
});
