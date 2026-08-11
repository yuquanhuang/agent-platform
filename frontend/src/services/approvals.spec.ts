import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Approval } from '@/api/generated/core-models';
import { coreApiClient } from '@/services/api';
import { approvalService } from '@/services/approvals';

vi.mock('@/services/api', () => ({
  coreApiClient: {
    listApprovals: vi.fn(),
    getApproval: vi.fn(),
    decideApproval: vi.fn(),
  },
}));

const approval: Approval = {
  id: '11111111-1111-4111-8111-111111111111',
  run_id: '22222222-2222-4222-8222-222222222222',
  tool_name: 'production.write',
  parameter_digest: `sha256:${'a'.repeat(64)}`,
  status: 'PENDING',
  expires_at: '2026-08-10T10:30:00Z',
  resource_version: 3,
};

describe('approvalService', () => {
  beforeEach(() => vi.clearAllMocks());

  it('uses generated list and detail clients', async () => {
    vi.mocked(coreApiClient.listApprovals).mockResolvedValue({
      items: [approval],
      has_more: false,
    });
    vi.mocked(coreApiClient.getApproval).mockResolvedValue(approval);

    await approvalService.list({ status: 'PENDING', runId: approval.run_id });
    await approvalService.get(approval.id);

    expect(coreApiClient.listApprovals).toHaveBeenCalledWith({
      status: 'PENDING',
      runId: approval.run_id,
    });
    expect(coreApiClient.getApproval).toHaveBeenCalledWith({ approvalId: approval.id });
  });

  it('derives the strong If-Match header from resource_version', async () => {
    vi.mocked(coreApiClient.decideApproval).mockResolvedValue({
      ...approval,
      status: 'APPROVED',
      resource_version: 4,
    });

    await approvalService.decide(
      approval,
      { decision: 'APPROVED', comment: 'reviewed' },
      'approval-key-001',
    );

    expect(coreApiClient.decideApproval).toHaveBeenCalledWith({
      approvalId: approval.id,
      idempotencyKey: 'approval-key-001',
      ifMatch: '"rv:3"',
      body: { decision: 'APPROVED', comment: 'reviewed' },
    });
  });
});
