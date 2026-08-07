<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query';
import { computed, reactive, ref } from 'vue';

import { ApiError } from '@/api/generated';
import type {
  ResourceContentModelConfig,
  ResourceContentModelProvider,
} from '@/api/generated/resource-content';
import type { Resource } from '@/api/generated/resources-models';
import { modelConfigService, modelProviderService } from '@/services/models';
import {
  ElAlert,
  ElButton,
  ElCard,
  ElCheckbox,
  ElDialog,
  ElDivider,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
  ElInputNumber,
  ElPopconfirm,
  ElSkeleton,
  ElSpace,
  ElTable,
  ElTableColumn,
  ElTag,
} from '@/ui/element-plus';

type Capability = ResourceContentModelConfig['capabilities'][number];
type DefaultParameters = Record<string, string | number | boolean | null>;
type ModelConfigResource = Omit<Resource, 'resource_type' | 'content'> & {
  readonly resource_type: 'model_config';
  readonly content: ResourceContentModelConfig;
};
type ModelProviderResource = Omit<Resource, 'resource_type' | 'content'> & {
  readonly resource_type: 'model_provider';
  readonly content: ResourceContentModelProvider;
};
type VersionedTarget = Pick<Resource, 'id' | 'name' | 'resource_version'>;

const capabilities: Capability[] = ['stream', 'tools', 'vision', 'structured_output', 'reasoning'];
const queryClient = useQueryClient();
const keyword = ref('');
const editorVisible = ref(false);
const editing = ref<Resource | null>(null);
const publishTarget = ref<VersionedTarget | null>(null);
const versionTarget = ref<VersionedTarget | null>(null);
const releaseNote = ref('');
const actionError = ref('');
const conflict = ref(false);
const form = reactive({
  code: '',
  name: '',
  description: '',
  visibility: 'tenant' as 'private' | 'tenant',
  providerId: '',
  modelId: '',
  capabilities: [] as Capability[],
  defaultParametersText: '{\n  "temperature": 0.2\n}',
  maxContextTokens: null as number | null,
  rateLimitRpm: null as number | null,
});

const configsQuery = useQuery({
  queryKey: ['model-configs'],
  queryFn: () => modelConfigService.list({ limit: 200 }),
});
const providersQuery = useQuery({
  queryKey: ['model-providers'],
  queryFn: () => modelProviderService.list({ limit: 200 }),
});
const versionQuery = useQuery({
  queryKey: computed(() => ['model-config-versions', versionTarget.value?.id]),
  enabled: computed(() => versionTarget.value !== null),
  queryFn: () => {
    if (versionTarget.value === null) throw new Error('模型配置尚未选择');
    return modelConfigService.versions(versionTarget.value.id);
  },
});
const referencesQuery = useQuery({
  queryKey: computed(() => ['model-config-references', versionTarget.value?.id]),
  enabled: computed(() => versionTarget.value !== null),
  queryFn: () => {
    if (versionTarget.value === null) throw new Error('模型配置尚未选择');
    return modelConfigService.references(versionTarget.value.id);
  },
});
const diffVersionIds = computed(() => {
  const versions = versionQuery.data.value?.items ?? [];
  return versions.length < 2 ? null : { from: versions[1]!.id, to: versions[0]!.id };
});
const diffQuery = useQuery({
  queryKey: computed(() => ['model-config-diff', versionTarget.value?.id, diffVersionIds.value]),
  enabled: computed(() => versionTarget.value !== null && diffVersionIds.value !== null),
  queryFn: () => {
    if (versionTarget.value === null || diffVersionIds.value === null) {
      throw new Error('模型配置版本不足');
    }
    return modelConfigService.diff(
      versionTarget.value.id,
      diffVersionIds.value.from,
      diffVersionIds.value.to,
    );
  },
});

const providerRows = computed(() =>
  (providersQuery.data.value?.items ?? [])
    .filter(isModelProviderResource)
    .filter((resource) => resource.status !== 'DELETED'),
);
const configRows = computed(() =>
  (configsQuery.data.value?.items ?? [])
    .filter(isModelConfigResource)
    .filter(
      (resource) =>
        keyword.value.trim() === '' ||
        resource.code.toLowerCase().includes(keyword.value.toLowerCase()) ||
        resource.name.toLowerCase().includes(keyword.value.toLowerCase()) ||
        resource.content.model_id.toLowerCase().includes(keyword.value.toLowerCase()),
    ),
);

const saveMutation = useMutation({
  mutationFn: () => {
    const content = buildContent();
    const resource = editing.value;
    return resource === null
      ? modelConfigService.create({
          code: form.code.trim(),
          name: form.name.trim(),
          ...(form.description.trim() === '' ? {} : { description: form.description.trim() }),
          visibility: form.visibility,
          content_schema_version: '1.0',
          content,
        })
      : modelConfigService.update(resource, {
          name: form.name.trim(),
          description: form.description.trim(),
          visibility: form.visibility,
          content_schema_version: resource.content_schema_version,
          content,
        });
  },
  onSuccess: async () => {
    closeEditor();
    await queryClient.invalidateQueries({ queryKey: ['model-configs'] });
  },
  onError: handleError,
});
const publishMutation = useMutation({
  mutationFn: () => {
    if (publishTarget.value === null) throw new Error('模型配置尚未选择');
    return modelConfigService.publish(publishTarget.value, releaseNote.value.trim());
  },
  onSuccess: async () => {
    publishTarget.value = null;
    releaseNote.value = '';
    await refreshConfigsAndVersions();
  },
  onError: handleError,
});
const rollbackMutation = useMutation({
  mutationFn: (versionId: string) => {
    if (versionTarget.value === null) throw new Error('模型配置尚未选择');
    return modelConfigService.rollback(versionTarget.value, versionId, '从历史版本回滚');
  },
  onSuccess: refreshConfigsAndVersions,
  onError: handleError,
});
const statusMutation = useMutation({
  mutationFn: ({ resource, enabled }: { resource: Resource; enabled: boolean }) =>
    modelConfigService.setEnabled(resource, enabled),
  onSuccess: () => queryClient.invalidateQueries({ queryKey: ['model-configs'] }),
  onError: handleError,
});
const deleteMutation = useMutation({
  mutationFn: (resource: Resource) => modelConfigService.delete(resource),
  onSuccess: () => queryClient.invalidateQueries({ queryKey: ['model-configs'] }),
  onError: handleError,
});

function buildContent(): ResourceContentModelConfig {
  return {
    resource_type: 'model_config',
    provider_id: form.providerId,
    model_id: form.modelId.trim(),
    capabilities: [...form.capabilities],
    default_parameters: parseDefaultParameters(),
    max_context_tokens: form.maxContextTokens,
    rate_limit_rpm: form.rateLimitRpm,
  };
}

function parseDefaultParameters(): DefaultParameters {
  const parsed: unknown = JSON.parse(form.defaultParametersText);
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== 'object') {
    throw new Error('默认参数必须是 JSON 对象');
  }
  for (const value of Object.values(parsed)) {
    if (value !== null && !['string', 'number', 'boolean'].includes(typeof value)) {
      throw new Error('默认参数值只能是字符串、数字、布尔值或 null');
    }
  }
  return parsed as DefaultParameters;
}

function openCreate(): void {
  editing.value = null;
  Object.assign(form, {
    code: '',
    name: '',
    description: '',
    visibility: 'tenant',
    providerId: providerRows.value[0]?.id ?? '',
    modelId: '',
    capabilities: [] as Capability[],
    defaultParametersText: '{\n  "temperature": 0.2\n}',
    maxContextTokens: null,
    rateLimitRpm: null,
  });
  conflict.value = false;
  actionError.value = '';
  editorVisible.value = true;
}

function isModelConfigResource(resource: Resource): resource is ModelConfigResource {
  return (
    resource.resource_type === 'model_config' && resource.content.resource_type === 'model_config'
  );
}

function isModelProviderResource(resource: Resource): resource is ModelProviderResource {
  return (
    resource.resource_type === 'model_provider' &&
    resource.content.resource_type === 'model_provider'
  );
}

function asModelConfigResource(value: unknown): ModelConfigResource | null {
  if (typeof value !== 'object' || value === null) return null;
  const resource = value as Resource;
  return isModelConfigResource(resource) ? resource : null;
}

function openEdit(value: unknown): void {
  const resource = asModelConfigResource(value);
  if (resource === null) return;
  editing.value = resource;
  Object.assign(form, {
    code: resource.code,
    name: resource.name,
    description: resource.description ?? '',
    visibility: resource.visibility,
    providerId: resource.content.provider_id,
    modelId: resource.content.model_id,
    capabilities: [...resource.content.capabilities],
    defaultParametersText: JSON.stringify(resource.content.default_parameters, null, 2),
    maxContextTokens: resource.content.max_context_tokens ?? null,
    rateLimitRpm: resource.content.rate_limit_rpm ?? null,
  });
  conflict.value = false;
  actionError.value = '';
  editorVisible.value = true;
}

function openVersions(value: unknown): void {
  const resource = asModelConfigResource(value);
  versionTarget.value = resource === null ? null : versionedTarget(resource);
}

function openPublish(value: unknown): void {
  const resource = asModelConfigResource(value);
  publishTarget.value = resource === null ? null : versionedTarget(resource);
}

function versionedTarget(resource: Resource): VersionedTarget {
  return {
    id: resource.id,
    name: resource.name,
    resource_version: resource.resource_version,
  };
}

function toggleConfig(value: unknown): void {
  const resource = asModelConfigResource(value);
  if (resource !== null) {
    statusMutation.mutate({ resource, enabled: resource.status === 'DISABLED' });
  }
}

function deleteConfig(value: unknown): void {
  const resource = asModelConfigResource(value);
  if (resource !== null) deleteMutation.mutate(resource);
}

function closeEditor(): void {
  editorVisible.value = false;
  editing.value = null;
  conflict.value = false;
  actionError.value = '';
}

function toggleCapability(capability: Capability, enabled: boolean): void {
  form.capabilities = enabled
    ? [...new Set([...form.capabilities, capability])]
    : form.capabilities.filter((value) => value !== capability);
}

function providerName(providerId: string): string {
  return providerRows.value.find((provider) => provider.id === providerId)?.name ?? providerId;
}

function handleError(error: unknown): void {
  conflict.value = error instanceof ApiError && error.code === 'RESOURCE_VERSION_CONFLICT';
  actionError.value = error instanceof Error ? error.message : '操作失败，请稍后重试';
}

async function refreshConfigsAndVersions(): Promise<void> {
  actionError.value = '';
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ['model-configs'] }),
    queryClient.invalidateQueries({ queryKey: ['model-config-versions'] }),
  ]);
  const targetId = versionTarget.value?.id;
  if (targetId !== undefined) {
    const refreshed = await configsQuery.refetch();
    const resource = refreshed.data?.items.find((item) => item.id === targetId);
    if (resource !== undefined) versionTarget.value = versionedTarget(resource);
  }
}

const versionRows = computed(() => [...(versionQuery.data.value?.items ?? [])]);
const diffRows = computed(() => [...(diffQuery.data.value?.changes ?? [])]);
const referenceRows = computed(() => [...(referencesQuery.data.value?.items ?? [])]);
</script>

<template>
  <section class="model-page" aria-labelledby="model-config-title">
    <header class="model-page__heading">
      <div>
        <h1 id="model-config-title">模型配置</h1>
        <p>治理模型 ID、能力、默认参数与限流，并发布不可变版本。</p>
      </div>
      <ElButton type="primary" :disabled="providerRows.length === 0" @click="openCreate">
        新增模型配置
      </ElButton>
    </header>

    <ElAlert
      v-if="providerRows.length === 0 && !providersQuery.isPending.value"
      type="warning"
      :closable="false"
      show-icon
      title="请先创建模型供应商"
    />
    <ElAlert v-if="actionError" :title="actionError" type="error" show-icon />

    <ElCard shadow="never">
      <ElInput
        v-model="keyword"
        class="model-page__search"
        clearable
        placeholder="搜索编码、名称或模型 ID"
      />
      <ElSkeleton v-if="configsQuery.isPending.value" :rows="6" animated />
      <ElAlert
        v-else-if="configsQuery.isError.value"
        title="模型配置加载失败"
        type="error"
        :closable="false"
        show-icon
      >
        <ElButton @click="configsQuery.refetch()">重新加载</ElButton>
      </ElAlert>
      <ElEmpty v-else-if="configRows.length === 0" description="暂无模型配置" />
      <ElTable v-else :data="configRows" row-key="id">
        <ElTableColumn prop="code" label="编码" min-width="150" />
        <ElTableColumn prop="name" label="名称" min-width="160" />
        <ElTableColumn label="供应商" min-width="160">
          <template #default="scope">{{ providerName(scope.row.content.provider_id) }}</template>
        </ElTableColumn>
        <ElTableColumn label="模型 ID" min-width="170">
          <template #default="scope">{{ scope.row.content.model_id }}</template>
        </ElTableColumn>
        <ElTableColumn label="能力" min-width="220">
          <template #default="scope">
            <ElSpace wrap
              ><ElTag v-for="item in scope.row.content.capabilities" :key="item">{{
                item
              }}</ElTag></ElSpace
            >
          </template>
        </ElTableColumn>
        <ElTableColumn label="状态" width="100">
          <template #default="scope"
            ><ElTag>{{ scope.row.status }}</ElTag></template
          >
        </ElTableColumn>
        <ElTableColumn label="操作" min-width="340" fixed="right">
          <template #default="scope">
            <ElSpace wrap>
              <ElButton link @click="openEdit(scope.row)">编辑</ElButton>
              <ElButton link @click="openVersions(scope.row)">版本</ElButton>
              <ElButton
                link
                :disabled="scope.row.status === 'DISABLED'"
                @click="openPublish(scope.row)"
                >发布</ElButton
              >
              <ElButton
                link
                :loading="statusMutation.isPending.value"
                @click="toggleConfig(scope.row)"
              >
                {{ scope.row.status === 'DISABLED' ? '启用' : '停用' }}
              </ElButton>
              <ElPopconfirm title="确认删除该模型配置？" @confirm="deleteConfig(scope.row)">
                <template #reference><ElButton type="danger" link>删除</ElButton></template>
              </ElPopconfirm>
            </ElSpace>
          </template>
        </ElTableColumn>
      </ElTable>
    </ElCard>

    <ElDialog
      v-model="editorVisible"
      :title="editing ? '编辑模型配置' : '新增模型配置'"
      width="min(760px, 94vw)"
    >
      <ElAlert
        v-if="conflict"
        title="模型配置已被其他人更新，请关闭后重新打开最新版本。"
        type="warning"
        show-icon
      />
      <ElForm label-position="top" @submit.prevent="saveMutation.mutate()">
        <div class="model-page__grid">
          <ElFormItem label="编码" required
            ><ElInput
              v-model="form.code"
              :disabled="editing !== null"
              placeholder="default_chat_model"
          /></ElFormItem>
          <ElFormItem label="名称" required><ElInput v-model="form.name" /></ElFormItem>
          <ElFormItem label="供应商" required>
            <select v-model="form.providerId" class="model-page__select">
              <option
                v-for="provider in providerRows"
                :key="provider.id"
                :value="provider.id"
                :disabled="provider.status === 'DISABLED'"
              >
                {{ provider.name }}（{{ provider.status }}）
              </option>
            </select>
          </ElFormItem>
          <ElFormItem label="模型 ID" required
            ><ElInput v-model="form.modelId" placeholder="gpt-5-mini"
          /></ElFormItem>
          <ElFormItem label="上下文 Token 上限"
            ><ElInputNumber v-model="form.maxContextTokens" :min="1"
          /></ElFormItem>
          <ElFormItem label="每分钟请求上限"
            ><ElInputNumber v-model="form.rateLimitRpm" :min="1"
          /></ElFormItem>
        </div>
        <ElFormItem label="模型能力">
          <ElSpace wrap>
            <ElCheckbox
              v-for="capability in capabilities"
              :key="capability"
              :model-value="form.capabilities.includes(capability)"
              @update:model-value="toggleCapability(capability, Boolean($event))"
            >
              {{ capability }}
            </ElCheckbox>
          </ElSpace>
        </ElFormItem>
        <ElFormItem label="默认参数 JSON" required
          ><ElInput v-model="form.defaultParametersText" type="textarea" :rows="7"
        /></ElFormItem>
        <ElFormItem label="描述"
          ><ElInput v-model="form.description" type="textarea" :rows="2"
        /></ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="closeEditor">取消</ElButton>
        <ElButton
          type="primary"
          :loading="saveMutation.isPending.value"
          @click="saveMutation.mutate()"
          >保存</ElButton
        >
      </template>
    </ElDialog>

    <ElDialog
      :model-value="publishTarget !== null"
      title="发布模型配置"
      width="min(560px, 92vw)"
      @update:model-value="publishTarget = null"
    >
      <ElAlert
        title="发布会冻结供应商引用、模型 ID、能力与默认参数；不会复制 Secret 明文。"
        type="info"
        :closable="false"
        show-icon
      />
      <ElFormItem label="发布说明" required class="model-page__release-note"
        ><ElInput v-model="releaseNote" type="textarea" :rows="3"
      /></ElFormItem>
      <template #footer>
        <ElButton @click="publishTarget = null">取消</ElButton>
        <ElButton
          type="primary"
          :disabled="releaseNote.trim() === ''"
          :loading="publishMutation.isPending.value"
          @click="publishMutation.mutate()"
          >确认发布</ElButton
        >
      </template>
    </ElDialog>

    <ElDialog
      :model-value="versionTarget !== null"
      title="模型配置版本与引用"
      width="min(900px, 96vw)"
      @update:model-value="versionTarget = null"
    >
      <ElSkeleton v-if="versionQuery.isPending.value" :rows="4" animated />
      <ElEmpty v-else-if="versionQuery.data.value?.items.length === 0" description="尚未发布版本" />
      <ElTable v-else :data="versionRows" row-key="id" size="small">
        <ElTableColumn prop="version_no" label="版本" width="90" />
        <ElTableColumn prop="release_note" label="发布说明" min-width="180" />
        <ElTableColumn prop="content_hash" label="内容 Hash" min-width="270" />
        <ElTableColumn label="操作" width="110">
          <template #default="scope">
            <ElPopconfirm
              title="确认从该版本生成新的回滚版本？"
              @confirm="rollbackMutation.mutate(scope.row.id)"
            >
              <template #reference><ElButton link>回滚</ElButton></template>
            </ElPopconfirm>
          </template>
        </ElTableColumn>
      </ElTable>
      <ElDivider content-position="left">最近版本 Diff</ElDivider>
      <ElEmpty v-if="diffVersionIds === null" description="至少两个版本后可查看 Diff" />
      <ElTable v-else :data="diffRows" size="small">
        <ElTableColumn prop="path" label="字段" min-width="180" />
        <ElTableColumn prop="change_type" label="变化" width="90" />
        <ElTableColumn label="内容" min-width="260"
          ><template #default="scope">{{
            `${String(scope.row.before)} → ${String(scope.row.after)}`
          }}</template></ElTableColumn
        >
      </ElTable>
      <ElDivider content-position="left">当前引用</ElDivider>
      <ElEmpty
        v-if="referencesQuery.data.value?.items.length === 0"
        description="当前没有 Agent Draft、部署、计划或会话引用"
      />
      <ElTable v-else :data="referenceRows" size="small">
        <ElTableColumn prop="reference_type" label="引用类型" />
        <ElTableColumn prop="resource_type" label="资源类型" />
        <ElTableColumn prop="resource_id" label="资源 ID" />
      </ElTable>
    </ElDialog>
  </section>
</template>

<style scoped>
.model-page {
  display: grid;
  gap: var(--ap-space-6);
}

.model-page__heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--ap-space-4);
}

.model-page__heading h1,
.model-page__heading p {
  margin: 0;
}

.model-page__heading p {
  margin-top: var(--ap-space-2);
  color: var(--ap-text-secondary);
}

.model-page__search {
  width: min(420px, 76vw);
  margin-bottom: var(--ap-space-4);
}

.model-page__grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 var(--ap-space-4);
}

.model-page__select {
  width: 100%;
  min-height: 32px;
  padding: 0 var(--ap-space-3);
  border: 1px solid var(--ap-border-color);
  border-radius: var(--ap-radius-md);
  background: var(--ap-surface-color);
  color: var(--ap-text-primary);
}

.model-page__release-note {
  margin-top: var(--ap-space-4);
}

@media (max-width: 767px) {
  .model-page__heading {
    flex-direction: column;
  }

  .model-page__grid {
    grid-template-columns: 1fr;
  }
}
</style>
