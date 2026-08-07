import type {
  ActionRequest,
  ModelConfigCreateRequest,
  ModelProviderCreateRequest,
  OperationAccepted,
  Resource,
  ResourceDiff,
  ResourcePage,
  ResourceReferencePage,
  ResourceUpdateRequest,
  ResourceVersion,
  ResourceVersionPage,
} from '@/api/generated/resources-models';
import type {
  ResourceContentModelConfig,
  ResourceContentModelProvider,
} from '@/api/generated/resource-content';
import { resourcesApiClient } from '@/services/api';
import { createIdempotencyKey, formatResourceEtag } from '@/services/prompts';

type ModelProviderUpdateRequest = Omit<ResourceUpdateRequest, 'content'> & {
  readonly content?: ResourceContentModelProvider;
};
type ModelConfigUpdateRequest = Omit<ResourceUpdateRequest, 'content'> & {
  readonly content?: ResourceContentModelConfig;
};
type VersionedResource = Pick<Resource, 'id' | 'resource_version'>;

export const modelProviderService = {
  list(input: { limit?: number; cursor?: string } = {}): Promise<ResourcePage> {
    return resourcesApiClient.listModelProviders(input);
  },

  get(resourceId: string): Promise<Resource> {
    return resourcesApiClient.getModelProvider({ resourceId });
  },

  create(body: ModelProviderCreateRequest): Promise<Resource> {
    return resourcesApiClient.createModelProvider({
      idempotencyKey: createIdempotencyKey(),
      body,
    });
  },

  update(resource: VersionedResource, body: ModelProviderUpdateRequest): Promise<Resource> {
    return resourcesApiClient.updateModelProvider({
      resourceId: resource.id,
      ifMatch: formatResourceEtag(resource.resource_version),
      body,
    });
  },

  delete(resource: VersionedResource): Promise<OperationAccepted> {
    return resourcesApiClient.deleteModelProvider({
      resourceId: resource.id,
      idempotencyKey: createIdempotencyKey(),
      ifMatch: formatResourceEtag(resource.resource_version),
    });
  },

  testConnection(resourceId: string): Promise<OperationAccepted> {
    return resourcesApiClient.testModelProviderConnection({
      resourceId,
      idempotencyKey: createIdempotencyKey(),
    });
  },

  setEnabled(
    resource: VersionedResource,
    enabled: boolean,
    body?: ActionRequest,
  ): Promise<Resource> {
    const input = {
      resourceId: resource.id,
      idempotencyKey: createIdempotencyKey(),
      ifMatch: formatResourceEtag(resource.resource_version),
      ...(body === undefined ? {} : { body }),
    };
    return enabled
      ? resourcesApiClient.enableModelProvider(input)
      : resourcesApiClient.disableModelProvider(input);
  },
};

export const modelConfigService = {
  list(input: { limit?: number; cursor?: string } = {}): Promise<ResourcePage> {
    return resourcesApiClient.listModelConfigs(input);
  },

  get(resourceId: string): Promise<Resource> {
    return resourcesApiClient.getModelConfig({ resourceId });
  },

  create(body: ModelConfigCreateRequest): Promise<Resource> {
    return resourcesApiClient.createModelConfig({
      idempotencyKey: createIdempotencyKey(),
      body,
    });
  },

  update(resource: VersionedResource, body: ModelConfigUpdateRequest): Promise<Resource> {
    return resourcesApiClient.updateModelConfig({
      resourceId: resource.id,
      ifMatch: formatResourceEtag(resource.resource_version),
      body,
    });
  },

  delete(resource: VersionedResource): Promise<OperationAccepted> {
    return resourcesApiClient.deleteModelConfig({
      resourceId: resource.id,
      idempotencyKey: createIdempotencyKey(),
      ifMatch: formatResourceEtag(resource.resource_version),
    });
  },

  publish(resource: VersionedResource, releaseNote: string): Promise<ResourceVersion> {
    return resourcesApiClient.publishModelConfig({
      resourceId: resource.id,
      idempotencyKey: createIdempotencyKey(),
      body: {
        expected_resource_version: resource.resource_version,
        release_note: releaseNote,
      },
    });
  },

  setEnabled(
    resource: VersionedResource,
    enabled: boolean,
    body?: ActionRequest,
  ): Promise<Resource> {
    const input = {
      resourceId: resource.id,
      idempotencyKey: createIdempotencyKey(),
      ifMatch: formatResourceEtag(resource.resource_version),
      ...(body === undefined ? {} : { body }),
    };
    return enabled
      ? resourcesApiClient.enableModelConfig(input)
      : resourcesApiClient.disableModelConfig(input);
  },

  rollback(
    resource: VersionedResource,
    versionId: string,
    releaseNote: string,
  ): Promise<ResourceVersion> {
    return resourcesApiClient.rollbackModelConfig({
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
    return resourcesApiClient.listModelConfigVersions({ resourceId, limit: 200 });
  },

  diff(resourceId: string, fromVersionId: string, toVersionId: string): Promise<ResourceDiff> {
    return resourcesApiClient.diffModelConfigVersions({
      resourceId,
      fromVersionId,
      toVersionId,
    });
  },

  references(resourceId: string): Promise<ResourceReferencePage> {
    return resourcesApiClient.listModelConfigReferences({ resourceId });
  },
};
