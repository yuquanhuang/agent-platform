import type {
  OperationAccepted,
  Session,
  MessagePage,
  RunPage,
  SessionCreateRequest,
  SessionPage,
  SessionUpdateRequest,
  ListSessionsStatus,
} from '@/api/generated/core-models';
import { coreApiClient } from '@/services/api';
import { createIdempotencyKey, formatResourceEtag } from '@/services/prompts';

export const sessionService = {
  list(
    input: {
      limit?: number;
      cursor?: string;
      agentId?: string;
      status?: ListSessionsStatus;
    } = {},
  ): Promise<SessionPage> {
    return coreApiClient.listSessions(input);
  },

  get(sessionId: string): Promise<Session> {
    return coreApiClient.getSession({ sessionId });
  },

  listMessages(
    sessionId: string,
    input: { limit?: number; cursor?: string; branchId?: string } = {},
  ): Promise<MessagePage> {
    return coreApiClient.listSessionMessages({ sessionId, ...input });
  },

  listRuns(sessionId: string, input: { limit?: number; cursor?: string } = {}): Promise<RunPage> {
    return coreApiClient.listSessionRuns({ sessionId, ...input });
  },

  create(body: SessionCreateRequest): Promise<Session> {
    return coreApiClient.createSession({ idempotencyKey: createIdempotencyKey(), body });
  },

  update(
    session: Pick<Session, 'id' | 'resource_version'>,
    body: SessionUpdateRequest,
  ): Promise<Session> {
    return coreApiClient.updateSession({
      sessionId: session.id,
      ifMatch: formatResourceEtag(session.resource_version),
      body,
    });
  },

  archive(session: Pick<Session, 'id' | 'resource_version'>): Promise<Session> {
    return coreApiClient.archiveSession({
      sessionId: session.id,
      idempotencyKey: createIdempotencyKey(),
      ifMatch: formatResourceEtag(session.resource_version),
    });
  },

  delete(session: Pick<Session, 'id' | 'resource_version'>): Promise<OperationAccepted> {
    return coreApiClient.deleteSession({
      sessionId: session.id,
      idempotencyKey: createIdempotencyKey(),
      ifMatch: formatResourceEtag(session.resource_version),
    });
  },
};
