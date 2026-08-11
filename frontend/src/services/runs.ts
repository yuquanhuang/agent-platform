import type { Run, RunEventPage } from '@/api/generated/core-models';
import { coreApiClient } from '@/services/api';

export const runService = {
  get(runId: string, signal?: AbortSignal): Promise<Run> {
    return coreApiClient.getRun({
      runId,
      ...(signal === undefined ? {} : { signal }),
    });
  },

  listEvents(
    runId: string,
    input: { after?: number; limit?: number; signal?: AbortSignal } = {},
  ): Promise<RunEventPage> {
    return coreApiClient.listRunEvents({ runId, ...input });
  },

  openEventStream(
    runId: string,
    input: {
      after?: number;
      lastSequenceNo?: number;
      signal?: AbortSignal;
    } = {},
  ): Promise<Response> {
    return coreApiClient.streamRunEvents({
      runId,
      ...(input.after === undefined ? {} : { after: input.after }),
      ...(input.lastSequenceNo === undefined
        ? {}
        : { lastEventID: formatLastEventId(input.lastSequenceNo) }),
      ...(input.signal === undefined ? {} : { signal: input.signal }),
    });
  },
};

function formatLastEventId(sequenceNo: number): string {
  if (!Number.isSafeInteger(sequenceNo) || sequenceNo < 0) {
    throw new RangeError('Last-Event-ID sequence must be a non-negative safe integer.');
  }
  return String(sequenceNo);
}
