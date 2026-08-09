import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Session } from '@/api/generated/core-models';
import { coreApiClient } from '@/services/api';
import { sessionService } from '@/services/sessions';

vi.mock('@/services/api', () => ({
  coreApiClient: {
    listSessionMessages: vi.fn(),
    updateSession: vi.fn(),
    archiveSession: vi.fn(),
    deleteSession: vi.fn(),
  },
}));

const session: Session = {
  id: '11111111-1111-4111-8111-111111111111',
  agent_id: '22222222-2222-4222-8222-222222222222',
  user_id: '33333333-3333-4333-8333-333333333333',
  default_deployment_id: '44444444-4444-4444-8444-444444444444',
  title: 'Support conversation',
  status: 'ACTIVE',
  resource_version: 3,
  created_at: '2026-08-08T00:00:00Z',
  updated_at: '2026-08-08T00:00:00Z',
};

describe('sessionService', () => {
  beforeEach(() => {
    vi.mocked(coreApiClient.updateSession).mockResolvedValue(session);
    vi.mocked(coreApiClient.archiveSession).mockResolvedValue({
      ...session,
      status: 'ARCHIVED',
      resource_version: 4,
    });
  });

  it('uses the current strong ETag for updates and archive', async () => {
    await sessionService.update(session, { title: 'Renamed' });
    await sessionService.archive(session);

    expect(coreApiClient.updateSession).toHaveBeenCalledWith({
      sessionId: session.id,
      ifMatch: '"rv:3"',
      body: { title: 'Renamed' },
    });
    expect(coreApiClient.archiveSession).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: session.id, ifMatch: '"rv:3"' }),
    );
  });

  it('passes the frozen branch and cursor filters to message history', async () => {
    vi.mocked(coreApiClient.listSessionMessages).mockResolvedValue({
      items: [],
      has_more: false,
    });

    await sessionService.listMessages(session.id, {
      limit: 25,
      cursor: 'cursor-1',
      branchId: 'branch-1',
    });

    expect(coreApiClient.listSessionMessages).toHaveBeenCalledWith({
      sessionId: session.id,
      limit: 25,
      cursor: 'cursor-1',
      branchId: 'branch-1',
    });
  });

  it('keeps deletion asynchronous through the frozen Operation response', async () => {
    vi.mocked(coreApiClient.deleteSession).mockResolvedValue({
      operation_id: 'operation-1',
      status: 'ACCEPTED',
      status_url: '/api/v1/operations/operation-1',
    });

    const accepted = await sessionService.delete({
      id: session.id,
      resource_version: session.resource_version,
    });

    expect(accepted.status_url).toBe('/api/v1/operations/operation-1');
    expect(coreApiClient.deleteSession).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: session.id, ifMatch: '"rv:3"' }),
    );
  });
});
