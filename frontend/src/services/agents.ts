import type {
  Agent,
  AgentCreateRequest,
  AgentPage,
  AgentUpdateRequest,
  CopyAgentRequest,
  DisableAgentRequest,
  OperationAccepted,
  ReferencePage,
  ResourceBinding,
} from '@/api/generated/core-models';
import { coreApiClient } from '@/services/api';
import { createIdempotencyKey, formatResourceEtag } from '@/services/prompts';

export type BindingVersionPolicy = 'fixed' | 'resolve_on_publish';

export interface ResourceBindingSelection {
  readonly resourceId: string;
  readonly versionPolicy: BindingVersionPolicy;
  readonly versionId?: string;
}

export interface ModelBindingSelection extends ResourceBindingSelection {
  readonly fallbackErrorCodes?: ReadonlyArray<'RATE_LIMITED' | 'PROVIDER_UNAVAILABLE'>;
}

export function buildAgentBindings(input: {
  readonly prompt: ResourceBindingSelection | null;
  readonly models: ReadonlyArray<ModelBindingSelection>;
  readonly childAgentIds: ReadonlyArray<string>;
}): ReadonlyArray<ResourceBinding> {
  const bindings: ResourceBinding[] = [];
  if (input.prompt !== null) bindings.push(binding('prompt', input.prompt));

  const models = input.models.filter((model) => model.resourceId !== '').slice(0, 3);
  const hasFallback = models.length > 1;
  models.forEach((model, index) => {
    const role = ['primary', 'fallback_1', 'fallback_2'][index] as
      'primary' | 'fallback_1' | 'fallback_2';
    bindings.push({
      ...binding('model', model),
      binding_role: role,
      ...(index === 0 && hasFallback
        ? {
            configuration_schema_version: 'model-routing/v1' as const,
            configuration: {
              fallback_error_codes:
                model.fallbackErrorCodes?.length === 0 || model.fallbackErrorCodes === undefined
                  ? (['RATE_LIMITED'] as const)
                  : model.fallbackErrorCodes,
            },
          }
        : {}),
    });
  });

  bindings.push(
    ...input.childAgentIds.map((resourceId) => ({
      resource_type: 'agent' as const,
      resource_id: resourceId,
      version_policy: 'resolve_on_publish' as const,
    })),
  );
  return bindings;
}

function binding(
  resourceType: 'prompt' | 'model',
  selection: ResourceBindingSelection,
): ResourceBinding {
  return {
    resource_type: resourceType,
    resource_id: selection.resourceId,
    version_policy: selection.versionPolicy,
    ...(selection.versionPolicy === 'fixed' ? { version_id: selection.versionId ?? null } : {}),
  };
}

export const agentService = {
  list(
    input: { limit?: number; cursor?: string; status?: string; keyword?: string } = {},
  ): Promise<AgentPage> {
    return coreApiClient.listAgents(input);
  },

  get(agentId: string): Promise<Agent> {
    return coreApiClient.getAgent({ agentId });
  },

  create(body: AgentCreateRequest): Promise<Agent> {
    return coreApiClient.createAgent({ idempotencyKey: createIdempotencyKey(), body });
  },

  update(agent: Pick<Agent, 'id' | 'resource_version'>, body: AgentUpdateRequest): Promise<Agent> {
    return coreApiClient.updateAgent({
      agentId: agent.id,
      ifMatch: formatResourceEtag(agent.resource_version),
      body,
    });
  },

  copy(agentId: string, body: CopyAgentRequest): Promise<Agent> {
    return coreApiClient.copyAgent({
      agentId,
      idempotencyKey: createIdempotencyKey(),
      body,
    });
  },

  disable(
    agent: Pick<Agent, 'id' | 'resource_version'>,
    body?: DisableAgentRequest,
  ): Promise<Agent> {
    return coreApiClient.disableAgent({
      agentId: agent.id,
      idempotencyKey: createIdempotencyKey(),
      ifMatch: formatResourceEtag(agent.resource_version),
      ...(body === undefined ? {} : { body }),
    });
  },

  references(agentId: string): Promise<ReferencePage> {
    return coreApiClient.listAgentReferences({ agentId });
  },

  delete(agent: Pick<Agent, 'id' | 'resource_version'>): Promise<OperationAccepted> {
    return coreApiClient.deleteAgent({
      agentId: agent.id,
      idempotencyKey: createIdempotencyKey(),
      ifMatch: formatResourceEtag(agent.resource_version),
    });
  },
};
