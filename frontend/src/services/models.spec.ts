import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Resource } from '@/api/generated/resources-models';
import { resourcesApiClient } from '@/services/api';
import { formatResourceEtag } from '@/services/prompts';
import { modelConfigService, modelProviderService } from '@/services/models';

vi.mock('@/services/api', () => ({
  resourcesApiClient: {
    updateModelProvider: vi.fn(),
    testModelProviderConnection: vi.fn(),
    publishModelConfig: vi.fn(),
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
  resource_version: 4,
  created_at: '2026-08-07T00:00:00Z',
  updated_at: '2026-08-07T00:00:00Z',
};

describe('model services', () => {
  beforeEach(() => {
    vi.mocked(resourcesApiClient.updateModelProvider).mockResolvedValue(provider);
    vi.mocked(resourcesApiClient.testModelProviderConnection).mockResolvedValue({
      operation_id: 'op-1',
      status: 'ACCEPTED',
      status_url: '/api/v1/operations/op-1',
    });
    vi.mocked(resourcesApiClient.publishModelConfig).mockResolvedValue({
      id: 'version-1',
      definition_id: 'config-1',
      version_no: 1,
      content_hash: `sha256:${'a'.repeat(64)}`,
      release_note: 'Initial',
      published_at: '2026-08-07T00:00:00Z',
    });
  });

  it('uses strong ETag for Provider updates', async () => {
    await modelProviderService.update(provider, { name: 'Updated Provider' });

    expect(formatResourceEtag(4)).toBe('"rv:4"');
    expect(resourcesApiClient.updateModelProvider).toHaveBeenCalledWith({
      resourceId: provider.id,
      ifMatch: '"rv:4"',
      body: { name: 'Updated Provider' },
    });
  });

  it('preserves status_url when requesting an asynchronous connection test', async () => {
    const accepted = await modelProviderService.testConnection(provider.id);

    expect(accepted.status).toBe('ACCEPTED');
    expect(accepted.status_url).toBe('/api/v1/operations/op-1');
    expect(resourcesApiClient.testModelProviderConnection).toHaveBeenCalledWith(
      expect.objectContaining({ resourceId: provider.id }),
    );
  });

  it('publishes a Model Config against its current resource version', async () => {
    await modelConfigService.publish(provider, 'Initial');

    expect(resourcesApiClient.publishModelConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        resourceId: provider.id,
        body: { expected_resource_version: 4, release_note: 'Initial' },
      }),
    );
  });
});
