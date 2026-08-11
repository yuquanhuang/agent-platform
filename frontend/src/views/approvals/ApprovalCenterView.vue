<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query';
import { computed, reactive, ref } from 'vue';
import { useRouter } from 'vue-router';

import { ApiError } from '@/api/generated';
import type { Approval, ListApprovalsStatus } from '@/api/generated/core-models';
import { approvalService } from '@/services/approvals';
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
const statusFilter = ref<ListApprovalsStatus | ''>('PENDING');
const runFilterInput = ref('');
const runFilter = ref('');
const cursor = ref<string | undefined>();
const cursorHistory = ref<Array<string | undefined>>([]);
const decisionTarget = ref<Approval | null>(null);
const decisionKey = ref('');
const actionError = ref('');
const decisionForm = reactive<{
  decision: 'APPROVED' | 'REJECTED';
  comment: string;
}>({ decision: 'APPROVED', comment: '' });

const approvalsQuery = useQuery({
  queryKey: computed(() => [
    'approvals',
    { status: statusFilter.value, runId: runFilter.value, cursor: cursor.value },
  ]),
  queryFn: () =>
    approvalService.list({
      limit: 100,
      ...(cursor.value === undefined ? {} : { cursor: cursor.value }),
      ...(statusFilter.value === '' ? {} : { status: statusFilter.value }),
      ...(runFilter.value === '' ? {} : { runId: runFilter.value }),
    }),
  refetchInterval: 30_000,
});

const approvals = computed(() => [...(approvalsQuery.data.value?.items ?? [])]);

const decisionMutation = useMutation({
  mutationFn: () => {
    if (decisionTarget.value === null) throw new Error('未选择审批请求');
    return approvalService.decide(
      decisionTarget.value,
      {
        decision: decisionForm.decision,
        ...(decisionForm.comment?.trim() ? { comment: decisionForm.comment.trim() } : {}),
      },
      decisionKey.value,
    );
  },
  onSuccess: async () => {
    closeDecision();
    await queryClient.invalidateQueries({ queryKey: ['approvals'] });
  },
  onError: (error) => {
    actionError.value = approvalErrorMessage(error);
  },
});

function search(): void {
  runFilter.value = runFilterInput.value.trim();
  cursor.value = undefined;
  cursorHistory.value = [];
}

function openDecision(approval: Approval, decision: 'APPROVED' | 'REJECTED'): void {
  actionError.value = '';
  decisionTarget.value = approval;
  decisionForm.decision = decision;
  decisionForm.comment = '';
  decisionKey.value = createIdempotencyKey();
}

function closeDecision(): void {
  decisionTarget.value = null;
  decisionKey.value = '';
  decisionForm.comment = '';
}

function submitDecision(): void {
  actionError.value = '';
  decisionMutation.mutate();
}

function nextPage(): void {
  const nextCursor = approvalsQuery.data.value?.next_cursor;
  if (nextCursor === null || nextCursor === undefined) return;
  cursorHistory.value.push(cursor.value);
  cursor.value = nextCursor;
}

function previousPage(): void {
  cursor.value = cursorHistory.value.pop();
}

function openRun(runId: string): void {
  void router.push({ name: 'run-detail', params: { id: runId } });
}

function isExpired(approval: Approval): boolean {
  return approval.status === 'EXPIRED' || Date.parse(approval.expires_at) <= Date.now();
}

function statusType(status: Approval['status']): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'APPROVED' || status === 'CONSUMED') return 'success';
  if (status === 'REJECTED' || status === 'EXPIRED' || status === 'CANCELLED') return 'danger';
  return 'warning';
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium',
    timeStyle: 'medium',
  }).format(new Date(value));
}

function compactDigest(value: string): string {
  return value.length <= 24 ? value : `${value.slice(0, 16)}…${value.slice(-8)}`;
}

function createIdempotencyKey(): string {
  if (typeof crypto.randomUUID === 'function') return `approval-${crypto.randomUUID()}`;
  return `approval-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function approvalErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === 'RESOURCE_VERSION_CONFLICT') return '审批已被其他人更新，请刷新后重试。';
    if (error.code === 'RESOURCE_STATE_CONFLICT') return '审批已处理或已过期，请刷新列表。';
    if (error.code === 'PERMISSION_DENIED') return '当前账号不能审批该请求，可能触发了自审批阻断。';
  }
  return error instanceof Error ? error.message : '审批操作失败，请稍后重试。';
}
</script>

<template>
  <section class="approval-center" aria-labelledby="approval-center-title">
    <header class="approval-center__heading">
      <div>
        <ElText id="approval-center-title" tag="h1" size="large">审批中心</ElText>
        <p>审批只改变持久化决策；高风险工具仍需一次性执行票据才能恢复执行。</p>
      </div>
      <ElButton :loading="approvalsQuery.isFetching.value" @click="approvalsQuery.refetch()">
        刷新
      </ElButton>
    </header>

    <ElAlert
      v-if="actionError"
      :title="actionError"
      type="error"
      show-icon
      @close="actionError = ''"
    />

    <ElCard shadow="never">
      <ElSpace wrap>
        <select v-model="statusFilter" aria-label="审批状态" @change="search">
          <option value="">全部状态</option>
          <option value="PENDING">待审批</option>
          <option value="APPROVED">已批准</option>
          <option value="REJECTED">已拒绝</option>
          <option value="EXPIRED">已过期</option>
          <option value="CANCELLED">已取消</option>
          <option value="CONSUMED">已消费</option>
        </select>
        <ElInput
          v-model="runFilterInput"
          aria-label="Run ID"
          placeholder="按 Run ID 过滤"
          clearable
          @keyup.enter="search"
        />
        <ElButton @click="search">查询</ElButton>
      </ElSpace>
    </ElCard>

    <ElSkeleton v-if="approvalsQuery.isPending.value" :rows="8" animated />
    <ElAlert
      v-else-if="approvalsQuery.isError.value"
      title="审批列表加载失败"
      type="error"
      :closable="false"
      show-icon
    >
      <ElButton @click="approvalsQuery.refetch()">重新加载</ElButton>
    </ElAlert>
    <ElEmpty v-else-if="approvals.length === 0" description="暂无符合条件的审批请求" />
    <div v-else class="approval-center__grid">
      <ElCard v-for="approval in approvals" :key="approval.id" shadow="hover">
        <template #header>
          <div class="approval-card__header">
            <strong>{{ approval.tool_name }}</strong>
            <ElTag :type="statusType(approval.status)">{{ approval.status }}</ElTag>
          </div>
        </template>
        <dl class="approval-card__meta">
          <div>
            <dt>Approval</dt>
            <dd>{{ approval.id }}</dd>
          </div>
          <div>
            <dt>Run</dt>
            <dd>{{ approval.run_id }}</dd>
          </div>
          <div>
            <dt>参数摘要</dt>
            <dd>{{ compactDigest(approval.parameter_digest) }}</dd>
          </div>
          <div>
            <dt>过期时间</dt>
            <dd>{{ formatDate(approval.expires_at) }}</dd>
          </div>
        </dl>
        <ElAlert
          v-if="approval.status === 'PENDING' && isExpired(approval)"
          title="该审批已超过有效期，服务端将拒绝决策"
          type="warning"
          :closable="false"
          show-icon
        />
        <ElSpace wrap>
          <ElButton @click="openRun(approval.run_id)">查看 Run</ElButton>
          <ElButton
            type="success"
            :disabled="approval.status !== 'PENDING' || isExpired(approval)"
            @click="openDecision(approval, 'APPROVED')"
          >
            批准
          </ElButton>
          <ElButton
            type="danger"
            :disabled="approval.status !== 'PENDING' || isExpired(approval)"
            @click="openDecision(approval, 'REJECTED')"
          >
            拒绝
          </ElButton>
        </ElSpace>
      </ElCard>
    </div>

    <ElSpace class="approval-center__pagination">
      <ElButton :disabled="cursorHistory.length === 0" @click="previousPage">上一页</ElButton>
      <ElButton :disabled="!approvalsQuery.data.value?.has_more" @click="nextPage">
        下一页
      </ElButton>
    </ElSpace>

    <ElDialog
      :model-value="decisionTarget !== null"
      :title="decisionForm.decision === 'APPROVED' ? '批准审批' : '拒绝审批'"
      width="min(520px, 92vw)"
      @close="closeDecision"
    >
      <ElForm label-position="top">
        <ElFormItem label="审批说明">
          <ElInput
            v-model="decisionForm.comment"
            type="textarea"
            :rows="4"
            maxlength="2000"
            show-word-limit
          />
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton :disabled="decisionMutation.isPending.value" @click="closeDecision">
          取消
        </ElButton>
        <ElButton
          :type="decisionForm.decision === 'APPROVED' ? 'success' : 'danger'"
          :loading="decisionMutation.isPending.value"
          @click="submitDecision"
        >
          确认{{ decisionForm.decision === 'APPROVED' ? '批准' : '拒绝' }}
        </ElButton>
      </template>
    </ElDialog>
  </section>
</template>

<style scoped>
.approval-center {
  display: grid;
  gap: var(--ap-space-5);
}

.approval-center__heading,
.approval-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ap-space-4);
}

.approval-center__heading p {
  margin-bottom: 0;
  color: var(--ap-text-secondary);
}

.approval-center__grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 360px), 1fr));
  gap: var(--ap-space-4);
}

.approval-card__meta {
  display: grid;
  gap: var(--ap-space-3);
}

.approval-card__meta div {
  display: grid;
  grid-template-columns: 88px minmax(0, 1fr);
  gap: var(--ap-space-3);
}

.approval-card__meta dt {
  color: var(--ap-text-secondary);
}

.approval-card__meta dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.approval-center__pagination {
  justify-self: end;
}

@media (max-width: 767px) {
  .approval-center__heading {
    align-items: flex-start;
    flex-direction: column;
  }

  .approval-card__meta div {
    grid-template-columns: 1fr;
    gap: var(--ap-space-1);
  }
}
</style>
