import type { RunEventPage } from '@/api/generated/core-models';
import type { RunEvent } from '@/api/generated/run-event';
import { ApiError } from '@/api/generated/transport';
import {
  readRunEventStream,
  RunEventStreamProtocolError,
  type RunEventStreamOptions,
} from '@/services/run-event-stream';
import { parseRunEvent, RunEventValidationError } from '@/services/run-event-validator';
import { runService } from '@/services/runs';
import {
  canCloseRunProjection,
  createRunProjection,
  RunProjectionIdentityError,
  RunProjectionSequenceError,
  type RunProjection,
  type RunProjectionKey,
  type RunProjectionOptions,
} from '@/stores/run-projection';

export type RunEventConnectionStatus =
  'idle' | 'catching_up' | 'connecting' | 'live' | 'retrying' | 'closed' | 'failed' | 'stopped';

export interface RunEventCoordinatorState {
  readonly status: RunEventConnectionStatus;
  readonly projection: RunProjection;
  readonly retryAttempt: number;
  readonly nextRetryDelayMs: number | null;
  readonly errorCode: string | null;
  readonly errorMessage: string | null;
}

export interface RunProjectionSink {
  projectionFor(key: RunProjectionKey): RunProjection | undefined;
  applyPage(
    key: RunProjectionKey,
    page: RunEventPage,
    options?: RunProjectionOptions,
  ): RunProjection;
  applyEvent(event: RunEvent, options?: RunProjectionOptions): RunProjection;
}

export interface RunEventCoordinatorDependencies {
  readonly listEvents?: typeof runService.listEvents;
  readonly openEventStream?: typeof runService.openEventStream;
  readonly readStream?: (
    response: Response,
    options?: RunEventStreamOptions,
  ) => AsyncIterable<RunEvent>;
  readonly delay?: (milliseconds: number, signal: AbortSignal) => Promise<void>;
}

export interface RunEventCoordinatorOptions {
  readonly pageLimit?: number;
  readonly initialRetryDelayMs?: number;
  readonly maxRetryDelayMs?: number;
  readonly projectionOptions?: RunProjectionOptions;
}

export class RunEventCoordinatorError extends Error {
  readonly code: string;
  readonly retryable: boolean;

  constructor(code: string, message: string, retryable: boolean) {
    super(message);
    this.name = 'RunEventCoordinatorError';
    this.code = code;
    this.retryable = retryable;
  }
}

export class RunEventCoordinator {
  private readonly abortController = new AbortController();
  private readonly listeners = new Set<(state: RunEventCoordinatorState) => void>();
  private readonly listEvents: typeof runService.listEvents;
  private readonly openEventStream: typeof runService.openEventStream;
  private readonly readStream: NonNullable<RunEventCoordinatorDependencies['readStream']>;
  private readonly delay: NonNullable<RunEventCoordinatorDependencies['delay']>;
  private readonly pageLimit: number;
  private readonly initialRetryDelayMs: number;
  private readonly maxRetryDelayMs: number;
  private readonly projectionOptions: RunProjectionOptions;
  private started = false;
  private task: Promise<void> | null = null;
  private currentState: RunEventCoordinatorState;

  constructor(
    private readonly key: RunProjectionKey,
    private readonly sink: RunProjectionSink,
    dependencies: RunEventCoordinatorDependencies = {},
    options: RunEventCoordinatorOptions = {},
  ) {
    this.listEvents = dependencies.listEvents ?? runService.listEvents;
    this.openEventStream = dependencies.openEventStream ?? runService.openEventStream;
    this.readStream = dependencies.readStream ?? readRunEventStream;
    this.delay = dependencies.delay ?? abortableDelay;
    this.pageLimit = options.pageLimit ?? 200;
    this.initialRetryDelayMs = options.initialRetryDelayMs ?? 500;
    this.maxRetryDelayMs = options.maxRetryDelayMs ?? 8000;
    this.projectionOptions = options.projectionOptions ?? {};
    assertPositiveInteger(this.pageLimit, 'pageLimit');
    assertPositiveInteger(this.initialRetryDelayMs, 'initialRetryDelayMs');
    assertPositiveInteger(this.maxRetryDelayMs, 'maxRetryDelayMs');
    if (this.maxRetryDelayMs < this.initialRetryDelayMs) {
      throw new RangeError('maxRetryDelayMs must not be less than initialRetryDelayMs.');
    }
    this.currentState = {
      status: 'idle',
      projection: sink.projectionFor(key) ?? createRunProjection(key),
      retryAttempt: 0,
      nextRetryDelayMs: null,
      errorCode: null,
      errorMessage: null,
    };
  }

  get state(): RunEventCoordinatorState {
    return this.currentState;
  }

  subscribe(listener: (state: RunEventCoordinatorState) => void): () => void {
    this.listeners.add(listener);
    listener(this.currentState);
    return () => this.listeners.delete(listener);
  }

  start(): Promise<void> {
    if (!this.started) {
      this.started = true;
      this.task = this.run();
    }
    return this.task ?? Promise.resolve();
  }

  stop(): void {
    if (this.abortController.signal.aborted) return;
    this.abortController.abort();
    this.update({ status: 'stopped', nextRetryDelayMs: null });
  }

  private async run(): Promise<void> {
    let retryAttempt = 0;
    while (!this.abortController.signal.aborted) {
      try {
        const projection = await this.catchUpHistory();
        if (canCloseRunProjection(projection)) {
          this.update({
            status: 'closed',
            retryAttempt: 0,
            nextRetryDelayMs: null,
            errorCode: null,
            errorMessage: null,
          });
          return;
        }

        await this.consumeLiveStream(projection.lastSequenceNo);
      } catch (error) {
        if (this.abortController.signal.aborted || isAbortError(error)) return;
        const coordinatorError = classifyError(error);
        if (!coordinatorError.retryable) {
          this.update({
            status: 'failed',
            nextRetryDelayMs: null,
            errorCode: coordinatorError.code,
            errorMessage: coordinatorError.message,
          });
          return;
        }

        retryAttempt = this.currentState.retryAttempt + 1;
        const retryDelay = Math.min(
          this.initialRetryDelayMs * 2 ** (retryAttempt - 1),
          this.maxRetryDelayMs,
        );
        this.update({
          status: 'retrying',
          retryAttempt,
          nextRetryDelayMs: retryDelay,
          errorCode: coordinatorError.code,
          errorMessage: coordinatorError.message,
        });
        try {
          await this.delay(retryDelay, this.abortController.signal);
        } catch (delayError) {
          if (this.abortController.signal.aborted || isAbortError(delayError)) return;
          this.update({
            status: 'failed',
            nextRetryDelayMs: null,
            errorCode: 'RETRY_DELAY_FAILED',
            errorMessage: 'RunEvent reconnect scheduling failed.',
          });
          return;
        }
      }
    }
  }

  private async catchUpHistory(): Promise<RunProjection> {
    this.update({
      status: 'catching_up',
      nextRetryDelayMs: null,
      errorCode: null,
      errorMessage: null,
    });
    while (true) {
      const before = this.currentState.projection.lastSequenceNo;
      const rawPage = await this.listEvents(this.key.runId, {
        after: before,
        limit: this.pageLimit,
        signal: this.abortController.signal,
      });
      const page = validateEventPage(rawPage);
      const projection = this.sink.applyPage(this.key, page, this.projectionOptions);
      this.update({ projection });
      assertProjectionHealthy(projection);

      const madeProgress = projection.lastSequenceNo > before;
      const needsMore =
        page.has_more ||
        projection.gap !== null ||
        projection.lastSequenceNo < page.latest_sequence_no;
      if (!needsMore) return projection;
      if (!madeProgress) {
        throw new RunEventCoordinatorError(
          'HISTORY_GAP',
          'Persisted RunEvent history contains an unresolved sequence gap.',
          false,
        );
      }
    }
  }

  private async consumeLiveStream(lastSequenceNo: number): Promise<void> {
    this.update({
      status: 'connecting',
      nextRetryDelayMs: null,
      errorCode: null,
      errorMessage: null,
    });
    const response = await this.openEventStream(this.key.runId, {
      lastSequenceNo,
      signal: this.abortController.signal,
    });
    this.update({ status: 'live' });

    for await (const rawEvent of this.readStream(response, {
      signal: this.abortController.signal,
    })) {
      const event = parseRunEvent(rawEvent);
      const projection = this.sink.applyEvent(event, this.projectionOptions);
      this.update({
        projection,
        retryAttempt: 0,
        errorCode: null,
        errorMessage: null,
      });
      assertProjectionHealthy(projection);
      if (projection.gap !== null || projection.terminal !== null) return;
    }

    throw new RunEventCoordinatorError(
      'STREAM_DISCONNECTED',
      'RunEvent stream disconnected before the Run reached a confirmed terminal state.',
      true,
    );
  }

  private update(patch: Partial<RunEventCoordinatorState>): void {
    this.currentState = { ...this.currentState, ...patch };
    for (const listener of this.listeners) listener(this.currentState);
  }
}

function validateEventPage(page: RunEventPage): RunEventPage {
  if (!Number.isSafeInteger(page.latest_sequence_no) || page.latest_sequence_no < 0) {
    throw new RunEventCoordinatorError(
      'INVALID_HISTORY_PAGE',
      'RunEvent history returned an invalid high-water mark.',
      false,
    );
  }
  if (typeof page.has_more !== 'boolean' || !Array.isArray(page.items)) {
    throw new RunEventCoordinatorError(
      'INVALID_HISTORY_PAGE',
      'RunEvent history returned an invalid page envelope.',
      false,
    );
  }
  return { ...page, items: page.items.map((event) => parseRunEvent(event)) };
}

function assertProjectionHealthy(projection: RunProjection): void {
  if (projection.terminalConflict !== null) {
    throw new RunEventCoordinatorError(
      'TERMINAL_CONFLICT',
      'RunEvent history contains facts after the first terminal event.',
      false,
    );
  }
}

function classifyError(error: unknown): RunEventCoordinatorError {
  if (error instanceof RunEventCoordinatorError) return error;
  if (error instanceof RunEventStreamProtocolError) {
    return new RunEventCoordinatorError(error.code, error.message, false);
  }
  if (error instanceof RunEventValidationError) {
    return new RunEventCoordinatorError(error.code, error.message, false);
  }
  if (error instanceof RunProjectionIdentityError || error instanceof RunProjectionSequenceError) {
    return new RunEventCoordinatorError('INVALID_PROJECTION', error.message, false);
  }
  if (error instanceof ApiError) {
    const retryable =
      error.statusCode === 408 || error.statusCode === 429 || error.statusCode >= 500;
    return new RunEventCoordinatorError(
      `HTTP_${error.statusCode}`,
      retryable
        ? 'RunEvent service is temporarily unavailable.'
        : 'RunEvent service rejected the subscription request.',
      retryable,
    );
  }
  return new RunEventCoordinatorError(
    'STREAM_UNAVAILABLE',
    'RunEvent connection was interrupted.',
    true,
  );
}

function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException('RunEvent reconnect aborted.', 'AbortError'));
      return;
    }
    const onAbort = (): void => {
      clearTimeout(timeout);
      reject(new DOMException('RunEvent reconnect aborted.', 'AbortError'));
    };
    const timeout = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, milliseconds);
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

function assertPositiveInteger(value: number, name: string): void {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new RangeError(`${name} must be a positive safe integer.`);
  }
}
