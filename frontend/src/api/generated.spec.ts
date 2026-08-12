import { describe, expect, it, vi } from 'vitest';

import { CoreApiClient, operationIds as coreOperationIds } from '@/api/generated/core-client';
import { operationIds as resourceOperationIds } from '@/api/generated/resources-client';
import { runEventTypes, type RunEvent } from '@/api/generated/run-event';
import type { ApiTransport } from '@/api/generated/transport';

describe('generated contracts', () => {
  it('covers both frozen OpenAPI operation catalogs', () => {
    expect(coreOperationIds).toHaveLength(42);
    expect(coreOperationIds).toContain('downloadArtifactContent');
    expect(resourceOperationIds).toHaveLength(134);
    expect(resourceOperationIds).toContain('createQuotaPolicy');
    expect(resourceOperationIds).toContain('listQuotaPolicyVersions');
    expect(new Set([...coreOperationIds, ...resourceOperationIds]).size).toBe(176);
  });

  it('keeps RunEvent as a discriminated union', () => {
    const event: RunEvent = {
      schema_version: '1.0',
      event_id: 'event_42',
      source_event_id: 'source_42',
      tenant_id: 'tenant_01',
      run_id: 'run_01',
      session_id: 'session_01',
      sequence_no: 42,
      event_type: 'text_delta',
      occurred_at: '2026-08-05T08:00:00Z',
      recorded_at: '2026-08-05T08:00:00Z',
      trace_id: 'trace_01',
      execution_attempt: 1,
      payload_version: '1.0',
      payload: { message_id: 'message_01', delta: 'hello' },
    };

    expect(runEventTypes).toHaveLength(20);
    expect(event.payload.delta).toBe('hello');
  });

  it('routes generated methods through the shared transport', async () => {
    const identity = {
      user_id: 'user_01',
      external_subject: 'mock-user-01',
      display_name: 'Mock User',
      active_tenant_id: null,
      memberships: [],
      auth_time: '2026-08-05T08:00:00Z',
    };
    const request = vi.fn().mockResolvedValue(identity);
    const transport: ApiTransport = {
      request,
      openStream: vi.fn(),
    };
    const client = new CoreApiClient(transport);

    await expect(client.getCurrentIdentity()).resolves.toEqual(identity);
    expect(request).toHaveBeenCalledWith({
      method: 'GET',
      path: '/api/v1/me',
      signal: undefined,
    });
  });
});
