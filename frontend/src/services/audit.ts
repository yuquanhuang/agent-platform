import type { AuditPage } from '@/api/generated/resources-models';
import { resourcesApiClient } from '@/services/api';

export interface AuditListInput {
  limit?: number;
  cursor?: string;
  action?: string;
  resourceType?: string;
  actorId?: string;
  runId?: string;
  occurredFrom?: string;
  occurredTo?: string;
  signal?: AbortSignal;
}

export const auditService = {
  list(input: AuditListInput = {}): Promise<AuditPage> {
    return resourcesApiClient.listAuditLogs(input);
  },
};
