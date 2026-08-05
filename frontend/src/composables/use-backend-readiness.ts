import { useQuery } from '@tanstack/vue-query';

import { fetchBackendReadiness } from '@/services/health';

export const backendReadinessQueryKey = ['platform', 'backend-readiness'] as const;

export function useBackendReadiness() {
  return useQuery({
    queryKey: backendReadinessQueryKey,
    queryFn: ({ signal }) => fetchBackendReadiness(signal),
    refetchInterval: 60_000,
  });
}
