import type { Operation, OperationAccepted } from '@/api/generated/core-models';
import { coreApiClient } from '@/services/api';

export function operationIdFromStatusUrl(statusUrl: string): string {
  const match = /^\/api\/v1\/operations\/([^/?#]+)$/.exec(statusUrl);
  if (match?.[1] === undefined) throw new Error('Operation status_url 不符合冻结契约');
  return decodeURIComponent(match[1]);
}

export const operationService = {
  getByAccepted(operation: OperationAccepted, signal?: AbortSignal): Promise<Operation> {
    return coreApiClient.getOperation({
      operationId: operationIdFromStatusUrl(operation.status_url),
      ...(signal === undefined ? {} : { signal }),
    });
  },
};
