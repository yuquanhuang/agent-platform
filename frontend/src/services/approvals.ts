import type {
  Approval,
  ApprovalDecisionRequest,
  ApprovalPage,
  ListApprovalsStatus,
} from '@/api/generated/core-models';
import { coreApiClient } from '@/services/api';

export const approvalService = {
  list(
    input: {
      limit?: number;
      cursor?: string;
      status?: ListApprovalsStatus;
      runId?: string;
      signal?: AbortSignal;
    } = {},
  ): Promise<ApprovalPage> {
    return coreApiClient.listApprovals(input);
  },

  get(approvalId: string, signal?: AbortSignal): Promise<Approval> {
    return coreApiClient.getApproval({
      approvalId,
      ...(signal === undefined ? {} : { signal }),
    });
  },

  decide(
    approval: Approval,
    body: ApprovalDecisionRequest,
    idempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<Approval> {
    return coreApiClient.decideApproval({
      approvalId: approval.id,
      idempotencyKey,
      ifMatch: `"rv:${approval.resource_version}"`,
      body,
      ...(signal === undefined ? {} : { signal }),
    });
  },
};
