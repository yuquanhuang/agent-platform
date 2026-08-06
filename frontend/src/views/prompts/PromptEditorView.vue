<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query';
import { computed, reactive, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import { ApiError, type JSONValue } from '@/api/generated';
import type { ResourceContentVariable } from '@/api/generated/resource-content';
import type { Resource } from '@/api/generated/resources-models';
import { promptService } from '@/services/prompts';
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
  ElSwitch,
  ElTable,
  ElTableColumn,
  ElTag,
  ElText,
} from '@/ui/element-plus';

interface EditableVariable {
  name: string;
  type: ResourceContentVariable['type'];
  required: boolean;
  sensitive: boolean;
  defaultText: string;
  maxLength: number | null;
}

const route = useRoute();
const router = useRouter();
const queryClient = useQueryClient();
const resourceId = computed(() => String(route.params.id));
const publishDialogVisible = ref(false);
const releaseNote = ref('');
const actionError = ref('');
const conflict = ref(false);

const form = reactive({
  name: '',
  description: '',
  visibility: 'private' as 'private' | 'tenant',
  template: '',
  language: 'zh-CN',
  compilerPolicyVersion: '1',
  variables: [] as EditableVariable[],
});

const promptQuery = useQuery({
  queryKey: computed(() => ['prompt', resourceId.value]),
  queryFn: () => promptService.get(resourceId.value),
});

const versionsQuery = useQuery({
  queryKey: computed(() => ['prompt-versions', resourceId.value]),
  queryFn: () => promptService.versions(resourceId.value),
});

const referencesQuery = useQuery({
  queryKey: computed(() => ['prompt-references', resourceId.value]),
  queryFn: () => promptService.references(resourceId.value),
});

const diffVersionIds = computed(() => {
  const versions = versionsQuery.data.value?.items ?? [];
  const latest = versions[0];
  const previous = versions[1];
  return latest === undefined || previous === undefined
    ? null
    : { from: previous.id, to: latest.id };
});

const latestDiffQuery = useQuery({
  queryKey: computed(() => ['prompt-diff', resourceId.value, diffVersionIds.value]),
  enabled: computed(() => diffVersionIds.value !== null),
  queryFn: () => {
    const ids = diffVersionIds.value;
    if (ids === null) throw new Error('至少需要两个已发布版本');
    return promptService.diff(resourceId.value, ids.from, ids.to);
  },
});
const versionRows = computed(() => [...(versionsQuery.data.value?.items ?? [])]);
const diffRows = computed(() => [...(latestDiffQuery.data.value?.changes ?? [])]);
const referenceRows = computed(() => [...(referencesQuery.data.value?.items ?? [])]);

watch(
  () => promptQuery.data.value,
  (resource) => {
    if (resource === undefined || resource.content.resource_type !== 'prompt') return;
    form.name = resource.name;
    form.description = resource.description ?? '';
    form.visibility = resource.visibility;
    form.template = resource.content.template;
    form.language = resource.content.language;
    form.compilerPolicyVersion = resource.content.compiler_policy_version;
    form.variables = resource.content.variables.map((variable) => ({
      name: variable.name,
      type: variable.type,
      required: variable.required,
      sensitive: variable.sensitive,
      defaultText: variable.default === undefined ? '' : JSON.stringify(variable.default),
      maxLength: variable.max_length ?? null,
    }));
    conflict.value = false;
  },
  { immediate: true },
);

const draftChanged = computed(() => {
  const resource = promptQuery.data.value;
  if (resource === undefined || resource.content.resource_type !== 'prompt') return false;
  return (
    resource.name !== form.name ||
    (resource.description ?? '') !== form.description ||
    resource.visibility !== form.visibility ||
    JSON.stringify(resource.content) !== JSON.stringify(buildContent(false))
  );
});

const saveMutation = useMutation({
  mutationFn: () => {
    const resource = requireResource();
    return promptService.update(resource, {
      name: form.name,
      description: form.description,
      visibility: form.visibility,
      content_schema_version: resource.content_schema_version,
      content: buildContent(true),
    });
  },
  onSuccess: async () => {
    actionError.value = '';
    conflict.value = false;
    await queryClient.invalidateQueries({ queryKey: ['prompt', resourceId.value] });
  },
  onError: handleMutationError,
});

const publishMutation = useMutation({
  mutationFn: () => promptService.publish(requireResource(), releaseNote.value),
  onSuccess: refreshAfterVersionMutation,
  onError: handleMutationError,
});

const rollbackMutation = useMutation({
  mutationFn: (version: { id: string; versionNo: number }) =>
    promptService.rollback(requireResource(), version.id, `回滚到版本 ${version.versionNo}`),
  onSuccess: refreshAfterVersionMutation,
  onError: handleMutationError,
});

const statusMutation = useMutation({
  mutationFn: (enabled: boolean) => promptService.setEnabled(requireResource(), enabled),
  onSuccess: async () => {
    await queryClient.invalidateQueries({ queryKey: ['prompt', resourceId.value] });
    await queryClient.invalidateQueries({ queryKey: ['prompts'] });
  },
  onError: handleMutationError,
});

function requireResource(): Resource {
  const resource = promptQuery.data.value;
  if (resource === undefined) throw new Error('Prompt 尚未加载完成');
  return resource;
}

function buildContent(validateDefaults: boolean) {
  return {
    resource_type: 'prompt' as const,
    template: form.template,
    variables: form.variables.map((variable) => {
      const defaultValue = parseDefault(variable, validateDefaults);
      return {
        name: variable.name,
        type: variable.type,
        required: variable.required,
        sensitive: variable.sensitive,
        ...(defaultValue === undefined ? {} : { default: defaultValue }),
        ...(variable.maxLength === null ? {} : { max_length: variable.maxLength }),
      };
    }),
    language: form.language,
    compiler_policy_version: form.compilerPolicyVersion,
  };
}

function parseDefault(variable: EditableVariable, validate: boolean): JSONValue | undefined {
  if (variable.defaultText.trim() === '') return undefined;
  try {
    return JSON.parse(variable.defaultText) as JSONValue;
  } catch (error) {
    if (!validate) return variable.defaultText;
    throw new Error(`变量 ${variable.name || '未命名'} 的默认值必须是合法 JSON`, { cause: error });
  }
}

function addVariable(): void {
  form.variables.push({
    name: '',
    type: 'string',
    required: false,
    sensitive: false,
    defaultText: '',
    maxLength: null,
  });
}

function removeVariable(index: number): void {
  form.variables.splice(index, 1);
}

function openPublish(): void {
  actionError.value = '';
  releaseNote.value = '';
  publishDialogVisible.value = true;
}

async function refreshAfterVersionMutation(): Promise<void> {
  publishDialogVisible.value = false;
  releaseNote.value = '';
  actionError.value = '';
  conflict.value = false;
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ['prompt', resourceId.value] }),
    queryClient.invalidateQueries({ queryKey: ['prompt-versions', resourceId.value] }),
  ]);
}

function handleMutationError(error: unknown): void {
  conflict.value = error instanceof ApiError && error.code === 'RESOURCE_VERSION_CONFLICT';
  actionError.value = error instanceof Error ? error.message : '操作失败，请稍后重试';
}

function reloadConflict(): void {
  conflict.value = false;
  actionError.value = '';
  void promptQuery.refetch();
}
</script>

<template>
  <section class="prompt-editor" aria-labelledby="prompt-editor-title">
    <header class="prompt-editor__heading">
      <div>
        <ElButton link @click="router.push({ name: 'prompt-list' })">← 返回 Prompt 列表</ElButton>
        <ElText id="prompt-editor-title" tag="h1" size="large">
          {{ promptQuery.data.value?.name ?? 'Prompt 编辑' }}
        </ElText>
      </div>
      <ElSpace v-if="promptQuery.data.value">
        <ElTag>{{ promptQuery.data.value.status }}</ElTag>
        <ElButton
          :loading="statusMutation.isPending.value"
          @click="statusMutation.mutate(promptQuery.data.value.status === 'DISABLED')"
        >
          {{ promptQuery.data.value.status === 'DISABLED' ? '启用' : '停用' }}
        </ElButton>
        <ElButton type="primary" :disabled="!draftChanged" @click="saveMutation.mutate()">
          保存草稿
        </ElButton>
        <ElButton type="success" :disabled="draftChanged" @click="openPublish">发布</ElButton>
      </ElSpace>
    </header>

    <ElSkeleton v-if="promptQuery.isPending.value" :rows="10" animated />
    <ElAlert
      v-else-if="promptQuery.isError.value"
      title="Prompt 加载失败"
      type="error"
      :closable="false"
      show-icon
    >
      <ElButton @click="promptQuery.refetch()">重新加载</ElButton>
    </ElAlert>

    <template v-else-if="promptQuery.data.value">
      <ElAlert
        v-if="conflict"
        title="该 Prompt 已被其他人更新，当前修改未覆盖服务端内容。"
        type="warning"
        :closable="false"
        show-icon
      >
        <ElButton @click="reloadConflict">重新加载最新版本</ElButton>
      </ElAlert>
      <ElAlert v-else-if="actionError" :title="actionError" type="error" show-icon />

      <ElCard shadow="never">
        <template #header><strong>基本信息与模板</strong></template>
        <ElForm label-position="top">
          <div class="prompt-editor__metadata-grid">
            <ElFormItem label="名称" required><ElInput v-model="form.name" /></ElFormItem>
            <ElFormItem label="可见范围">
              <select v-model="form.visibility" class="prompt-editor__select">
                <option value="private">仅自己</option>
                <option value="tenant">租户内</option>
              </select>
            </ElFormItem>
            <ElFormItem label="语言"><ElInput v-model="form.language" /></ElFormItem>
            <ElFormItem label="编译策略版本">
              <ElInput v-model="form.compilerPolicyVersion" />
            </ElFormItem>
          </div>
          <ElFormItem label="描述">
            <ElInput v-model="form.description" type="textarea" :rows="2" />
          </ElFormItem>
          <ElFormItem label="Prompt 模板" required>
            <ElInput v-model="form.template" type="textarea" :rows="12" />
          </ElFormItem>
        </ElForm>
      </ElCard>

      <ElCard shadow="never">
        <template #header>
          <div class="prompt-editor__card-heading">
            <strong>变量 Schema</strong>
            <ElButton @click="addVariable">新增变量</ElButton>
          </div>
        </template>
        <ElEmpty v-if="form.variables.length === 0" description="当前模板未声明变量" />
        <div v-else class="prompt-editor__variables">
          <div v-for="(variable, index) in form.variables" :key="index" class="variable-row">
            <ElInput v-model="variable.name" aria-label="变量名" placeholder="变量名" />
            <select v-model="variable.type" aria-label="变量类型" class="prompt-editor__select">
              <option
                v-for="type in ['string', 'integer', 'number', 'boolean', 'object', 'array']"
                :key="type"
                :value="type"
              >
                {{ type }}
              </option>
            </select>
            <ElInput
              v-model="variable.defaultText"
              aria-label="默认值 JSON"
              placeholder="默认值（JSON）"
            />
            <ElInputNumber
              v-model="variable.maxLength"
              aria-label="最大长度"
              :min="1"
              :max="100000"
              placeholder="最大长度"
            />
            <ElCheckbox v-model="variable.required">必填</ElCheckbox>
            <ElSwitch v-model="variable.sensitive" active-text="敏感" />
            <ElButton type="danger" link @click="removeVariable(index)">移除</ElButton>
          </div>
        </div>
        <p class="prompt-editor__hint">
          敏感变量默认值只保存在当前内存表单与受治理资源中，不写入本地存储、日志或审计详情。
        </p>
      </ElCard>

      <ElCard shadow="never">
        <template #header><strong>已发布版本</strong></template>
        <ElSkeleton v-if="versionsQuery.isPending.value" :rows="3" animated />
        <ElEmpty
          v-else-if="versionsQuery.data.value?.items.length === 0"
          description="尚未发布版本"
        />
        <ElTable v-else :data="versionRows" row-key="id">
          <ElTableColumn prop="version_no" label="版本" width="90" />
          <ElTableColumn prop="release_note" label="发布说明" min-width="200" />
          <ElTableColumn prop="content_hash" label="内容 Hash" min-width="260" />
          <ElTableColumn label="操作" width="120">
            <template #default="scope">
              <ElPopconfirm
                title="确认从该版本生成新的回滚版本？"
                @confirm="
                  rollbackMutation.mutate({ id: scope.row.id, versionNo: scope.row.version_no })
                "
              >
                <template #reference><ElButton link>回滚到此版本</ElButton></template>
              </ElPopconfirm>
            </template>
          </ElTableColumn>
        </ElTable>
      </ElCard>
    </template>

    <ElDialog v-model="publishDialogVisible" title="发布前确认" width="min(760px, 94vw)">
      <ElAlert
        :title="draftChanged ? '请先保存草稿后再发布' : '草稿已保存，可发布新版本'"
        :type="draftChanged ? 'warning' : 'success'"
        :closable="false"
        show-icon
      />
      <ElDivider content-position="left">版本 Diff</ElDivider>
      <ElEmpty v-if="diffVersionIds === null" description="已发布版本不足两个，暂无历史 Diff" />
      <ElTable v-else :data="diffRows" size="small">
        <ElTableColumn prop="path" label="字段" min-width="180" />
        <ElTableColumn prop="change_type" label="变化" width="90" />
        <ElTableColumn label="内容" min-width="260">
          <template #default="scope">
            {{
              scope.row.sensitive
                ? '敏感内容已脱敏'
                : `${String(scope.row.before)} → ${String(scope.row.after)}`
            }}
          </template>
        </ElTableColumn>
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
      <ElFormItem label="发布说明" required class="prompt-editor__release-note">
        <ElInput v-model="releaseNote" type="textarea" :rows="3" />
      </ElFormItem>
      <template #footer>
        <ElButton @click="publishDialogVisible = false">取消</ElButton>
        <ElButton
          type="primary"
          :disabled="draftChanged || releaseNote.trim() === ''"
          :loading="publishMutation.isPending.value"
          @click="publishMutation.mutate()"
        >
          确认发布
        </ElButton>
      </template>
    </ElDialog>
  </section>
</template>

<style scoped>
.prompt-editor {
  display: grid;
  gap: var(--ap-space-5);
}

.prompt-editor__heading,
.prompt-editor__card-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--ap-space-4);
}

.prompt-editor__heading h1 {
  margin: var(--ap-space-2) 0 0;
}

.prompt-editor__metadata-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 var(--ap-space-4);
}

.prompt-editor__variables {
  display: grid;
  gap: var(--ap-space-3);
  overflow-x: auto;
}

.prompt-editor__select {
  width: 100%;
  min-height: 32px;
  padding: 0 var(--ap-space-3);
  border: 1px solid var(--ap-border-color);
  border-radius: var(--ap-radius-md);
  background: var(--ap-surface-color);
  color: var(--ap-text-primary);
}

.variable-row {
  display: grid;
  grid-template-columns: minmax(140px, 1fr) 130px minmax(180px, 1.4fr) 150px auto auto auto;
  align-items: center;
  gap: var(--ap-space-3);
  min-width: 980px;
}

.prompt-editor__hint {
  margin: var(--ap-space-3) 0 0;
  color: var(--ap-text-secondary);
  font-size: 13px;
}

.prompt-editor__release-note {
  margin-top: var(--ap-space-5);
}

@media (max-width: 767px) {
  .prompt-editor__heading {
    flex-direction: column;
  }

  .prompt-editor__metadata-grid {
    grid-template-columns: 1fr;
  }
}
</style>
