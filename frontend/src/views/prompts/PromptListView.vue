<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query';
import { computed, reactive, ref } from 'vue';
import { useRouter } from 'vue-router';

import type { PromptCreateRequest, Resource } from '@/api/generated/resources-models';
import { promptService } from '@/services/prompts';
import {
  ElAlert,
  ElButton,
  ElCard,
  ElDialog,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
  ElPopconfirm,
  ElSkeleton,
  ElSpace,
  ElTable,
  ElTableColumn,
  ElTag,
  ElText,
} from '@/ui/element-plus';

const router = useRouter();
const queryClient = useQueryClient();
const keywordInput = ref('');
const keyword = ref('');
const createDialogVisible = ref(false);
const copySource = ref<Resource | null>(null);
const actionError = ref('');

const createForm = reactive({
  code: '',
  name: '',
  description: '',
  visibility: 'private' as 'private' | 'tenant',
});
const copyForm = reactive({ code: '', name: '' });

const promptsQuery = useQuery({
  queryKey: computed(() => ['prompts', { keyword: keyword.value }]),
  queryFn: () =>
    promptService.list({
      limit: 100,
      ...(keyword.value === '' ? {} : { keyword: keyword.value }),
    }),
});
const promptRows = computed(() => [...(promptsQuery.data.value?.items ?? [])]);

const createMutation = useMutation({
  mutationFn: (body: PromptCreateRequest) => promptService.create(body),
  onSuccess: async (resource) => {
    createDialogVisible.value = false;
    await queryClient.invalidateQueries({ queryKey: ['prompts'] });
    await router.push({ name: 'prompt-edit', params: { id: resource.id } });
  },
  onError: showActionError,
});

const copyMutation = useMutation({
  mutationFn: () => {
    if (copySource.value === null) throw new Error('未选择待复制的 Prompt');
    return promptService.copy(copySource.value.id, copyForm);
  },
  onSuccess: async (resource) => {
    copySource.value = null;
    await queryClient.invalidateQueries({ queryKey: ['prompts'] });
    await router.push({ name: 'prompt-edit', params: { id: resource.id } });
  },
  onError: showActionError,
});

const deleteMutation = useMutation({
  mutationFn: (resource: Resource) => promptService.delete(resource),
  onSuccess: async () => {
    await queryClient.invalidateQueries({ queryKey: ['prompts'] });
  },
  onError: showActionError,
});

function search(): void {
  keyword.value = keywordInput.value.trim();
}

function openCreate(): void {
  actionError.value = '';
  Object.assign(createForm, { code: '', name: '', description: '', visibility: 'private' });
  createDialogVisible.value = true;
}

function submitCreate(): void {
  actionError.value = '';
  createMutation.mutate({
    code: createForm.code,
    name: createForm.name,
    ...(createForm.description === '' ? {} : { description: createForm.description }),
    visibility: createForm.visibility,
    content_schema_version: '1.0',
    content: {
      resource_type: 'prompt',
      template: '请在这里编写 Prompt 模板',
      variables: [],
      language: 'zh-CN',
      compiler_policy_version: '1',
    },
  });
}

function openCopy(resource: Resource): void {
  actionError.value = '';
  copySource.value = resource;
  copyForm.code = `${resource.code}_copy`;
  copyForm.name = `${resource.name} 副本`;
}

function showActionError(error: unknown): void {
  actionError.value = error instanceof Error ? error.message : '操作失败，请稍后重试';
}

function promptRow(value: unknown): Resource {
  return value as Resource;
}

function retry(): void {
  void promptsQuery.refetch();
}
</script>

<template>
  <section class="prompt-list" aria-labelledby="prompt-list-title">
    <header class="prompt-list__heading">
      <div>
        <ElText id="prompt-list-title" tag="h1" size="large">Prompt 管理</ElText>
        <p>统一管理模板、变量 Schema、版本与发布状态。</p>
      </div>
      <ElButton type="primary" @click="openCreate">新建 Prompt</ElButton>
    </header>

    <ElAlert
      v-if="actionError"
      :title="actionError"
      type="error"
      :closable="true"
      show-icon
      @close="actionError = ''"
    />

    <ElCard shadow="never">
      <ElSpace class="prompt-list__filters" wrap>
        <ElInput
          v-model="keywordInput"
          aria-label="搜索 Prompt"
          placeholder="按名称或编码搜索"
          clearable
          @keyup.enter="search"
        />
        <ElButton :loading="promptsQuery.isFetching.value" @click="search">搜索</ElButton>
      </ElSpace>

      <ElSkeleton v-if="promptsQuery.isPending.value" :rows="6" animated />
      <ElAlert
        v-else-if="promptsQuery.isError.value"
        title="Prompt 列表加载失败"
        type="error"
        :closable="false"
        show-icon
      >
        <ElButton @click="retry">重新加载</ElButton>
      </ElAlert>
      <ElEmpty
        v-else-if="promptsQuery.data.value?.items.length === 0"
        description="暂无 Prompt，可从新建开始"
      />
      <ElTable v-else :data="promptRows" row-key="id">
        <ElTableColumn prop="name" label="名称" min-width="180" />
        <ElTableColumn prop="code" label="编码" min-width="160" />
        <ElTableColumn label="状态" width="110">
          <template #default="scope">
            <ElTag :type="scope.row.status === 'ACTIVE' ? 'success' : 'info'">
              {{ scope.row.status }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn prop="resource_version" label="资源版本" width="110" />
        <ElTableColumn label="操作" width="250" fixed="right">
          <template #default="scope">
            <ElSpace>
              <ElButton
                link
                type="primary"
                @click="router.push({ name: 'prompt-edit', params: { id: scope.row.id } })"
              >
                编辑
              </ElButton>
              <ElButton link @click="openCopy(promptRow(scope.row))">复制</ElButton>
              <ElPopconfirm
                title="确认删除该 Prompt？已发布版本仍保留审计事实。"
                @confirm="deleteMutation.mutate(promptRow(scope.row))"
              >
                <template #reference>
                  <ElButton link type="danger">删除</ElButton>
                </template>
              </ElPopconfirm>
            </ElSpace>
          </template>
        </ElTableColumn>
      </ElTable>
    </ElCard>

    <ElDialog v-model="createDialogVisible" title="新建 Prompt" width="min(560px, 92vw)">
      <ElForm label-position="top" @submit.prevent="submitCreate">
        <ElFormItem label="编码" required>
          <ElInput v-model="createForm.code" placeholder="welcome_prompt" />
        </ElFormItem>
        <ElFormItem label="名称" required>
          <ElInput v-model="createForm.name" />
        </ElFormItem>
        <ElFormItem label="描述">
          <ElInput v-model="createForm.description" type="textarea" :rows="3" />
        </ElFormItem>
        <ElFormItem label="可见范围">
          <select v-model="createForm.visibility" class="prompt-list__select">
            <option value="private">仅自己</option>
            <option value="tenant">租户内</option>
          </select>
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="createDialogVisible = false">取消</ElButton>
        <ElButton type="primary" :loading="createMutation.isPending.value" @click="submitCreate">
          创建并编辑
        </ElButton>
      </template>
    </ElDialog>

    <ElDialog
      :model-value="copySource !== null"
      title="复制 Prompt"
      width="min(520px, 92vw)"
      @update:model-value="copySource = null"
    >
      <ElForm label-position="top" @submit.prevent="copyMutation.mutate()">
        <ElFormItem label="新编码" required><ElInput v-model="copyForm.code" /></ElFormItem>
        <ElFormItem label="新名称" required><ElInput v-model="copyForm.name" /></ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="copySource = null">取消</ElButton>
        <ElButton
          type="primary"
          :loading="copyMutation.isPending.value"
          @click="copyMutation.mutate()"
        >
          复制
        </ElButton>
      </template>
    </ElDialog>
  </section>
</template>

<style scoped>
.prompt-list {
  display: grid;
  gap: var(--ap-space-5);
}

.prompt-list__heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--ap-space-4);
}

.prompt-list__heading h1,
.prompt-list__heading p {
  margin: 0;
}

.prompt-list__heading p {
  margin-top: var(--ap-space-2);
  color: var(--ap-text-secondary);
}

.prompt-list__filters {
  margin-bottom: var(--ap-space-4);
}

.prompt-list__filters :deep(.el-input) {
  width: min(360px, 72vw);
}

.prompt-list__select {
  width: 100%;
  min-height: 32px;
  padding: 0 var(--ap-space-3);
  border: 1px solid var(--ap-border-color);
  border-radius: var(--ap-radius-md);
  background: var(--ap-surface-color);
  color: var(--ap-text-primary);
}

@media (max-width: 767px) {
  .prompt-list__heading {
    flex-direction: column;
  }
}
</style>
