import { describe, expect, it } from 'vitest';

import { readRunEventStream, RunEventStreamProtocolError } from '@/services/run-event-stream';

function eventJson(sequenceNo = 1): string {
  return JSON.stringify({
    schema_version: '1.0',
    event_id: `event-${sequenceNo}`,
    source_event_id: `source-${sequenceNo}`,
    tenant_id: 'tenant-001',
    run_id: 'run-001',
    session_id: 'session-001',
    sequence_no: sequenceNo,
    event_type: 'text_delta',
    occurred_at: '2026-08-09T08:00:00Z',
    recorded_at: '2026-08-09T08:00:01Z',
    trace_id: 'trace-001',
    execution_attempt: 1,
    payload_version: '1.0',
    payload: { message_id: 'message-001', delta: 'hello' },
  });
}

function responseFromChunks(chunks: ReadonlyArray<string>): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
    { headers: { 'Content-Type': 'text/event-stream' } },
  );
}

async function collect(response: Response): Promise<ReadonlyArray<number>> {
  const sequenceNumbers: number[] = [];
  for await (const event of readRunEventStream(response)) {
    sequenceNumbers.push(event.sequence_no);
  }
  return sequenceNumbers;
}

describe('readRunEventStream', () => {
  it('parses CRLF frames split across chunks and ignores heartbeat comments', async () => {
    const json = eventJson();
    const response = responseFromChunks([
      ': heartbeat\r\n\r\nev',
      `ent: run_event\r\nid: 1\r\ndata: ${json.slice(0, 30)}`,
      `${json.slice(30)}\r\n\r\n`,
    ]);

    await expect(collect(response)).resolves.toEqual([1]);
  });

  it('joins multiline data fields before parsing JSON', async () => {
    const json = eventJson();
    const midpoint = json.indexOf(',');
    const response = responseFromChunks([
      `event: run_event\nid: 1\ndata: ${json.slice(0, midpoint + 1)}\ndata: ${json.slice(midpoint + 1)}\n\n`,
    ]);

    await expect(collect(response)).resolves.toEqual([1]);
  });

  it('fails closed on an unexpected event name or mismatched id', async () => {
    await expect(
      collect(responseFromChunks([`event: message\nid: 1\ndata: ${eventJson()}\n\n`])),
    ).rejects.toMatchObject({ code: 'INVALID_EVENT_NAME' });
    await expect(
      collect(responseFromChunks([`event: run_event\nid: 2\ndata: ${eventJson()}\n\n`])),
    ).rejects.toMatchObject({ code: 'SEQUENCE_MISMATCH' });
  });

  it('rejects malformed JSON and oversized frames without echoing payload content', async () => {
    const secret = 'sensitive-payload-value';
    await expect(
      collect(responseFromChunks([`event: run_event\nid: 1\ndata: {${secret}\n\n`])),
    ).rejects.not.toThrow(secret);

    await expect(
      (async () => {
        const iterator = readRunEventStream(
          responseFromChunks([`event: run_event\nid: 1\ndata: ${eventJson()}\n\n`]),
          { maxFrameBytes: 32 },
        );
        await iterator.next();
      })(),
    ).rejects.toBeInstanceOf(RunEventStreamProtocolError);
  });

  it('cancels a pending reader when the subscription is aborted', async () => {
    let cancelled = false;
    const response = new Response(
      new ReadableStream<Uint8Array>({
        pull() {
          return new Promise<void>(() => undefined);
        },
        cancel() {
          cancelled = true;
        },
      }),
    );
    const controller = new AbortController();
    const consume = (async () => {
      const iterator = readRunEventStream(response, { signal: controller.signal });
      await iterator.next();
    })();

    controller.abort();

    await expect(consume).rejects.toMatchObject({ name: 'AbortError' });
    expect(cancelled).toBe(true);
  });
});
