import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Agent } from '@/api/generated/core-models';
import { coreApiClient } from '@/services/api';
import { agentService, buildAgentBindings } from '@/services/agents';

vi.mock('@/services/api', () => ({
  coreApiClient: {
    updateAgent: vi.fn(),
    listAgentReferences: vi.fn(),
  },
}));

const agent: Agent = {
  id: '11111111-1111-4111-8111-111111111111',
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

describe('agentService', () => {
  beforeEach(() => {
    vi.mocked(coreApiClient.updateAgent).mockResolvedValue(agent);
    vi.mocked(coreApiClient.listAgentReferences).mockResolvedValue({
      items: [],
      has_more: false,
    });
  });

  it('uses strong ETag for Agent Draft updates', async () => {
    await agentService.update(agent, { name: 'Updated Agent' });

    expect(coreApiClient.updateAgent).toHaveBeenCalledWith({
      agentId: agent.id,
      ifMatch: '"rv:3"',
      body: { name: 'Updated Agent' },
    });
  });

  it('places fallback policy only on the primary model binding', () => {
    const bindings = buildAgentBindings({
      prompt: {
        resourceId: 'prompt-1',
        versionPolicy: 'fixed',
        versionId: 'prompt-version-1',
      },
      models: [
        {
          resourceId: 'model-1',
          versionPolicy: 'fixed',
          versionId: 'model-version-1',
          fallbackErrorCodes: ['RATE_LIMITED', 'PROVIDER_UNAVAILABLE'],
        },
        {
          resourceId: 'model-2',
          versionPolicy: 'fixed',
          versionId: 'model-version-2',
        },
      ],
      childAgentIds: ['child-agent-1'],
    });

    expect(bindings).toEqual([
      expect.objectContaining({ resource_type: 'prompt', version_id: 'prompt-version-1' }),
      expect.objectContaining({
        resource_type: 'model',
        binding_role: 'primary',
        configuration_schema_version: 'model-routing/v1',
        configuration: {
          fallback_error_codes: ['RATE_LIMITED', 'PROVIDER_UNAVAILABLE'],
        },
      }),
      expect.objectContaining({
        resource_type: 'model',
        binding_role: 'fallback_1',
      }),
      {
        resource_type: 'agent',
        resource_id: 'child-agent-1',
        version_policy: 'resolve_on_publish',
      },
    ]);
    expect(bindings[2]).not.toHaveProperty('configuration');
  });
});
