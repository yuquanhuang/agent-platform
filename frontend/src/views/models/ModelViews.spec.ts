import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query';
import { flushPromises, mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Resource } from '@/api/generated/resources-models';
import { modelConfigService, modelProviderService } from '@/services/models';
import ModelConfigView from '@/views/models/ModelConfigView.vue';
import ModelProviderView from '@/views/models/ModelProviderView.vue';

vi.mock('@/services/models', () => ({
  modelProviderService: {
    list: vi.fn(),
    testConnection: vi.fn(),
  },
  modelConfigService: {
    list: vi.fn(),
    versions: vi.fn(),
    references: vi.fn(),
  },
}));

const provider: Resource = {
  id: '11111111-1111-4111-8111-111111111111',
  resource_type: 'model_provider',
  code: 'primary_openai',
  name: 'Primary OpenAI',
  visibility: 'tenant',
  content_schema_version: '1.0',
  content: {
    resource_type: 'model_provider',
    provider_type: 'openai',
    base_url: 'https://api.openai.com/v1',
    secret_ref: 'secret://tenant/tenant-a/model/openai',
    timeout_seconds: 30,
  },
  status: 'DRAFT',
  resource_version: 1,
  created_at: '2026-08-07T00:00:00Z',
  updated_at: '2026-08-07T00:00:00Z',
};

const config: Resource = {
  id: '22222222-2222-4222-8222-222222222222',
  resource_type: 'model_config',
  code: 'default_chat',
  name: 'Default Chat',
  visibility: 'tenant',
  content_schema_version: '1.0',
  content: {
    resource_type: 'model_config',
    provider_id: provider.id,
    model_id: 'gpt-5-mini',
    capabilities: ['stream', 'tools'],
    default_parameters: { temperature: 0.2 },
    max_context_tokens: 128000,
    rate_limit_rpm: 60,
  },
  status: 'ACTIVE',
  resource_version: 2,
  created_at: '2026-08-07T00:00:00Z',
  updated_at: '2026-08-07T00:00:00Z',
};

function queryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

describe('Model views', () => {
  beforeEach(() => {
    vi.mocked(modelProviderService.list).mockResolvedValue({
      items: [provider],
      has_more: false,
    });
    vi.mocked(modelConfigService.list).mockResolvedValue({
      items: [config],
      has_more: false,
    });
    vi.mocked(modelConfigService.versions).mockResolvedValue({ items: [], has_more: false });
    vi.mocked(modelConfigService.references).mockResolvedValue({ items: [], has_more: false });
  });

  it('renders a masked Secret Reference and provider status', async () => {
    const wrapper = mount(ModelProviderView, {
      global: { plugins: [[VueQueryPlugin, { queryClient: queryClient() }]] },
    });
    await flushPromises();

    expect(wrapper.text()).toContain('Primary OpenAI');
    expect(wrapper.text()).toContain('secret://tenant/tenant-a/model/•••/openai');
    expect(wrapper.text()).not.toContain('sk-');
  });

  it('renders Model Config model id and capabilities', async () => {
    const wrapper = mount(ModelConfigView, {
      global: { plugins: [[VueQueryPlugin, { queryClient: queryClient() }]] },
    });
    await flushPromises();

    expect(wrapper.text()).toContain('Default Chat');
    expect(wrapper.text()).toContain('gpt-5-mini');
    expect(wrapper.text()).toContain('stream');
    expect(wrapper.text()).toContain('tools');
  });
});
