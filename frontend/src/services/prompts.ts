import type {
  ActionRequest,
  PromptCreateRequest,
  Resource,
  ResourceCopyRequest,
  ResourceDiff,
  ResourcePage,
  ResourceReferencePage,
  ResourceUpdateRequest,
  ResourceVersion,
  ResourceVersionPage,
} from '@/api/generated/resources-models';
import { resourcesApiClient } from '@/services/api';

export function formatResourceEtag(resourceVersion: number): string {
  return `"rv:${resourceVersion}"`;
}

export function createIdempotencyKey(): string {
  return crypto.randomUUID();
}

export const promptService = {
  list(input: { limit?: number; cursor?: string; keyword?: string } = {}): Promise<ResourcePage> {
    return resourcesApiClient.listPrompts(input);
  },

  get(resourceId: string): Promise<Resource> {
    return resourcesApiClient.getPrompt({ resourceId });
  },

  create(body: PromptCreateRequest): Promise<Resource> {
    return resourcesApiClient.createPrompt({
      idempotencyKey: createIdempotencyKey(),
      body,
    });
  },

  update(resource: Resource, body: ResourceUpdateRequest): Promise<Resource> {
    return resourcesApiClient.updatePrompt({
      resourceId: resource.id,
      ifMatch: formatResourceEtag(resource.resource_version),
      body,
    });
  },

  delete(resource: Resource) {
    return resourcesApiClient.deletePrompt({
      resourceId: resource.id,
      idempotencyKey: createIdempotencyKey(),
      ifMatch: formatResourceEtag(resource.resource_version),
    });
  },

  publish(resource: Resource, releaseNote: string): Promise<ResourceVersion> {
    return resourcesApiClient.publishPrompt({
      resourceId: resource.id,
      idempotencyKey: createIdempotencyKey(),
      body: {
        expected_resource_version: resource.resource_version,
        release_note: releaseNote,
      },
    });
  },

  copy(resourceId: string, body: ResourceCopyRequest): Promise<Resource> {
    return resourcesApiClient.copyPrompt({
      resourceId,
      idempotencyKey: createIdempotencyKey(),
      body,
    });
  },

  setEnabled(resource: Resource, enabled: boolean, body?: ActionRequest): Promise<Resource> {
    const input = {
      resourceId: resource.id,
      idempotencyKey: createIdempotencyKey(),
      ifMatch: formatResourceEtag(resource.resource_version),
      ...(body === undefined ? {} : { body }),
    };
    return enabled
      ? resourcesApiClient.enablePrompt(input)
      : resourcesApiClient.disablePrompt(input);
  },

  rollback(resource: Resource, versionId: string, releaseNote: string): Promise<ResourceVersion> {
    return resourcesApiClient.rollbackPrompt({
      resourceId: resource.id,
      idempotencyKey: createIdempotencyKey(),
      body: {
        version_id: versionId,
        expected_resource_version: resource.resource_version,
        release_note: releaseNote,
      },
    });
  },

  versions(resourceId: string): Promise<ResourceVersionPage> {
    return resourcesApiClient.listPromptVersions({ resourceId, limit: 200 });
  },

  diff(resourceId: string, fromVersionId: string, toVersionId: string): Promise<ResourceDiff> {
    return resourcesApiClient.diffPromptVersions({
      resourceId,
      fromVersionId,
      toVersionId,
    });
  },

  references(resourceId: string): Promise<ResourceReferencePage> {
    return resourcesApiClient.listPromptReferences({ resourceId });
  },
};
