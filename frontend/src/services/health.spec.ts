import { afterEach, describe, expect, it, vi } from 'vitest';

import { fetchBackendReadiness } from '@/services/health';

describe('fetchBackendReadiness', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('maps the backend readiness response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ status: 'ok', service_name: 'api' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );

    await expect(fetchBackendReadiness()).resolves.toEqual({
      status: 'ok',
      serviceName: 'api',
    });
  });

  it('rejects an unexpected response shape', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ status: 'ok' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );

    await expect(fetchBackendReadiness()).rejects.toThrow('健康响应格式不符合预期');
  });
});
