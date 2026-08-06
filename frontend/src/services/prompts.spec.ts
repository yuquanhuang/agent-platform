import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Resource } from '@/api/generated/resources-models';
import { resourcesApiClient } from '@/services/api';
import { formatResourceEtag, promptService } from '@/services/prompts';

vi.mock('@/services/api', () => ({
  resourcesApiClient: {
    updatePrompt: vi.fn(),
    publishPrompt: vi.fn(),
  },
}));

const resource: Resource = {
  id: '11111111-1111-4111-8111-111111111111',
  resource_type: 'prompt',
  code: 'welcome_prompt',
  name: 'Welcome Prompt',
  visibility: 'tenant',
  content_schema_version: '1.0',
  content: {
    resource_type: 'prompt',
    template: 'Hello',
    variables: [],
    language: 'en',
    compiler_policy_version: '1',
  },
  status: 'DRAFT',
  resource_version: 7,
  created_at: '2026-08-06T00:00:00Z',
  updated_at: '2026-08-06T00:00:00Z',
};

describe('promptService', () => {
  beforeEach(() => {
    vi.mocked(resourcesApiClient.updatePrompt).mockResolvedValue(resource);
    vi.mocked(resourcesApiClient.publishPrompt).mockResolvedValue({
      id: '22222222-2222-4222-8222-222222222222',
      definition_id: resource.id,
      version_no: 1,
      content_hash: `sha256:${'a'.repeat(64)}`,
      release_note: 'Initial',
      published_at: '2026-08-06T00:00:00Z',
    });
  });

  it('uses the frozen strong ETag for updates', async () => {
    await promptService.update(resource, { name: 'Updated Prompt' });

    expect(formatResourceEtag(7)).toBe('"rv:7"');
    expect(resourcesApiClient.updatePrompt).toHaveBeenCalledWith({
      resourceId: resource.id,
      ifMatch: '"rv:7"',
      body: { name: 'Updated Prompt' },
    });
  });

  it('publishes against the currently loaded resource version', async () => {
    await promptService.publish(resource, 'Initial');

    expect(resourcesApiClient.publishPrompt).toHaveBeenCalledWith(
      expect.objectContaining({
        resourceId: resource.id,
        body: { expected_resource_version: 7, release_note: 'Initial' },
      }),
    );
  });
});
