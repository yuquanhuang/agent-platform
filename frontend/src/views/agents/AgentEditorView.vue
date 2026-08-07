<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query';
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router';

import { ApiError } from '@/api/generated';
import type { Agent, ResourceBinding } from '@/api/generated/core-models';
import type { Resource, ResourceVersion } from '@/api/generated/resources-models';
import { agentService, buildAgentBindings } from '@/services/agents';
import { modelConfigService } from '@/services/models';
import { promptService } from '@/services/prompts';
import {
  ElAlert,
  ElButton,
  ElCard,
  ElDivider,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
  ElSkeleton,
  ElSpace,
  ElStep,
  ElSteps,
  ElTag,
  ElText,
} from '@/ui/element-plus';

interface ModelRouteForm {
  resourceId: string;
  versionPolicy: 'fixed' | 'resolve_on_publish';
  versionId: string;
}

const route = useRoute();
const router = useRouter();
const queryClient = useQueryClient();
const agentId = computed(() => String(route.params.id));
const activeStep = ref(0);
const saveError = ref('');
const conflict = ref(false);
const savedSignature = ref('');

const form = reactive({
  name: '',
  description: '',
  visibility: 'private' as 'private' | 'tenant',
  tags: '',
});
const promptBinding = reactive({
  resourceId: '',
  versionPolicy: 'fixed' as 'fixed' | 'resolve_on_publish',
  versionId: '',
});
const modelRoutes = reactive<ModelRouteForm[]>([]);
const fallbackErrorCodes = ref<Array<'RATE_LIMITED' | 'PROVIDER_UNAVAILABLE'>>(['RATE_LIMITED']);
const childAgentIds = ref<string[]>([]);

const agentQuery = useQuery({
  queryKey: computed(() => ['agent', agentId.value]),
  queryFn: () => agentService.get(agentId.value),
});
const promptsQuery = useQuery({
  queryKey: ['prompts', { agentEditor: true }],
  queryFn: () => promptService.list({ limit: 200 }),
});
const modelConfigsQuery = useQuery({
  queryKey: ['model-configs', { agentEditor: true }],
  queryFn: () => modelConfigService.list({ limit: 200 }),
});
const agentsQuery = useQuery({
  queryKey: ['agents', { childSelector: true }],
  queryFn: () => agentService.list({ limit: 200 }),
});

const promptVersionsQuery = useQuery({
  queryKey: computed(() => ['prompt-versions', promptBinding.resourceId]),
  enabled: computed(
    () => promptBinding.resourceId !== '' && promptBinding.versionPolicy === 'fixed',
  ),
  queryFn: () => promptService.versions(promptBinding.resourceId),
});

const selectedFixedModelIds = computed(() =>
  [
    ...new Set(
      modelRoutes
        .filter((model) => model.resourceId !== '' && model.versionPolicy === 'fixed')
        .map((model) => model.resourceId),
    ),
  ].sort(),
);
const modelVersionsQuery = useQuery({
  queryKey: computed(() => ['agent-model-versions', selectedFixedModelIds.value]),
  enabled: computed(() => selectedFixedModelIds.value.length > 0),
  queryFn: async () => {
    const entries = await Promise.all(
      selectedFixedModelIds.value.map(
        async (resourceId) =>
          [resourceId, (await modelConfigService.versions(resourceId)).items] as const,
      ),
    );
    return Object.fromEntries(entries) as Record<string, ReadonlyArray<ResourceVersion>>;
  },
});

const promptOptions = computed(() => [...(promptsQuery.data.value?.items ?? [])]);
const modelOptions = computed(() => [...(modelConfigsQuery.data.value?.items ?? [])]);
const childAgentOptions = computed(() =>
  [...(agentsQuery.data.value?.items ?? [])].filter((agent) => agent.id !== agentId.value),
);
const dependenciesPending = computed(
  () =>
    promptsQuery.isPending.value ||
    modelConfigsQuery.isPending.value ||
    agentsQuery.isPending.value,
);
const dependenciesError = computed(
  () => promptsQuery.isError.value || modelConfigsQuery.isError.value || agentsQuery.isError.value,
);
const saveBlocked = computed(
  () =>
    agentQuery.data.value === undefined ||
    agentQuery.isPending.value ||
    agentQuery.isError.value ||
    dependenciesPending.value ||
    dependenciesError.value,
);
const currentSignature = computed(() =>
  JSON.stringify({
    form,
    promptBinding,
    modelRoutes,
    fallbackErrorCodes: fallbackErrorCodes.value,
    childAgentIds: childAgentIds.value,
  }),
);
const dirty = computed(
  () => savedSignature.value !== '' && currentSignature.value !== savedSignature.value,
);
const availableSteps = [
  '基本信息',
  '模型与 Prompt',
  '工具与能力',
  'MCP 与子 Agent',
  '高级设置',
  '检查并保存',
];

watch(
  () => agentQuery.data.value,
  async (agent) => {
    if (agent === undefined) return;
    populate(agent);
    await nextTick();
    savedSignature.value = currentSignature.value;
  },
  { immediate: true },
);

watch(
  () => promptVersionsQuery.data.value,
  (page) => {
    if (
      page !== undefined &&
      promptBinding.versionPolicy === 'fixed' &&
      promptBinding.versionId === ''
    ) {
      promptBinding.versionId = page.items[0]?.id ?? '';
    }
  },
);

watch(
  () => modelVersionsQuery.data.value,
  (versions) => {
    if (versions === undefined) return;
    for (const model of modelRoutes) {
      if (model.versionPolicy === 'fixed' && model.versionId === '') {
        model.versionId = versions[model.resourceId]?.[0]?.id ?? '';
      }
    }
  },
  { deep: true },
);

const saveMutation = useMutation({
  mutationFn: () => {
    const agent = agentQuery.data.value;
    if (agent === undefined) throw new Error('Agent 尚未加载');
    const validationMessage = validate(agent);
    if (validationMessage !== '') throw new Error(validationMessage);
    return agentService.update(agent, {
      name: form.name.trim(),
      description: form.description.trim(),
      visibility: form.visibility,
      tags: parseTags(form.tags),
      bindings: buildAgentBindings({
        prompt: {
          resourceId: promptBinding.resourceId,
          versionPolicy: promptBinding.versionPolicy,
          ...(promptBinding.versionPolicy === 'fixed'
            ? { versionId: promptBinding.versionId }
            : {}),
        },
        models: modelRoutes.map((model, index) => ({
          resourceId: model.resourceId,
          versionPolicy: model.versionPolicy,
          ...(model.versionPolicy === 'fixed' ? { versionId: model.versionId } : {}),
          ...(index === 0 ? { fallbackErrorCodes: fallbackErrorCodes.value } : {}),
        })),
        childAgentIds: childAgentIds.value,
      }),
    });
  },
  onSuccess: async (agent) => {
    saveError.value = '';
    conflict.value = false;
    queryClient.setQueryData(['agent', agentId.value], agent);
    await nextTick();
    savedSignature.value = currentSignature.value;
  },
  onError: (error) => {
    conflict.value = error instanceof ApiError && error.code === 'RESOURCE_VERSION_CONFLICT';
    saveError.value = conflict.value
      ? '服务端 Agent 已被其他请求更新，当前修改未覆盖服务端内容。'
      : error instanceof Error
        ? error.message
        : '保存失败，请稍后重试';
  },
});

function populate(agent: Agent): void {
  Object.assign(form, {
    name: agent.name,
    description: agent.description ?? '',
    visibility: agent.visibility,
    tags: agent.tags.join(', '),
  });
  const prompt = agent.bindings.find((binding) => binding.resource_type === 'prompt');
  Object.assign(promptBinding, {
    resourceId: prompt?.resource_id ?? '',
    versionPolicy: prompt?.version_policy ?? 'fixed',
    versionId: prompt?.version_id ?? '',
  });
  const roles = { primary: 0, fallback_1: 1, fallback_2: 2 } as const;
  const models = agent.bindings
    .filter((binding) => binding.resource_type === 'model')
    .sort(
      (left, right) =>
        (roles[left.binding_role ?? 'primary'] ?? 0) -
        (roles[right.binding_role ?? 'primary'] ?? 0),
    );
  modelRoutes.splice(
    0,
    modelRoutes.length,
    ...models.map((binding) => ({
      resourceId: binding.resource_id,
      versionPolicy: binding.version_policy,
      versionId: binding.version_id ?? '',
    })),
  );
  const configuredErrors = models[0]?.configuration?.fallback_error_codes;
  fallbackErrorCodes.value =
    configuredErrors === undefined ? ['RATE_LIMITED'] : [...configuredErrors];
  childAgentIds.value = agent.bindings
    .filter((binding) => binding.resource_type === 'agent')
    .map((binding) => binding.resource_id);
}

function validate(agent: Agent): string {
  if (dependenciesPending.value) return '依赖资源尚未加载完成';
  if (dependenciesError.value) return '依赖资源加载失败，无法安全保存 Agent Draft';
  if (form.name.trim() === '') return 'Agent 名称不能为空';
  if (parseTags(form.tags).some((tag) => tag.length > 32)) return '单个标签不能超过 32 个字符';
  if (promptBinding.resourceId === '') return '请选择 Prompt';
  if (promptBinding.versionPolicy === 'fixed' && promptBinding.versionId === '') {
    return '固定 Prompt 绑定必须选择已发布版本';
  }
  if (agent.runtime_type === 'agentscope' && modelRoutes.length === 0) {
    return 'AgentScope Agent 至少需要一个模型配置';
  }
  if (modelRoutes.some((model) => model.resourceId === '')) return '模型路由存在未选择的配置';
  if (new Set(modelRoutes.map((model) => model.resourceId)).size !== modelRoutes.length) {
    return '同一模型配置不能重复用于多个路由角色';
  }
  if (modelRoutes.some((model) => model.versionPolicy === 'fixed' && model.versionId === '')) {
    return '固定模型绑定必须选择已发布版本';
  }
  if (modelRoutes.length > 1 && fallbackErrorCodes.value.length === 0) {
    return '启用 fallback 时至少选择一个受控错误码';
  }
  return '';
}

function addModelRoute(): void {
  if (modelRoutes.length >= 3) return;
  modelRoutes.push({ resourceId: '', versionPolicy: 'fixed', versionId: '' });
}

function removeModelRoute(index: number): void {
  modelRoutes.splice(index, 1);
}

function onPromptResourceChanged(): void {
  promptBinding.versionId = '';
}

function onPromptPolicyChanged(): void {
  if (promptBinding.versionPolicy === 'resolve_on_publish') promptBinding.versionId = '';
}

function onModelResourceChanged(model: ModelRouteForm): void {
  model.versionId = '';
}

function onModelPolicyChanged(model: ModelRouteForm): void {
  if (model.versionPolicy === 'resolve_on_publish') model.versionId = '';
}

function modelVersions(resourceId: string): ReadonlyArray<ResourceVersion> {
  return modelVersionsQuery.data.value?.[resourceId] ?? [];
}

function modelLabel(resourceId: string): string {
  return resourceLabel(modelOptions.value, resourceId);
}

function promptLabel(resourceId: string): string {
  return resourceLabel(promptOptions.value, resourceId);
}

function resourceLabel(resources: ReadonlyArray<Resource>, resourceId: string): string {
  const resource = resources.find((item) => item.id === resourceId);
  return resource === undefined ? resourceId : `${resource.name} (${resource.code})`;
}

function bindingSummary(bindings: ReadonlyArray<ResourceBinding>): string {
  return bindings.length === 0 ? '尚未配置' : `${bindings.length} 项资源绑定`;
}

function previewBindings(): ReadonlyArray<ResourceBinding> {
  return buildAgentBindings({
    prompt:
      promptBinding.resourceId === ''
        ? null
        : {
            resourceId: promptBinding.resourceId,
            versionPolicy: promptBinding.versionPolicy,
            ...(promptBinding.versionPolicy === 'fixed'
              ? { versionId: promptBinding.versionId }
              : {}),
          },
    models: modelRoutes.map((model, index) => ({
      resourceId: model.resourceId,
      versionPolicy: model.versionPolicy,
      ...(model.versionPolicy === 'fixed' ? { versionId: model.versionId } : {}),
      ...(index === 0 ? { fallbackErrorCodes: fallbackErrorCodes.value } : {}),
    })),
    childAgentIds: childAgentIds.value,
  });
}

function parseTags(value: string): string[] {
  return [
    ...new Set(
      value
        .split(',')
        .map((tag) => tag.trim())
        .filter(Boolean),
    ),
  ].slice(0, 20);
}

async function reloadLatest(): Promise<void> {
  saveError.value = '';
  conflict.value = false;
  await agentQuery.refetch();
}

function handleBeforeUnload(event: BeforeUnloadEvent): void {
  if (!dirty.value) return;
  event.preventDefault();
}

onMounted(() => window.addEventListener('beforeunload', handleBeforeUnload));
onBeforeUnmount(() => window.removeEventListener('beforeunload', handleBeforeUnload));
onBeforeRouteLeave(
  () => !dirty.value || window.confirm('当前 Agent 草稿存在未保存修改，确认离开？'),
);
</script>

<template>
  <section class="agent-editor" aria-labelledby="agent-editor-title">
    <header class="agent-editor__heading">
      <div>
        <ElText id="agent-editor-title" tag="h1" size="large">Agent Draft 编辑器</ElText>
        <p>按步骤维护草稿；保存后进入发布页预览 Snapshot Diff 和目标 Deployment。</p>
      </div>
      <ElSpace>
        <ElTag v-if="agentQuery.isPending.value" type="info">加载中</ElTag>
        <ElTag v-else-if="agentQuery.isError.value" type="danger">加载失败</ElTag>
        <ElTag v-else-if="dependenciesPending" type="info">依赖加载中</ElTag>
        <ElTag v-else-if="dependenciesError" type="danger">依赖加载失败</ElTag>
        <ElTag v-else-if="dirty" type="warning">有未保存修改</ElTag>
        <ElTag v-else type="success">已保存</ElTag>
        <ElButton @click="router.push({ name: 'agent-list' })">返回列表</ElButton>
        <ElButton
          :disabled="dirty || agentQuery.data.value === undefined"
          @click="router.push({ name: 'agent-publish', params: { id: agentId } })"
        >
          版本与发布
        </ElButton>
        <ElButton
          type="primary"
          :disabled="saveBlocked"
          :loading="saveMutation.isPending.value"
          @click="saveMutation.mutate()"
        >
          保存草稿
        </ElButton>
      </ElSpace>
    </header>

    <ElAlert
      v-if="saveError"
      :title="saveError"
      :type="conflict ? 'warning' : 'error'"
      show-icon
      :closable="false"
    >
      <ElButton v-if="conflict" @click="reloadLatest">重新加载最新版本</ElButton>
    </ElAlert>

    <ElSkeleton v-if="agentQuery.isPending.value" :rows="10" animated />
    <ElAlert
      v-else-if="agentQuery.isError.value"
      title="Agent 草稿加载失败"
      type="error"
      :closable="false"
      show-icon
    >
      <ElButton @click="agentQuery.refetch()">重新加载</ElButton>
    </ElAlert>
    <template v-else-if="agentQuery.data.value">
      <ElCard shadow="never">
        <ElSteps :active="activeStep" finish-status="success" align-center>
          <ElStep v-for="step in availableSteps" :key="step" :title="step" />
        </ElSteps>
      </ElCard>

      <ElCard shadow="never" class="agent-editor__panel">
        <template v-if="activeStep === 0">
          <ElText tag="h2">基本信息</ElText>
          <ElAlert
            title="编码与 Runtime 创建后不可修改；负责人由当前租户身份确定。"
            type="info"
            :closable="false"
            show-icon
          />
          <ElForm label-position="top">
            <div class="agent-editor__form-grid">
              <ElFormItem label="编码"
                ><ElInput :model-value="agentQuery.data.value.code" disabled
              /></ElFormItem>
              <ElFormItem label="Runtime"
                ><ElInput :model-value="agentQuery.data.value.runtime_type" disabled
              /></ElFormItem>
            </div>
            <ElFormItem label="名称" required
              ><ElInput v-model="form.name" maxlength="100"
            /></ElFormItem>
            <ElFormItem label="描述"
              ><ElInput
                v-model="form.description"
                type="textarea"
                :rows="4"
                maxlength="2000"
                show-word-limit
            /></ElFormItem>
            <div class="agent-editor__form-grid">
              <ElFormItem label="可见范围">
                <select v-model="form.visibility" class="agent-editor__select">
                  <option value="private">仅自己</option>
                  <option value="tenant">租户内</option>
                </select>
              </ElFormItem>
              <ElFormItem label="标签"
                ><ElInput v-model="form.tags" placeholder="客服,内部"
              /></ElFormItem>
            </div>
          </ElForm>
        </template>

        <template v-else-if="activeStep === 1">
          <ElText tag="h2">Runtime、模型与 Prompt</ElText>
          <ElAlert
            v-if="dependenciesError"
            title="部分依赖资源加载失败，已阻止保存无效绑定。"
            type="error"
            :closable="false"
            show-icon
          />
          <ElForm label-position="top">
            <ElDivider content-position="left">Prompt 绑定</ElDivider>
            <div class="agent-editor__binding-grid">
              <ElFormItem label="Prompt" required>
                <select
                  v-model="promptBinding.resourceId"
                  class="agent-editor__select"
                  @change="onPromptResourceChanged"
                >
                  <option value="">请选择 Prompt</option>
                  <option v-for="prompt in promptOptions" :key="prompt.id" :value="prompt.id">
                    {{ prompt.name }} ({{ prompt.code }})
                  </option>
                </select>
              </ElFormItem>
              <ElFormItem label="版本策略">
                <select
                  v-model="promptBinding.versionPolicy"
                  class="agent-editor__select"
                  @change="onPromptPolicyChanged"
                >
                  <option value="fixed">固定已发布版本</option>
                  <option value="resolve_on_publish">发布时解析</option>
                </select>
              </ElFormItem>
              <ElFormItem
                v-if="promptBinding.versionPolicy === 'fixed'"
                label="Prompt 版本"
                required
              >
                <select
                  v-model="promptBinding.versionId"
                  class="agent-editor__select"
                  :disabled="promptVersionsQuery.isFetching.value"
                >
                  <option value="">请选择已发布版本</option>
                  <option
                    v-for="version in promptVersionsQuery.data.value?.items ?? []"
                    :key="version.id"
                    :value="version.id"
                  >
                    v{{ version.version_no }} · {{ version.release_note || '无发布说明' }}
                  </option>
                </select>
              </ElFormItem>
            </div>

            <ElDivider content-position="left">模型路由</ElDivider>
            <ElAlert
              v-if="agentQuery.data.value.runtime_type === 'codex'"
              title="Codex 模型可由后续 Runtime Target Policy 解析；当前可选绑定平台 ModelConfig。"
              type="info"
              :closable="false"
              show-icon
            />
            <div v-if="modelRoutes.length === 0" class="agent-editor__empty-binding">
              <ElEmpty description="尚未配置模型路由" />
            </div>
            <div v-for="(model, index) in modelRoutes" :key="index" class="agent-editor__route">
              <div class="agent-editor__route-heading">
                <ElTag :type="index === 0 ? 'success' : 'warning'">
                  {{ ['primary', 'fallback_1', 'fallback_2'][index] }}
                </ElTag>
                <ElButton
                  v-if="index > 0 || agentQuery.data.value.runtime_type === 'codex'"
                  link
                  type="danger"
                  @click="removeModelRoute(index)"
                  >移除</ElButton
                >
              </div>
              <div class="agent-editor__binding-grid">
                <ElFormItem label="模型配置" required>
                  <select
                    v-model="model.resourceId"
                    class="agent-editor__select"
                    @change="onModelResourceChanged(model)"
                  >
                    <option value="">请选择模型配置</option>
                    <option v-for="config in modelOptions" :key="config.id" :value="config.id">
                      {{ config.name }} ({{ config.code }})
                    </option>
                  </select>
                </ElFormItem>
                <ElFormItem label="版本策略">
                  <select
                    v-model="model.versionPolicy"
                    class="agent-editor__select"
                    @change="onModelPolicyChanged(model)"
                  >
                    <option value="fixed">固定已发布版本</option>
                    <option value="resolve_on_publish">发布时解析</option>
                  </select>
                </ElFormItem>
                <ElFormItem v-if="model.versionPolicy === 'fixed'" label="模型版本" required>
                  <select
                    v-model="model.versionId"
                    class="agent-editor__select"
                    :disabled="modelVersionsQuery.isFetching.value"
                  >
                    <option value="">请选择已发布版本</option>
                    <option
                      v-for="version in modelVersions(model.resourceId)"
                      :key="version.id"
                      :value="version.id"
                    >
                      v{{ version.version_no }} · {{ version.release_note || '无发布说明' }}
                    </option>
                  </select>
                </ElFormItem>
              </div>
            </div>
            <ElButton :disabled="modelRoutes.length >= 3" @click="addModelRoute">
              {{ modelRoutes.length === 0 ? '添加主模型' : '添加 fallback' }}
            </ElButton>
            <ElFormItem v-if="modelRoutes.length > 1" label="允许触发 fallback 的错误码" required>
              <select v-model="fallbackErrorCodes" class="agent-editor__select" multiple>
                <option value="RATE_LIMITED">供应商限流 RATE_LIMITED</option>
                <option value="PROVIDER_UNAVAILABLE">供应商不可用 PROVIDER_UNAVAILABLE</option>
              </select>
            </ElFormItem>
          </ElForm>
        </template>

        <template v-else-if="activeStep === 2">
          <ElText tag="h2">Skill、工具与审批</ElText>
          <ElAlert
            title="Skill/MCP 管理、Tool Gateway 和审批策略将在 Epic 6 提供；当前阶段不展示无法落库的配置入口。"
            type="info"
            :closable="false"
            show-icon
          />
        </template>

        <template v-else-if="activeStep === 3">
          <ElText tag="h2">MCP、知识与子 Agent</ElText>
          <ElAlert
            title="知识库与 MCP 尚无当前生产管理链路；子 Agent 绑定可按发布时解析策略保存。"
            type="info"
            :closable="false"
            show-icon
          />
          <ElForm label-position="top">
            <ElFormItem label="子 Agent">
              <select v-model="childAgentIds" class="agent-editor__select" multiple>
                <option v-for="agent in childAgentOptions" :key="agent.id" :value="agent.id">
                  {{ agent.name }} ({{ agent.code }})
                </option>
              </select>
            </ElFormItem>
          </ElForm>
        </template>

        <template v-else-if="activeStep === 4">
          <ElText tag="h2">Sandbox、Workspace 与预算</ElText>
          <ElAlert
            title="Sandbox Profile、Workspace 配额和周期预算管理入口将在后续 Epic 提供；当前不会生成占位 ID 或虚构上限。"
            type="info"
            :closable="false"
            show-icon
          />
        </template>

        <template v-else>
          <ElText tag="h2">检查并保存草稿</ElText>
          <ElAlert
            title="本步骤只保存 Agent Draft，不创建 Snapshot、Release 或 Deployment。"
            type="warning"
            :closable="false"
            show-icon
          />
          <dl class="agent-editor__summary">
            <div>
              <dt>Agent</dt>
              <dd>{{ form.name }} / {{ agentQuery.data.value.code }}</dd>
            </div>
            <div>
              <dt>Runtime</dt>
              <dd>{{ agentQuery.data.value.runtime_type }}</dd>
            </div>
            <div>
              <dt>Prompt</dt>
              <dd>{{ promptLabel(promptBinding.resourceId) }}</dd>
            </div>
            <div>
              <dt>模型路由</dt>
              <dd>
                {{
                  modelRoutes.map((model) => modelLabel(model.resourceId)).join(' → ') || '未配置'
                }}
              </dd>
            </div>
            <div>
              <dt>绑定摘要</dt>
              <dd>{{ bindingSummary(previewBindings()) }}</dd>
            </div>
            <div>
              <dt>资源版本</dt>
              <dd>{{ agentQuery.data.value.resource_version }}</dd>
            </div>
          </dl>
          <ElButton
            type="primary"
            :disabled="saveBlocked"
            :loading="saveMutation.isPending.value"
            @click="saveMutation.mutate()"
            >保存 Agent Draft</ElButton
          >
        </template>
      </ElCard>

      <div class="agent-editor__navigation">
        <ElButton :disabled="activeStep === 0" @click="activeStep -= 1">上一步</ElButton>
        <ElButton
          v-if="activeStep < availableSteps.length - 1"
          type="primary"
          @click="activeStep += 1"
          >下一步</ElButton
        >
      </div>
    </template>
  </section>
</template>

<style scoped>
.agent-editor {
  display: grid;
  gap: var(--ap-space-6);
}

.agent-editor__heading,
.agent-editor__route-heading,
.agent-editor__navigation {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ap-space-4);
}

.agent-editor__heading {
  align-items: flex-start;
}

.agent-editor__heading p {
  color: var(--ap-text-secondary);
}

.agent-editor__panel {
  min-height: 430px;
}

.agent-editor__select {
  width: 100%;
  min-height: 32px;
  padding: 0 var(--ap-space-3);
  border: 1px solid var(--ap-border-color);
  border-radius: var(--ap-radius-md);
  color: var(--ap-text-primary);
  background: var(--ap-surface-color);
}

.agent-editor__select[multiple] {
  min-height: 84px;
  padding: var(--ap-space-2);
}

.agent-editor__form-grid,
.agent-editor__binding-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--ap-space-4);
}

.agent-editor__binding-grid {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.agent-editor__route {
  margin-bottom: var(--ap-space-4);
  padding: var(--ap-space-4);
  border: 1px solid var(--ap-border-color);
  border-radius: var(--ap-radius-md);
}

.agent-editor__route-heading {
  margin-bottom: var(--ap-space-3);
}

.agent-editor__empty-binding {
  border: 1px dashed var(--ap-border-color);
  border-radius: var(--ap-radius-md);
}

.agent-editor__summary {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--ap-space-4);
  margin: var(--ap-space-6) 0;
}

.agent-editor__summary div {
  padding: var(--ap-space-4);
  border: 1px solid var(--ap-border-color);
  border-radius: var(--ap-radius-md);
}

.agent-editor__summary dt {
  color: var(--ap-text-secondary);
  font-size: 12px;
}

.agent-editor__summary dd {
  margin: var(--ap-space-2) 0 0;
  overflow-wrap: anywhere;
}

@media (max-width: 900px) {
  .agent-editor__form-grid,
  .agent-editor__binding-grid,
  .agent-editor__summary {
    grid-template-columns: 1fr;
  }

  .agent-editor__heading {
    flex-direction: column;
  }
}
</style>
