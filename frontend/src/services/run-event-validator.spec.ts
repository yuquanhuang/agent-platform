import { describe, expect, it } from 'vitest';

import { parseRunEvent, RunEventValidationError } from '@/services/run-event-validator';

function validEvent(): Record<string, unknown> {
  return {
    schema_version: '1.0',
    event_id: 'event-001',
    source_event_id: 'source-001',
    tenant_id: 'tenant-001',
    run_id: 'run-001',
    session_id: 'session-001',
    sequence_no: 1,
    event_type: 'text_delta',
    occurred_at: '2026-08-09T08:00:00Z',
    recorded_at: '2026-08-09T08:00:01Z',
    trace_id: 'trace-001',
    execution_attempt: 1,
    payload_version: '1.0',
    payload: { message_id: 'message-001', delta: 'hello' },
  };
}

describe('parseRunEvent', () => {
  it('returns an event validated by the frozen generated schema', () => {
    const event = validEvent();

    expect(parseRunEvent(event)).toBe(event);
  });

  it('rejects envelope and payload fields outside the frozen contract', () => {
    expect(() => parseRunEvent({ ...validEvent(), unexpected: true })).toThrow(
      RunEventValidationError,
    );
    expect(() =>
      parseRunEvent({
        ...validEvent(),
        payload: { message_id: 'message-001', delta: 'hello', secret: 'not-allowed' },
      }),
    ).toThrow(RunEventValidationError);
  });

  it('rejects invalid sequence numbers, dates and variant payloads without echoing values', () => {
    const invalid = {
      ...validEvent(),
      sequence_no: 0,
      occurred_at: 'private-invalid-date-value',
      payload: { message_id: 'message-001', delta: '' },
    };

    expect(() => parseRunEvent(invalid)).toThrow(RunEventValidationError);
    try {
      parseRunEvent(invalid);
    } catch (error) {
      expect(String(error)).not.toContain('private-invalid-date-value');
    }
  });
});
