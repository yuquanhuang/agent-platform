import { beforeEach, describe, expect, it, vi } from 'vitest';

import { resourcesApiClient } from '@/services/api';
import { auditService } from '@/services/audit';

vi.mock('@/services/api', () => ({
  resourcesApiClient: {
    listAuditLogs: vi.fn(),
  },
}));

describe('auditService', () => {
  beforeEach(() => vi.clearAllMocks());

  it('uses the generated frozen audit client without adding a parallel DTO', async () => {
    vi.mocked(resourcesApiClient.listAuditLogs).mockResolvedValue({
      items: [],
      next_cursor: null,
      has_more: false,
    });
    const input = {
      limit: 50,
      cursor: 'audit-cursor',
      action: 'tool.execute',
      resourceType: 'tool',
      actorId: '11111111-1111-4111-8111-111111111111',
      runId: '22222222-2222-4222-8222-222222222222',
      occurredFrom: '2026-08-10T00:00:00.000Z',
      occurredTo: '2026-08-10T23:59:59.000Z',
    };

    await auditService.list(input);

    expect(resourcesApiClient.listAuditLogs).toHaveBeenCalledWith(input);
  });
});
