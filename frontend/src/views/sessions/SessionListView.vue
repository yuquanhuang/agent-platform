<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query';
import { computed, onBeforeUnmount, reactive, ref } from 'vue';

import type {
  ListSessionsStatus,
  OperationAccepted,
  Session,
  SessionCreateRequest,
} from '@/api/generated/core-models';
import { operationService } from '@/services/operations';
import { sessionService } from '@/services/sessions';
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
  ElTag,
  ElText,
} from '@/ui/element-plus';

const queryClient = useQueryClient();
const agentFilterInput = ref('');
const agentFilter = ref('');
const statusFilter = ref<ListSessionsStatus | ''>('');
const cursor = ref<string | undefined>();
const cursorHistory = ref<Array<string | undefined>>([]);
const createDialogVisible = ref(false);
const renameTarget = ref<Session | null>(null);
const actionError = ref('');
const operationMessage = ref('');
const pollController = new AbortController();

const createForm = reactive({ agentId: '', title: '' });
const renameTitle = ref('');

const sessionsQuery = useQuery({
  queryKey: computed(() => [
    'sessions',
    { agentId: agentFilter.value, status: statusFilter.value, cursor: cursor.value },
  ]),
  queryFn: () =>
    sessionService.list({
      limit: 50,
      ...(cursor.value === undefined ? {} : { cursor: cursor.value }),
      ...(agentFilter.value === '' ? {} : { agentId: agentFilter.value }),
      ...(statusFilter.value === '' ? {} : { status: statusFilter.value }),
    }),
});
const sessions = computed(() => [...(sessionsQuery.data.value?.items ?? [])]);

const createMutation = useMutation({
  mutationFn: (body: SessionCreateRequest) => sessionService.create(body),
  onSuccess: async () => {
    createDialogVisible.value = false;
    await invalidateSessions();
  },
  onError: showActionError,
});

const renameMutation = useMutation({
  mutationFn: () => {
    if (renameTarget.value === null) throw new Error('未选择待重命名 Session');
    return sessionService.update(renameTarget.value, { title: renameTitle.value.trim() || null });
  },
  onSuccess: async () => {
    renameTarget.value = null;
    await invalidateSessions();
  },
  onError: showActionError,
});

const archiveMutation = useMutation({
  mutationFn: (session: Session) => sessionService.archive(session),
  onSuccess: invalidateSessions,
  onError: showActionError,
});

const deleteMutation = useMutation({
  mutationFn: (session: Session) => sessionService.delete(session),
  onSuccess: async (accepted) => {
    operationMessage.value = '删除请求已接受，正在确认结果…';
    await pollDeletion(accepted);
    await invalidateSessions();
  },
  onError: showActionError,
});

function search(): void {
  agentFilter.value = agentFilterInput.value.trim();
  cursor.value = undefined;
  cursorHistory.value = [];
}

function openCreate(): void {
  actionError.value = '';
  Object.assign(createForm, { agentId: '', title: '' });
  createDialogVisible.value = true;
}

function submitCreate(): void {
  const body: SessionCreateRequest = {
    agent_id: createForm.agentId.trim(),
    ...(createForm.title.trim() === '' ? {} : { title: createForm.title.trim() }),
  };
  createMutation.mutate(body);
}

function openRename(session: Session): void {
  actionError.value = '';
  renameTarget.value = session;
  renameTitle.value = session.title ?? '';
}

function nextPage(): void {
  const nextCursor = sessionsQuery.data.value?.next_cursor;
  if (nextCursor === null || nextCursor === undefined) return;
  cursorHistory.value.push(cursor.value);
  cursor.value = nextCursor;
}

function previousPage(): void {
  cursor.value = cursorHistory.value.pop();
}

async function invalidateSessions(): Promise<void> {
  actionError.value = '';
  await queryClient.invalidateQueries({ queryKey: ['sessions'] });
}

async function pollDeletion(accepted: OperationAccepted): Promise<void> {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const operation = await operationService.getByAccepted(accepted, pollController.signal);
    if (operation.status === 'SUCCEEDED') {
      operationMessage.value = 'Session 已进入延迟删除状态';
      return;
    }
    if (['FAILED', 'CANCELLED'].includes(operation.status)) {
      throw new Error(operation.error?.message ?? 'Session 删除失败');
    }
    await waitForPoll(pollController.signal);
  }
  throw new Error('删除状态确认超时，请稍后刷新列表');
}

function waitForPoll(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, 500);
    signal.addEventListener(
      'abort',
      () => {
        window.clearTimeout(timer);
        reject(new DOMException('Operation polling aborted', 'AbortError'));
      },
      { once: true },
    );
  });
}

function showActionError(error: unknown): void {
  if (error instanceof DOMException && error.name === 'AbortError') return;
  actionError.value = error instanceof Error ? error.message : '操作失败，请稍后重试';
}

function formatUpdatedAt(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(
    new Date(value),
  );
}

onBeforeUnmount(() => pollController.abort());
</script>

<template>
  <section class="session-list" aria-labelledby="session-list-title">
    <header class="session-list__heading">
      <div>
        <ElText id="session-list-title" tag="h1" size="large">Session 管理</ElText>
        <p>每个 Session 固定使用创建时的 Deployment，后续 Agent 发布不会改变历史会话。</p>
      </div>
      <ElButton type="primary" @click="openCreate">新建 Session</ElButton>
    </header>

    <ElAlert
      v-if="actionError"
      :title="actionError"
      type="error"
      show-icon
      @close="actionError = ''"
    />
    <ElAlert v-if="operationMessage" :title="operationMessage" type="info" show-icon />

    <ElCard shadow="never">
      <ElSpace class="session-list__filters" wrap>
        <ElInput
          v-model="agentFilterInput"
          aria-label="按 Agent ID 筛选"
          placeholder="Agent ID"
          clearable
          @keyup.enter="search"
        />
        <select v-model="statusFilter" class="session-list__select" aria-label="Session 状态">
          <option value="">活动与归档</option>
          <option value="ACTIVE">活动</option>
          <option value="ARCHIVED">已归档</option>
          <option value="DELETED">已删除</option>
        </select>
        <ElButton :loading="sessionsQuery.isFetching.value" @click="search">查询</ElButton>
      </ElSpace>
    </ElCard>

    <ElSkeleton v-if="sessionsQuery.isPending.value" :rows="8" animated />
    <ElAlert
      v-else-if="sessionsQuery.isError.value"
      title="Session 列表加载失败"
      type="error"
      :closable="false"
      show-icon
    >
      <ElButton @click="sessionsQuery.refetch()">重新加载</ElButton>
    </ElAlert>
    <ElEmpty v-else-if="sessions.length === 0" description="暂无符合条件的 Session" />
    <div v-else class="session-list__grid">
      <ElCard v-for="session in sessions" :key="session.id" shadow="hover">
        <template #header>
          <div class="session-list__card-title">
            <strong>{{ session.title || '未命名 Session' }}</strong>
            <ElTag
              :type="
                session.status === 'ACTIVE'
                  ? 'success'
                  : session.status === 'ARCHIVED'
                    ? 'warning'
                    : 'info'
              "
              >{{ session.status }}</ElTag
            >
          </div>
        </template>
        <dl class="session-list__meta">
          <div>
            <dt>Agent</dt>
            <dd>{{ session.agent_id }}</dd>
          </div>
          <div>
            <dt>固定 Deployment</dt>
            <dd>{{ session.default_deployment_id }}</dd>
          </div>
          <div>
            <dt>资源版本</dt>
            <dd>{{ session.resource_version }}</dd>
          </div>
          <div>
            <dt>最近更新</dt>
            <dd>{{ formatUpdatedAt(session.updated_at) }}</dd>
          </div>
        </dl>
        <ElSpace v-if="session.status !== 'DELETED'" wrap>
          <RouterLink
            class="session-list__messages-link"
            :to="{ name: 'session-messages', params: { id: session.id } }"
          >
            查看消息
          </RouterLink>
          <ElButton @click="openRename(session)">重命名</ElButton>
          <ElPopconfirm
            v-if="session.status === 'ACTIVE'"
            title="归档后仍可查看，但需要归档后才能删除。确认归档？"
            @confirm="archiveMutation.mutate(session)"
          >
            <template #reference><ElButton>归档</ElButton></template>
          </ElPopconfirm>
          <ElPopconfirm
            v-if="session.status === 'ARCHIVED'"
            title="删除将进入延迟删除状态，确认继续？"
            @confirm="deleteMutation.mutate(session)"
          >
            <template #reference><ElButton type="danger" plain>删除</ElButton></template>
          </ElPopconfirm>
        </ElSpace>
      </ElCard>
    </div>

    <ElSpace v-if="sessions.length > 0" class="session-list__pagination">
      <ElButton :disabled="cursorHistory.length === 0" @click="previousPage">上一页</ElButton>
      <ElButton :disabled="!sessionsQuery.data.value?.has_more" @click="nextPage">下一页</ElButton>
    </ElSpace>

    <ElDialog v-model="createDialogVisible" title="新建 Session" width="min(560px, 94vw)">
      <ElForm label-position="top" @submit.prevent="submitCreate">
        <ElFormItem label="Agent ID" required>
          <ElInput v-model="createForm.agentId" autocomplete="off" />
        </ElFormItem>
        <ElFormItem label="标题">
          <ElInput v-model="createForm.title" maxlength="200" show-word-limit />
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="createDialogVisible = false">取消</ElButton>
        <ElButton
          type="primary"
          :disabled="createForm.agentId.trim() === ''"
          :loading="createMutation.isPending.value"
          @click="submitCreate"
          >创建</ElButton
        >
      </template>
    </ElDialog>

    <ElDialog
      :model-value="renameTarget !== null"
      title="重命名 Session"
      width="min(520px, 94vw)"
      @update:model-value="renameTarget = null"
    >
      <ElInput v-model="renameTitle" maxlength="200" show-word-limit />
      <template #footer>
        <ElButton @click="renameTarget = null">取消</ElButton>
        <ElButton
          type="primary"
          :loading="renameMutation.isPending.value"
          @click="renameMutation.mutate()"
          >保存</ElButton
        >
      </template>
    </ElDialog>
  </section>
</template>

<style scoped>
.session-list {
  display: grid;
  gap: var(--ap-space-5);
}

.session-list__heading,
.session-list__card-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ap-space-4);
}

.session-list__heading p {
  margin-bottom: 0;
  color: var(--ap-text-secondary);
}

.session-list__filters {
  width: 100%;
}

.session-list__select {
  min-height: 32px;
  padding: 0 var(--ap-space-3);
  border: 1px solid var(--ap-border-color);
  border-radius: var(--ap-radius-sm);
  background: var(--ap-surface-color);
}

.session-list__grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 360px), 1fr));
  gap: var(--ap-space-4);
}

.session-list__meta {
  display: grid;
  gap: var(--ap-space-2);
}

.session-list__meta div {
  display: grid;
  grid-template-columns: 120px minmax(0, 1fr);
  gap: var(--ap-space-3);
}

.session-list__meta dt {
  color: var(--ap-text-secondary);
}

.session-list__meta dd {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
}

.session-list__pagination {
  justify-content: flex-end;
}

.session-list__messages-link {
  color: var(--el-color-primary);
  text-decoration: none;
}

@media (max-width: 767px) {
  .session-list__heading {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
