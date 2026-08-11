import type { CurrentIdentity } from '@/api/generated/core-models';
import { coreApiClient } from '@/services/api';

export const identityService = {
  getCurrent(signal?: AbortSignal): Promise<CurrentIdentity> {
    return coreApiClient.getCurrentIdentity(signal === undefined ? {} : { signal });
  },
};
