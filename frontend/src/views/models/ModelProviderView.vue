<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query';
import { computed, reactive, ref } from 'vue';

import { ApiError } from '@/api/generated';
import type { ResourceContentModelProvider } from '@/api/generated/resource-content';
import type { OperationAccepted, Resource } from '@/api/generated/resources-models';
import { modelProviderService } from '@/services/models';
import { operationService } from '@/services/operations';
import {
  ElAlert,
  ElButton,
  ElCard,
  ElDialog,
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

type ProviderType = 'openai' | 'qwen' | 'deepseek';
type ModelProviderResource = Omit<Resource, 'resource_type' | 'content'> & {
  readonly resource_type: 'model_provider';
  readonly content: ResourceContentModelProvider;
};

const queryClient = useQueryClient();
const keyword = ref('');
const editorVisible = ref(false);
const editing = ref<Resource | null>(null);
const actionError = ref('');
const conflict = ref(false);
const acceptedOperation = ref<OperationAccepted | null>(null);
const form = reactive({
  code: '',
  name: '',
  description: '',
  visibility: 'tenant' as 'private' | 'tenant',
  providerType: 'openai' as ProviderType,
  baseUrl: 'https://api.openai.com/v1',
  secretRef: '',
  timeoutSeconds: 30,
  dataRetentionPolicy: '',
});

const providersQuery = useQuery({
  queryKey: ['model-providers'],
  queryFn: () => modelProviderService.list({ limit: 200 }),
});

const providerRows = computed(() =>
  (providersQuery.data.value?.items ?? [])
    .filter(isModelProviderResource)
    .filter(
      (resource) =>
        keyword.value.trim() === '' ||
        resource.code.toLowerCase().includes(keyword.value.toLowerCase()) ||
        resource.name.toLowerCase().includes(keyword.value.toLowerCase()),
    ),
);

const operationQuery = useQuery({
  queryKey: computed(() => ['operation', acceptedOperation.value?.operation_id]),
  enabled: computed(() => acceptedOperation.value !== null),
  queryFn: ({ signal }) => {
    if (acceptedOperation.value === null) throw new Error('连接测试 Operation 尚未创建');
    return operationService.getByAccepted(acceptedOperation.value, signal);
  },
  refetchInterval: (query) => {
    const status = query.state.data?.status;
    return status === 'SUCCEEDED' || status === 'FAILED' || status === 'CANCELLED' ? false : 2000;
  },
});

const saveMutation = useMutation<Resource, Error, void>({
  mutationFn: persistProvider,
  onSuccess: async () => {
    closeEditor();
    await queryClient.invalidateQueries({ queryKey: ['model-providers'] });
  },
  onError: handleError,
});

async function persistProvider(): Promise<Resource> {
  const content: ResourceContentModelProvider = {
    resource_type: 'model_provider',
    provider_type: form.providerType,
    base_url: form.baseUrl.trim(),
    secret_ref: form.secretRef.trim(),
    timeout_seconds: form.timeoutSeconds,
    ...(form.dataRetentionPolicy.trim() === ''
      ? {}
      : { data_retention_policy: form.dataRetentionPolicy.trim() }),
  };
  const resource = editing.value;
  if (resource === null) {
    return await modelProviderService.create({
      code: form.code.trim(),
      name: form.name.trim(),
      ...(form.description.trim() === '' ? {} : { description: form.description.trim() }),
      visibility: form.visibility,
      content_schema_version: '1.0',
      content,
    });
  }
  const body = {
    name: form.name.trim(),
    description: form.description.trim(),
    visibility: form.visibility,
    content_schema_version: resource.content_schema_version,
    content,
  };
  return await modelProviderService.update(resource, body);
}

const statusMutation = useMutation({
  mutationFn: ({ resource, enabled }: { resource: Resource; enabled: boolean }) =>
    modelProviderService.setEnabled(resource, enabled),
  onSuccess: () => queryClient.invalidateQueries({ queryKey: ['model-providers'] }),
  onError: handleError,
});

const deleteMutation = useMutation({
  mutationFn: (resource: Resource) => modelProviderService.delete(resource),
  onSuccess: () => queryClient.invalidateQueries({ queryKey: ['model-providers'] }),
  onError: handleError,
});

const testMutation = useMutation({
  mutationFn: (resource: Resource) => modelProviderService.testConnection(resource.id),
  onSuccess: (operation) => {
    actionError.value = '';
    acceptedOperation.value = operation;
  },
  onError: handleError,
});

function openCreate(): void {
  editing.value = null;
  Object.assign(form, {
    code: '',
    name: '',
    description: '',
    visibility: 'tenant',
    providerType: 'openai',
    baseUrl: 'https://api.openai.com/v1',
    secretRef: '',
    timeoutSeconds: 30,
    dataRetentionPolicy: '',
  });
  conflict.value = false;
  actionError.value = '';
  editorVisible.value = true;
}

function isModelProviderResource(resource: Resource): resource is ModelProviderResource {
  return (
    resource.resource_type === 'model_provider' &&
    resource.content.resource_type === 'model_provider'
  );
}

function asModelProviderResource(value: unknown): ModelProviderResource | null {
  if (typeof value !== 'object' || value === null) return null;
  const resource = value as Resource;
  return isModelProviderResource(resource) ? resource : null;
}

function openEdit(value: unknown): void {
  const resource = asModelProviderResource(value);
  if (resource === null) return;
  editing.value = resource;
  Object.assign(form, {
    code: resource.code,
    name: resource.name,
    description: resource.description ?? '',
    visibility: resource.visibility,
    providerType: resource.content.provider_type as ProviderType,
    baseUrl: resource.content.base_url,
    secretRef: resource.content.secret_ref,
    timeoutSeconds: resource.content.timeout_seconds,
    dataRetentionPolicy: resource.content.data_retention_policy ?? '',
  });
  conflict.value = false;
  actionError.value = '';
  editorVisible.value = true;
}

function testProvider(value: unknown): void {
  const resource = asModelProviderResource(value);
  if (resource !== null) testMutation.mutate(resource);
}

function toggleProvider(value: unknown): void {
  const resource = asModelProviderResource(value);
  if (resource !== null) {
    statusMutation.mutate({ resource, enabled: resource.status === 'DISABLED' });
  }
}

function deleteProvider(value: unknown): void {
  const resource = asModelProviderResource(value);
  if (resource !== null) deleteMutation.mutate(resource);
}

function closeEditor(): void {
  editorVisible.value = false;
  editing.value = null;
  conflict.value = false;
  actionError.value = '';
}

function handleError(error: unknown): void {
  conflict.value = error instanceof ApiError && error.code === 'RESOURCE_VERSION_CONFLICT';
  actionError.value = error instanceof Error ? error.message : '操作失败，请稍后重试';
}

function maskSecretReference(secretRef: string): string {
  const segments = secretRef.split('/');
  if (segments.length < 6) return 'secret://tenant/•••';
  return `${segments.slice(0, 5).join('/')}/•••/${segments.at(-1)}`;
}
</script>

<template>
  <section class="model-page" aria-labelledby="model-provider-title">
    <header class="model-page__heading">
      <div>
        <h1 id="model-provider-title">模型供应商</h1>
        <p>管理 OpenAI、Qwen 与 DeepSeek 接入定义；平台只保存 Secret Reference。</p>
      </div>
      <ElButton type="primary" @click="openCreate">新增供应商</ElButton>
    </header>

    <ElAlert v-if="actionError" :title="actionError" type="error" show-icon />
    <ElAlert
      v-if="acceptedOperation"
      :title="`连接测试 ${operationQuery.data.value?.status ?? acceptedOperation.status}`"
      type="info"
      :closable="false"
      show-icon
    >
      Operation：{{ acceptedOperation.operation_id }}。当前阶段已可靠入队，结果由后续 Model Gateway
      Worker 完成。
    </ElAlert>

    <ElCard shadow="never">
      <ElInput
        v-model="keyword"
        class="model-page__search"
        clearable
        placeholder="搜索编码或名称"
      />
      <ElSkeleton v-if="providersQuery.isPending.value" :rows="6" animated />
      <ElAlert
        v-else-if="providersQuery.isError.value"
        title="模型供应商加载失败"
        type="error"
        :closable="false"
        show-icon
      >
        <ElButton @click="providersQuery.refetch()">重新加载</ElButton>
      </ElAlert>
      <ElEmpty v-else-if="providerRows.length === 0" description="暂无模型供应商" />
      <ElTable v-else :data="providerRows" row-key="id">
        <ElTableColumn prop="code" label="编码" min-width="150" />
        <ElTableColumn prop="name" label="名称" min-width="160" />
        <ElTableColumn label="类型" width="110">
          <template #default="scope">{{ scope.row.content.provider_type }}</template>
        </ElTableColumn>
        <ElTableColumn label="Base URL" min-width="230">
          <template #default="scope">{{ scope.row.content.base_url }}</template>
        </ElTableColumn>
        <ElTableColumn label="认证引用" min-width="230">
          <template #default="scope">{{
            maskSecretReference(scope.row.content.secret_ref)
          }}</template>
        </ElTableColumn>
        <ElTableColumn label="超时" width="90">
          <template #default="scope">{{ scope.row.content.timeout_seconds }}s</template>
        </ElTableColumn>
        <ElTableColumn label="状态" width="100">
          <template #default="scope"
            ><ElTag>{{ scope.row.status }}</ElTag></template
          >
        </ElTableColumn>
        <ElTableColumn label="操作" min-width="310" fixed="right">
          <template #default="scope">
            <ElSpace wrap>
              <ElButton link @click="openEdit(scope.row)">编辑</ElButton>
              <ElButton
                link
                :disabled="scope.row.status === 'DISABLED'"
                :loading="testMutation.isPending.value"
                @click="testProvider(scope.row)"
              >
                连通性测试
              </ElButton>
              <ElButton
                link
                :loading="statusMutation.isPending.value"
                @click="toggleProvider(scope.row)"
              >
                {{ scope.row.status === 'DISABLED' ? '启用' : '停用' }}
              </ElButton>
              <ElPopconfirm
                title="确认删除？被模型配置引用时服务端会拒绝。"
                @confirm="deleteProvider(scope.row)"
              >
                <template #reference><ElButton type="danger" link>删除</ElButton></template>
              </ElPopconfirm>
            </ElSpace>
          </template>
        </ElTableColumn>
      </ElTable>
    </ElCard>

    <ElDialog
      v-model="editorVisible"
      :title="editing ? '编辑模型供应商' : '新增模型供应商'"
      width="min(720px, 94vw)"
    >
      <ElAlert
        v-if="conflict"
        title="供应商已被其他人更新，请关闭后重新打开最新版本。"
        type="warning"
        show-icon
      />
      <ElForm label-position="top" @submit.prevent="saveMutation.mutate()">
        <div class="model-page__grid">
          <ElFormItem label="编码" required>
            <ElInput
              v-model="form.code"
              :disabled="editing !== null"
              placeholder="primary_openai"
            />
          </ElFormItem>
          <ElFormItem label="名称" required><ElInput v-model="form.name" /></ElFormItem>
          <ElFormItem label="供应商类型" required>
            <select v-model="form.providerType" class="model-page__select">
              <option value="openai">OpenAI</option>
              <option value="qwen">Qwen</option>
              <option value="deepseek">DeepSeek</option>
            </select>
          </ElFormItem>
          <ElFormItem label="可见范围">
            <select v-model="form.visibility" class="model-page__select">
              <option value="private">仅自己</option>
              <option value="tenant">租户内</option>
            </select>
          </ElFormItem>
          <ElFormItem label="Base URL" required><ElInput v-model="form.baseUrl" /></ElFormItem>
          <ElFormItem label="超时（秒）" required>
            <ElInputNumber v-model="form.timeoutSeconds" :min="1" :max="600" />
          </ElFormItem>
        </div>
        <ElFormItem label="Secret Reference" required>
          <ElInput v-model="form.secretRef" placeholder="secret://tenant/tenant_01/model/default" />
          <p class="model-page__hint">这里只填写密钥引用；不要粘贴 API Key 或 Token 明文。</p>
        </ElFormItem>
        <ElFormItem label="数据保留策略">
          <ElInput v-model="form.dataRetentionPolicy" type="textarea" :rows="2" />
        </ElFormItem>
        <ElFormItem label="描述">
          <ElInput v-model="form.description" type="textarea" :rows="2" />
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="closeEditor">取消</ElButton>
        <ElButton
          type="primary"
          :loading="saveMutation.isPending.value"
          @click="saveMutation.mutate()"
        >
          保存
        </ElButton>
      </template>
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

.model-page__heading p,
.model-page__hint {
  margin-top: var(--ap-space-2);
  color: var(--ap-text-secondary);
}

.model-page__search {
  width: min(360px, 72vw);
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

.model-page__hint {
  width: 100%;
  font-size: 13px;
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
