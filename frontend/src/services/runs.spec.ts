import { beforeEach, describe, expect, it, vi } from 'vitest';

import { coreApiClient } from '@/services/api';
import { runService } from '@/services/runs';

vi.mock('@/services/api', () => ({
  coreApiClient: {
    getRun: vi.fn(),
    listRunEvents: vi.fn(),
    streamRunEvents: vi.fn(),
  },
}));

describe('runService', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('uses the frozen history cursor and page limit', async () => {
    vi.mocked(coreApiClient.listRunEvents).mockResolvedValue({
      items: [],
      has_more: false,
      latest_sequence_no: 42,
    });

    await runService.listEvents('run-1', { after: 21, limit: 100 });

    expect(coreApiClient.listRunEvents).toHaveBeenCalledWith({
      runId: 'run-1',
      after: 21,
      limit: 100,
    });
  });

  it('sends the last applied sequence through Last-Event-ID', async () => {
    vi.mocked(coreApiClient.streamRunEvents).mockResolvedValue(new Response());

    await runService.openEventStream('run-1', {
      after: 20,
      lastSequenceNo: 21,
    });

    expect(coreApiClient.streamRunEvents).toHaveBeenCalledWith({
      runId: 'run-1',
      after: 20,
      lastEventID: '21',
    });
  });

  it('rejects an invalid Last-Event-ID before opening a stream', () => {
    expect(() => runService.openEventStream('run-1', { lastSequenceNo: Number.NaN })).toThrow(
      RangeError,
    );

    expect(coreApiClient.streamRunEvents).not.toHaveBeenCalled();
  });
});
