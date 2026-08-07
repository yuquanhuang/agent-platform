import { beforeEach, describe, expect, it, vi } from 'vitest';

import { coreApiClient } from '@/services/api';
import { normalizeRuntimeTargets, releaseService } from '@/services/releases';

vi.mock('@/services/api', () => ({
  coreApiClient: {
    previewAgentPublish: vi.fn(),
    publishAgent: vi.fn(),
    rollbackAgent: vi.fn(),
    getRelease: vi.fn(),
    getDeployment: vi.fn(),
    listAgentVersions: vi.fn(),
    getAgentVersion: vi.fn(),
    diffAgentSnapshots: vi.fn(),
  },
}));

describe('releaseService', () => {
  beforeEach(() => vi.clearAllMocks());

  it('normalizes unique Runtime Targets deterministically', () => {
    expect(normalizeRuntimeTargets(' rt-b,rt-a,rt-b ,, ')).toEqual(['rt-a', 'rt-b']);
  });

  it('uses the generated read-only preview operation', async () => {
    vi.mocked(coreApiClient.previewAgentPublish).mockResolvedValue({
      agent_id: 'agent-1',
      expected_agent_version: 2,
      preview_snapshot_hash: `sha256:${'a'.repeat(64)}`,
      resolved_bindings: [],
      targets: [],
      ready_to_publish: true,
    });

    await releaseService.preview('agent-1', {
      expected_agent_version: 2,
      runtime_targets: ['rt-a'],
    });

    expect(coreApiClient.previewAgentPublish).toHaveBeenCalledWith({
      agentId: 'agent-1',
      body: { expected_agent_version: 2, runtime_targets: ['rt-a'] },
    });
  });

  it('uses the frozen rollback operation with a fresh idempotency key', async () => {
    vi.mocked(coreApiClient.rollbackAgent).mockResolvedValue({
      release_id: 'release-rollback',
      workflow_id: 'publish/tenant/release-rollback',
      status: 'REQUESTED',
      status_url: '/api/v1/releases/release-rollback',
    });

    await releaseService.rollback('agent-1', {
      snapshot_id: 'snapshot-1',
      runtime_targets: ['rt-a'],
      release_note: 'Rollback to v1',
    });

    expect(coreApiClient.rollbackAgent).toHaveBeenCalledWith({
      agentId: 'agent-1',
      idempotencyKey: expect.any(String),
      body: {
        snapshot_id: 'snapshot-1',
        runtime_targets: ['rt-a'],
        release_note: 'Rollback to v1',
      },
    });
  });
});
