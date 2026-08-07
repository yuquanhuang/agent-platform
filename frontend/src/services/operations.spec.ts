import { describe, expect, it, vi } from 'vitest';

import { coreApiClient } from '@/services/api';
import { operationIdFromStatusUrl, operationService } from '@/services/operations';

vi.mock('@/services/api', () => ({ coreApiClient: { getOperation: vi.fn() } }));

describe('operation service', () => {
  it('extracts the operation id only from the frozen status_url path', () => {
    expect(operationIdFromStatusUrl('/api/v1/operations/op%2F1')).toBe('op/1');
    expect(() => operationIdFromStatusUrl('https://example.com/operations/op-1')).toThrow();
  });

  it('polls through CoreApiClient using the accepted status_url', async () => {
    vi.mocked(coreApiClient.getOperation).mockResolvedValue({
      operation_id: 'op-1',
      operation_type: 'model_provider.connection_test',
      status: 'ACCEPTED',
      resource_type: 'model_provider',
      resource_id: 'provider-1',
      created_at: '2026-08-07T00:00:00Z',
      updated_at: '2026-08-07T00:00:00Z',
    });

    await operationService.getByAccepted({
      operation_id: 'op-1',
      status: 'ACCEPTED',
      status_url: '/api/v1/operations/op-1',
    });

    expect(coreApiClient.getOperation).toHaveBeenCalledWith({ operationId: 'op-1' });
  });
});
