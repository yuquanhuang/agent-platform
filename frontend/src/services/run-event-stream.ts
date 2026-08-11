import type { RunEvent } from '@/api/generated/run-event';
import { parseRunEvent } from '@/services/run-event-validator';

export const DEFAULT_MAX_SSE_FRAME_BYTES = 320 * 1024;

export class RunEventStreamProtocolError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = 'RunEventStreamProtocolError';
    this.code = code;
  }
}

export interface RunEventStreamOptions {
  readonly signal?: AbortSignal;
  readonly maxFrameBytes?: number;
}

interface SseFrame {
  readonly event: string | null;
  readonly id: string | null;
  readonly data: string | null;
}

export async function* readRunEventStream(
  response: Response,
  options: RunEventStreamOptions = {},
): AsyncGenerator<RunEvent> {
  if (response.body === null) {
    throw new RunEventStreamProtocolError('MISSING_BODY', 'RunEvent stream has no response body.');
  }

  const maxFrameBytes = options.maxFrameBytes ?? DEFAULT_MAX_SSE_FRAME_BYTES;
  if (!Number.isSafeInteger(maxFrameBytes) || maxFrameBytes <= 0) {
    throw new RangeError('maxFrameBytes must be a positive safe integer.');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const encoder = new TextEncoder();
  let buffer = '';
  let frameLines: string[] = [];
  let frameBytes = 0;
  let completed = false;

  const abortReader = (): void => {
    void reader.cancel().catch(() => undefined);
  };
  options.signal?.addEventListener('abort', abortReader, { once: true });

  const consumeLine = (line: string): RunEvent | null => {
    if (line !== '') {
      frameBytes += encoder.encode(line).byteLength + 1;
      if (frameBytes > maxFrameBytes) throw oversizedFrameError();
      frameLines.push(line);
      return null;
    }

    const frame = parseSseFrame(frameLines);
    frameLines = [];
    frameBytes = 0;
    return frame === null ? null : validateFrame(frame);
  };

  try {
    while (true) {
      throwIfAborted(options.signal);
      const result = await reader.read();
      throwIfAborted(options.signal);
      if (result.done) {
        completed = true;
        break;
      }
      buffer += decoder.decode(result.value, { stream: true });
      if (encoder.encode(buffer).byteLength > maxFrameBytes + 8192) {
        throw oversizedFrameError();
      }

      let newlineIndex = buffer.indexOf('\n');
      while (newlineIndex >= 0) {
        const rawLine = buffer.slice(0, newlineIndex);
        buffer = buffer.slice(newlineIndex + 1);
        const event = consumeLine(rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine);
        if (event !== null) yield event;
        newlineIndex = buffer.indexOf('\n');
      }
    }

    buffer += decoder.decode();
    if (buffer !== '') {
      const event = consumeLine(buffer.endsWith('\r') ? buffer.slice(0, -1) : buffer);
      if (event !== null) yield event;
    }
    if (frameLines.length > 0) {
      const frame = parseSseFrame(frameLines);
      if (frame !== null) yield validateFrame(frame);
    }
  } finally {
    options.signal?.removeEventListener('abort', abortReader);
    if (!completed) await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

function parseSseFrame(lines: ReadonlyArray<string>): SseFrame | null {
  let event: string | null = null;
  let id: string | null = null;
  const data: string[] = [];

  for (const line of lines) {
    if (line.startsWith(':')) continue;
    const separator = line.indexOf(':');
    const field = separator < 0 ? line : line.slice(0, separator);
    const rawValue = separator < 0 ? '' : line.slice(separator + 1);
    const value = rawValue.startsWith(' ') ? rawValue.slice(1) : rawValue;
    if (field === 'event') event = value;
    else if (field === 'id') id = value;
    else if (field === 'data') data.push(value);
  }

  if (data.length === 0) return null;
  return { event, id, data: data.join('\n') };
}

function validateFrame(frame: SseFrame): RunEvent {
  if (frame.event !== 'run_event') {
    throw new RunEventStreamProtocolError(
      'INVALID_EVENT_NAME',
      'SSE event name must be run_event.',
    );
  }
  if (frame.id === null || !/^[1-9]\d*$/.test(frame.id)) {
    throw new RunEventStreamProtocolError(
      'INVALID_EVENT_ID',
      'SSE event id must be a positive integer.',
    );
  }
  const sequenceNo = Number(frame.id);
  if (!Number.isSafeInteger(sequenceNo)) {
    throw new RunEventStreamProtocolError(
      'INVALID_EVENT_ID',
      'SSE event id exceeds the supported integer range.',
    );
  }

  let payload: unknown;
  try {
    payload = JSON.parse(frame.data ?? '') as unknown;
  } catch {
    throw new RunEventStreamProtocolError('INVALID_JSON', 'SSE data is not valid JSON.');
  }
  const event = parseRunEvent(payload);
  if (event.sequence_no !== sequenceNo) {
    throw new RunEventStreamProtocolError(
      'SEQUENCE_MISMATCH',
      'SSE event id does not match RunEvent sequence_no.',
    );
  }
  return event;
}

function oversizedFrameError(): RunEventStreamProtocolError {
  return new RunEventStreamProtocolError(
    'FRAME_TOO_LARGE',
    'SSE frame exceeds the configured safety limit.',
  );
}

function throwIfAborted(signal: AbortSignal | undefined): void {
  if (!signal?.aborted) return;
  throw new DOMException('RunEvent stream aborted.', 'AbortError');
}
