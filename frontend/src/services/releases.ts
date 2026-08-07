import type {
  AgentVersion,
  AgentVersionPage,
  Deployment,
  PublishAgentPreview,
  PublishAgentPreviewRequest,
  PublishAgentRequest,
  Release,
  ReleaseAccepted,
  RollbackAgentRequest,
  SnapshotDiff,
} from '@/api/generated/core-models';
import { coreApiClient } from '@/services/api';
import { createIdempotencyKey } from '@/services/prompts';

export const TERMINAL_RELEASE_STATUSES = new Set<Release['status']>([
  'SUCCEEDED',
  'FAILED',
  'CANCELLED',
]);

export function normalizeRuntimeTargets(value: string): string[] {
  return [
    ...new Set(
      value
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ].sort();
}

export const releaseService = {
  preview(
    agentId: string,
    body: PublishAgentPreviewRequest,
    signal?: AbortSignal,
  ): Promise<PublishAgentPreview> {
    return coreApiClient.previewAgentPublish({
      agentId,
      body,
      ...(signal === undefined ? {} : { signal }),
    });
  },

  publish(agentId: string, body: PublishAgentRequest): Promise<ReleaseAccepted> {
    return coreApiClient.publishAgent({
      agentId,
      idempotencyKey: createIdempotencyKey(),
      body,
    });
  },

  rollback(agentId: string, body: RollbackAgentRequest): Promise<ReleaseAccepted> {
    return coreApiClient.rollbackAgent({
      agentId,
      idempotencyKey: createIdempotencyKey(),
      body,
    });
  },

  get(releaseId: string, signal?: AbortSignal): Promise<Release> {
    return coreApiClient.getRelease({
      releaseId,
      ...(signal === undefined ? {} : { signal }),
    });
  },

  getDeployment(deploymentId: string, signal?: AbortSignal): Promise<Deployment> {
    return coreApiClient.getDeployment({
      deploymentId,
      ...(signal === undefined ? {} : { signal }),
    });
  },

  listVersions(
    agentId: string,
    input: { limit?: number; cursor?: string } = {},
    signal?: AbortSignal,
  ): Promise<AgentVersionPage> {
    return coreApiClient.listAgentVersions({
      agentId,
      ...input,
      ...(signal === undefined ? {} : { signal }),
    });
  },

  getVersion(agentId: string, versionId: string, signal?: AbortSignal): Promise<AgentVersion> {
    return coreApiClient.getAgentVersion({
      agentId,
      versionId,
      ...(signal === undefined ? {} : { signal }),
    });
  },

  diff(
    agentId: string,
    fromSnapshotId: string,
    toSnapshotId: string,
    signal?: AbortSignal,
  ): Promise<SnapshotDiff> {
    return coreApiClient.diffAgentSnapshots({
      agentId,
      fromSnapshotId,
      toSnapshotId,
      ...(signal === undefined ? {} : { signal }),
    });
  },
};
