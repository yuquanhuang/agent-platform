<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query';
import { computed, reactive, ref } from 'vue';
import { useRouter } from 'vue-router';

import type { Agent, AgentCreateRequest } from '@/api/generated/core-models';
import { agentService } from '@/services/agents';
import {
  ElAlert,
  ElButton,
  ElCard,
  ElDialog,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
  ElSkeleton,
  ElSpace,
  ElTag,
  ElText,
} from '@/ui/element-plus';

const router = useRouter();
const queryClient = useQueryClient();
const keywordInput = ref('');
const keyword = ref('');
const statusFilter = ref('');
const cursor = ref<string | undefined>();
const cursorHistory = ref<Array<string | undefined>>([]);
const createDialogVisible = ref(false);
const copySource = ref<Agent | null>(null);
const deleteTarget = ref<Agent | null>(null);
const actionError = ref('');

const createForm = reactive({
  code: '',
  name: '',
  description: '',
  runtimeType: 'agentscope' as 'agentscope' | 'codex',
  visibility: 'private' as 'private' | 'tenant',
  tags: '',
});
const copyForm = reactive({ code: '', name: '' });

const agentsQuery = useQuery({
  queryKey: computed(() => [
    'agents',
    { keyword: keyword.value, status: statusFilter.value, cursor: cursor.value },
  ]),
  queryFn: () =>
    agentService.list({
      limit: 200,
      ...(cursor.value === undefined ? {} : { cursor: cursor.value }),
      ...(keyword.value === '' ? {} : { keyword: keyword.value }),
      ...(statusFilter.value === '' ? {} : { status: statusFilter.value }),
    }),
});
const agents = computed(() => [...(agentsQuery.data.value?.items ?? [])]);

const referencesQuery = useQuery({
  queryKey: computed(() => ['agent-references', deleteTarget.value?.id]),
  enabled: computed(() => deleteTarget.value !== null),
  queryFn: () => {
    if (deleteTarget.value === null) throw new Error('未选择待删除 Agent');
    return agentService.references(deleteTarget.value.id);
  },
});

const createMutation = useMutation({
  mutationFn: (body: AgentCreateRequest) => agentService.create(body),
  onSuccess: async (agent) => {
    createDialogVisible.value = false;
    await queryClient.invalidateQueries({ queryKey: ['agents'] });
    await router.push({ name: 'agent-edit', params: { id: agent.id } });
  },
  onError: showActionError,
});

const copyMutation = useMutation({
  mutationFn: () => {
    if (copySource.value === null) throw new Error('未选择待复制 Agent');
    return agentService.copy(copySource.value.id, copyForm);
  },
  onSuccess: async (agent) => {
    copySource.value = null;
    await queryClient.invalidateQueries({ queryKey: ['agents'] });
    await router.push({ name: 'agent-edit', params: { id: agent.id } });
  },
  onError: showActionError,
});

const disableMutation = useMutation({
  mutationFn: (agent: Agent) => agentService.disable(agent),
  onSuccess: async () => queryClient.invalidateQueries({ queryKey: ['agents'] }),
  onError: showActionError,
});

const deleteMutation = useMutation({
  mutationFn: () => {
    if (deleteTarget.value === null) throw new Error('未选择待删除 Agent');
    return agentService.delete(deleteTarget.value);
  },
  onSuccess: async () => {
    deleteTarget.value = null;
    await queryClient.invalidateQueries({ queryKey: ['agents'] });
  },
  onError: showActionError,
});

function search(): void {
  keyword.value = keywordInput.value.trim();
  cursor.value = undefined;
  cursorHistory.value = [];
}

function openCreate(): void {
  actionError.value = '';
  Object.assign(createForm, {
    code: '',
    name: '',
    description: '',
    runtimeType: 'agentscope',
    visibility: 'private',
    tags: '',
  });
  createDialogVisible.value = true;
}

function submitCreate(): void {
  actionError.value = '';
  createMutation.mutate({
    code: createForm.code.trim(),
    name: createForm.name.trim(),
    ...(createForm.description.trim() === '' ? {} : { description: createForm.description.trim() }),
    runtime_type: createForm.runtimeType,
    visibility: createForm.visibility,
    tags: parseTags(createForm.tags),
  });
}

function openCopy(agent: Agent): void {
  actionError.value = '';
  copySource.value = agent;
  copyForm.code = `${agent.code}_copy`;
  copyForm.name = `${agent.name} 副本`;
}

function openDelete(agent: Agent): void {
  actionError.value = '';
  deleteTarget.value = agent;
}

function confirmDelete(): void {
  if ((referencesQuery.data.value?.items.length ?? 0) > 0) return;
  deleteMutation.mutate();
}

function nextPage(): void {
  const nextCursor = agentsQuery.data.value?.next_cursor;
  if (nextCursor === null || nextCursor === undefined) return;
  cursorHistory.value.push(cursor.value);
  cursor.value = nextCursor;
}

function previousPage(): void {
  cursor.value = cursorHistory.value.pop();
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

function showActionError(error: unknown): void {
  actionError.value = error instanceof Error ? error.message : '操作失败，请稍后重试';
}

function formatUpdatedAt(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(
    new Date(value),
  );
}
</script>

<template>
  <section class="agent-list" aria-labelledby="agent-list-title">
    <header class="agent-list__heading">
      <div>
        <ElText id="agent-list-title" tag="h1" size="large">Agent 管理</ElText>
        <p>组合 Runtime、Prompt、模型路由和子 Agent，草稿变更不会影响已发布运行版本。</p>
      </div>
      <ElButton type="primary" @click="openCreate">新建 Agent</ElButton>
    </header>

    <ElAlert
      v-if="actionError"
      :title="actionError"
      type="error"
      show-icon
      @close="actionError = ''"
    />

    <ElCard shadow="never">
      <ElSpace class="agent-list__filters" wrap>
        <ElInput
          v-model="keywordInput"
          aria-label="搜索 Agent"
          placeholder="按名称或编码搜索"
          clearable
          @keyup.enter="search"
        />
        <select v-model="statusFilter" class="agent-list__select" aria-label="Agent 状态">
          <option value="">全部状态</option>
          <option value="DRAFT">草稿</option>
          <option value="ACTIVE">可用</option>
          <option value="DISABLED">已停用</option>
        </select>
        <ElButton :loading="agentsQuery.isFetching.value" @click="search">查询</ElButton>
      </ElSpace>
    </ElCard>

    <ElSkeleton v-if="agentsQuery.isPending.value" :rows="8" animated />
    <ElAlert
      v-else-if="agentsQuery.isError.value"
      title="Agent 列表加载失败"
      type="error"
      :closable="false"
      show-icon
    >
      <ElButton @click="agentsQuery.refetch()">重新加载</ElButton>
    </ElAlert>
    <ElEmpty v-else-if="agents.length === 0" description="暂无 Agent，可从新建草稿开始" />
    <div v-else class="agent-list__grid">
      <ElCard v-for="agent in agents" :key="agent.id" shadow="hover" class="agent-card">
        <template #header>
          <div class="agent-card__title">
            <div>
              <strong>{{ agent.name }}</strong>
              <div class="agent-card__code">{{ agent.code }}</div>
            </div>
            <ElTag
              :type="
                agent.status === 'ACTIVE'
                  ? 'success'
                  : agent.status === 'DISABLED'
                    ? 'warning'
                    : 'info'
              "
            >
              {{ agent.status }}
            </ElTag>
          </div>
        </template>
        <p class="agent-card__description">{{ agent.description || '暂无描述' }}</p>
        <ElSpace wrap>
          <ElTag effect="plain">{{ agent.runtime_type }}</ElTag>
          <ElTag effect="plain">{{ agent.visibility }}</ElTag>
          <ElTag v-for="tag in agent.tags" :key="tag" effect="plain">{{ tag }}</ElTag>
        </ElSpace>
        <dl class="agent-card__meta">
          <div>
            <dt>资源版本</dt>
            <dd>{{ agent.resource_version }}</dd>
          </div>
          <div>
            <dt>最近更新</dt>
            <dd>{{ formatUpdatedAt(agent.updated_at) }}</dd>
          </div>
        </dl>
        <ElSpace wrap>
          <ElButton
            type="primary"
            @click="router.push({ name: 'agent-edit', params: { id: agent.id } })"
            >编辑草稿</ElButton
          >
          <ElButton @click="openCopy(agent)">复制</ElButton>
          <ElButton
            v-if="agent.status !== 'DISABLED'"
            :loading="disableMutation.isPending.value"
            @click="disableMutation.mutate(agent)"
            >停用</ElButton
          >
          <ElButton type="danger" plain @click="openDelete(agent)">删除</ElButton>
        </ElSpace>
      </ElCard>
    </div>

    <ElSpace v-if="agents.length > 0" class="agent-list__pagination">
      <ElButton :disabled="cursorHistory.length === 0" @click="previousPage">上一页</ElButton>
      <ElButton :disabled="!agentsQuery.data.value?.has_more" @click="nextPage">下一页</ElButton>
    </ElSpace>

    <ElDialog v-model="createDialogVisible" title="新建 Agent" width="min(620px, 94vw)">
      <ElForm label-position="top" @submit.prevent="submitCreate">
        <ElFormItem label="编码" required>
          <ElInput v-model="createForm.code" placeholder="support_agent" />
        </ElFormItem>
        <ElFormItem label="名称" required><ElInput v-model="createForm.name" /></ElFormItem>
        <ElFormItem label="描述">
          <ElInput v-model="createForm.description" type="textarea" :rows="3" />
        </ElFormItem>
        <ElFormItem label="Runtime" required>
          <select v-model="createForm.runtimeType" class="agent-list__select">
            <option value="agentscope">AgentScope 2.0.x</option>
            <option value="codex">Codex ACP</option>
          </select>
        </ElFormItem>
        <ElFormItem label="可见范围">
          <select v-model="createForm.visibility" class="agent-list__select">
            <option value="private">仅自己</option>
            <option value="tenant">租户内</option>
          </select>
        </ElFormItem>
        <ElFormItem label="标签"
          ><ElInput v-model="createForm.tags" placeholder="客服,内部"
        /></ElFormItem>
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
      title="复制 Agent"
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

    <ElDialog
      :model-value="deleteTarget !== null"
      title="删除 Agent"
      width="min(620px, 94vw)"
      @update:model-value="deleteTarget = null"
    >
      <ElSkeleton v-if="referencesQuery.isPending.value" :rows="3" animated />
      <ElAlert
        v-else-if="referencesQuery.isError.value"
        title="引用检查失败，已阻止删除"
        type="error"
        :closable="false"
        show-icon
      />
      <ElAlert
        v-else-if="(referencesQuery.data.value?.items.length ?? 0) > 0"
        title="该 Agent 仍被引用，不能删除"
        type="warning"
        :closable="false"
        show-icon
      >
        <ul>
          <li
            v-for="reference in referencesQuery.data.value?.items"
            :key="`${reference.reference_type}:${reference.resource_id}`"
          >
            {{ reference.reference_type }}：{{ reference.resource_id }}
          </li>
        </ul>
      </ElAlert>
      <p v-else>确认删除 {{ deleteTarget?.name }}？该操作会软删除草稿并生成可查询 Operation。</p>
      <template #footer>
        <ElButton @click="deleteTarget = null">取消</ElButton>
        <ElButton
          type="danger"
          :disabled="
            referencesQuery.isPending.value ||
            referencesQuery.isError.value ||
            (referencesQuery.data.value?.items.length ?? 0) > 0
          "
          :loading="deleteMutation.isPending.value"
          @click="confirmDelete"
          >确认删除</ElButton
        >
      </template>
    </ElDialog>
  </section>
</template>

<style scoped>
.agent-list {
  display: grid;
  gap: var(--ap-space-6);
}

.agent-list__heading,
.agent-card__title {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--ap-space-4);
}

.agent-list__heading p,
.agent-card__description {
  color: var(--ap-text-secondary);
}

.agent-list__filters :deep(.el-input) {
  width: min(360px, 80vw);
}

.agent-list__select {
  min-height: 32px;
  padding: 0 var(--ap-space-3);
  border: 1px solid var(--ap-border-color);
  border-radius: var(--ap-radius-md);
  color: var(--ap-text-primary);
  background: var(--ap-surface-color);
}

.agent-list__grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(320px, 100%), 1fr));
  gap: var(--ap-space-4);
}

.agent-card__code {
  margin-top: var(--ap-space-1);
  color: var(--ap-text-secondary);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}

.agent-card__description {
  min-height: 42px;
}

.agent-card__meta {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--ap-space-3);
  margin: var(--ap-space-4) 0;
}

.agent-card__meta div {
  min-width: 0;
}

.agent-card__meta dt {
  color: var(--ap-text-secondary);
  font-size: 12px;
}

.agent-card__meta dd {
  margin: var(--ap-space-1) 0 0;
  overflow-wrap: anywhere;
}

.agent-list__pagination {
  justify-self: end;
}

@media (max-width: 767px) {
  .agent-list__heading {
    flex-direction: column;
  }

  .agent-list__pagination {
    justify-self: stretch;
  }
}
</style>
