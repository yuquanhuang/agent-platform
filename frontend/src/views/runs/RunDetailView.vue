<script setup lang="ts">
import { useQuery } from '@tanstack/vue-query';
import { computed, onBeforeUnmount, shallowRef, watch } from 'vue';
import { useRoute } from 'vue-router';

import { identityService } from '@/services/identity';
import {
  RunEventCoordinator,
  type RunEventCoordinatorState,
} from '@/services/run-event-coordinator';
import { runService } from '@/services/runs';
import { useRunProjectionsStore } from '@/stores/run-projections';
import {
  ElAlert,
  ElButton,
  ElCard,
  ElEmpty,
  ElSkeleton,
  ElSpace,
  ElTag,
  ElText,
} from '@/ui/element-plus';

const route = useRoute();
const runId = computed(() => String(route.params.id ?? ''));
const projectionStore = useRunProjectionsStore();
const coordinatorState = shallowRef<RunEventCoordinatorState | null>(null);
let coordinator: RunEventCoordinator | null = null;
let unsubscribe: (() => void) | null = null;

const identityQuery = useQuery({
  queryKey: ['current-identity'],
  queryFn: () => identityService.getCurrent(),
});

const runQuery = useQuery({
  queryKey: computed(() => ['run', runId.value]),
  queryFn: () => runService.get(runId.value),
  enabled: computed(() => runId.value !== ''),
});

const coordinatorContext = computed(() => {
  const run = runQuery.data.value;
  const tenantId = identityQuery.data.value?.active_tenant_id;
  if (run === undefined || tenantId === undefined || tenantId === null) return null;
  return { tenantId, sessionId: run.session_id, runId: run.id };
});

watch(
  coordinatorContext,
  (context) => {
    stopCoordinator();
    if (context !== null) startCoordinator(context);
  },
  { immediate: true },
);

onBeforeUnmount(stopCoordinator);

const projection = computed(() => coordinatorState.value?.projection ?? null);
const displayedStatus = computed(() => {
  const projected = projection.value?.status;
  return projected === undefined || projected === 'UNKNOWN'
    ? (runQuery.data.value?.status ?? 'UNKNOWN')
    : projected;
});
const messages = computed(() => Object.values(projection.value?.messages ?? {}));
const thinking = computed(() => Object.entries(projection.value?.thinkingByMessageId ?? {}));
const activePlan = computed(() => {
  const planId = projection.value?.activePlanId;
  return planId === null || planId === undefined ? null : (projection.value?.plans[planId] ?? null);
});
const toolCalls = computed(() =>
  (projection.value?.toolCallOrder ?? []).flatMap((id) => {
    const value = projection.value?.toolCalls[id];
    return value === undefined ? [] : [value];
  }),
);
const approvals = computed(() =>
  (projection.value?.approvalOrder ?? []).flatMap((id) => {
    const value = projection.value?.approvals[id];
    return value === undefined ? [] : [value];
  }),
);
const tasks = computed(() => Object.values(projection.value?.tasks ?? {}));
const artifacts = computed(() =>
  (projection.value?.artifactOrder ?? []).flatMap((id) => {
    const value = projection.value?.artifacts[id];
    return value === undefined ? [] : [value];
  }),
);

function startCoordinator(context: NonNullable<typeof coordinatorContext.value>): void {
  coordinator = new RunEventCoordinator(context, projectionStore, undefined, {
    // The authorized endpoint already redacts Thinking for callers without run:view_sensitive.
    projectionOptions: { canViewThinking: true },
  });
  unsubscribe = coordinator.subscribe((state) => {
    coordinatorState.value = state;
  });
  void coordinator.start();
}

function stopCoordinator(): void {
  unsubscribe?.();
  unsubscribe = null;
  coordinator?.stop();
  coordinator = null;
}

function reconnect(): void {
  const context = coordinatorContext.value;
  stopCoordinator();
  if (context !== null) startCoordinator(context);
}

function connectionLabel(status: RunEventCoordinatorState['status'] | undefined): string {
  const labels: Record<RunEventCoordinatorState['status'], string> = {
    idle: '待连接',
    catching_up: '补齐历史',
    connecting: '正在连接',
    live: '实时接收',
    retrying: '等待重连',
    closed: '已追平关闭',
    failed: '协议失败',
    stopped: '已停止订阅',
  };
  return status === undefined ? '初始化' : labels[status];
}

function formatDate(value: string | null | undefined): string {
  if (value === undefined || value === null) return '—';
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'medium' }).format(
    new Date(value),
  );
}
</script>

<template>
  <section class="run-detail" aria-labelledby="run-detail-title">
    <header class="run-detail__heading">
      <div>
        <ElText id="run-detail-title" tag="h1" size="large">Run 详情</ElText>
        <p>{{ runId }}</p>
      </div>
      <ElSpace wrap>
        <ElTag>{{ displayedStatus }}</ElTag>
        <ElTag type="info">{{ connectionLabel(coordinatorState?.status) }}</ElTag>
        <ElButton
          v-if="coordinatorState?.status === 'failed' || coordinatorState?.status === 'retrying'"
          @click="reconnect"
        >
          重新连接
        </ElButton>
      </ElSpace>
    </header>

    <ElSkeleton
      v-if="identityQuery.isPending.value || runQuery.isPending.value"
      :rows="8"
      animated
    />
    <ElAlert
      v-else-if="identityQuery.isError.value || runQuery.isError.value"
      title="Run 详情加载失败"
      type="error"
      :closable="false"
      show-icon
    />
    <ElAlert
      v-else-if="identityQuery.data.value?.active_tenant_id == null"
      title="当前身份尚未选择活动租户，无法建立 Run 订阅"
      type="warning"
      :closable="false"
      show-icon
    />
    <template v-else-if="runQuery.data.value">
      <ElAlert
        v-if="coordinatorState?.errorMessage"
        :title="coordinatorState.errorMessage"
        :description="coordinatorState.errorCode ?? ''"
        :type="coordinatorState.status === 'failed' ? 'error' : 'warning'"
        :closable="false"
        show-icon
      />
      <ElAlert
        v-if="projection?.gap"
        :title="`事件序号缺口：等待 ${projection.gap.expectedSequenceNo}`"
        :description="
          projection.gap.observedSequenceNo === null
            ? '历史高水位表明仍有事件尚未补齐'
            : `实时流观察到 ${projection.gap.observedSequenceNo}，正在回查 PostgreSQL 历史`
        "
        type="warning"
        :closable="false"
        show-icon
      />
      <ElAlert
        v-if="projection?.terminalConflict"
        title="检测到终态之后仍存在事件，订阅已失败关闭"
        type="error"
        :closable="false"
        show-icon
      />

      <ElCard shadow="never">
        <template #header><ElText tag="h2">概览</ElText></template>
        <dl class="run-detail__meta">
          <div>
            <dt>Session</dt>
            <dd>{{ runQuery.data.value.session_id }}</dd>
          </div>
          <div>
            <dt>Deployment</dt>
            <dd>{{ runQuery.data.value.deployment_id }}</dd>
          </div>
          <div>
            <dt>Snapshot</dt>
            <dd>{{ runQuery.data.value.snapshot_id }}</dd>
          </div>
          <div>
            <dt>已应用序号</dt>
            <dd>{{ projection?.lastSequenceNo ?? 0 }}</dd>
          </div>
          <div>
            <dt>历史高水位</dt>
            <dd>{{ projection?.knownLatestSequenceNo ?? '—' }}</dd>
          </div>
          <div>
            <dt>创建时间</dt>
            <dd>{{ formatDate(runQuery.data.value.created_at) }}</dd>
          </div>
          <div>
            <dt>开始时间</dt>
            <dd>{{ formatDate(runQuery.data.value.started_at) }}</dd>
          </div>
          <div>
            <dt>结束时间</dt>
            <dd>{{ formatDate(runQuery.data.value.finished_at) }}</dd>
          </div>
        </dl>
      </ElCard>

      <ElCard shadow="never">
        <template #header><ElText tag="h2">流式消息</ElText></template>
        <ElEmpty v-if="messages.length === 0" description="暂无消息事件" />
        <article v-for="message in messages" :key="message.messageId" class="run-detail__item">
          <div class="run-detail__item-heading">
            <ElTag>{{ message.role }}</ElTag>
            <small>{{ message.completed ? message.finishReason : 'streaming' }}</small>
          </div>
          <p class="run-detail__preserve-text">{{ message.content }}</p>
        </article>
        <details v-for="[messageId, content] in thinking" :key="messageId">
          <summary>Thinking · {{ messageId }}</summary>
          <p class="run-detail__preserve-text">{{ content }}</p>
        </details>
      </ElCard>

      <div class="run-detail__grid">
        <ElCard shadow="never">
          <template #header><ElText tag="h2">Runtime</ElText></template>
          <ElEmpty v-if="!projection?.runtime" description="尚未启动 Runtime" />
          <dl v-else class="run-detail__meta">
            <div>
              <dt>类型</dt>
              <dd>{{ projection.runtime.runtime_type }}</dd>
            </div>
            <div>
              <dt>目标</dt>
              <dd>{{ projection.runtime.runtime_target_id }}</dd>
            </div>
            <div>
              <dt>Sandbox</dt>
              <dd>{{ projection.runtime.sandbox_id ?? '—' }}</dd>
            </div>
          </dl>
        </ElCard>

        <ElCard shadow="never">
          <template #header><ElText tag="h2">计划</ElText></template>
          <ElEmpty v-if="!activePlan" description="暂无计划" />
          <ol v-else class="run-detail__list">
            <li v-for="step in activePlan.steps" :key="step.step_id">
              <ElTag size="small">{{ step.status }}</ElTag>
              <span>{{ step.title }}</span>
            </li>
          </ol>
        </ElCard>

        <ElCard shadow="never">
          <template #header><ElText tag="h2">工具调用</ElText></template>
          <ElEmpty v-if="toolCalls.length === 0" description="暂无工具调用" />
          <article
            v-for="toolCall in toolCalls"
            :key="toolCall.toolCallId"
            class="run-detail__item"
          >
            <strong>{{ toolCall.toolName ?? toolCall.toolCallId }}</strong>
            <ElTag size="small">{{ toolCall.status }}</ElTag>
            <p>{{ toolCall.argumentsSummary }}</p>
            <p>{{ toolCall.resultSummary }}</p>
          </article>
        </ElCard>

        <ElCard shadow="never">
          <template #header><ElText tag="h2">审批</ElText></template>
          <ElEmpty v-if="approvals.length === 0" description="暂无审批" />
          <article
            v-for="approval in approvals"
            :key="approval.approvalId"
            class="run-detail__item"
          >
            <strong>{{ approval.toolName ?? approval.approvalId }}</strong>
            <ElTag size="small">{{ approval.status }}</ElTag>
            <p>到期：{{ formatDate(approval.expiresAt) }}</p>
          </article>
        </ElCard>

        <ElCard shadow="never">
          <template #header><ElText tag="h2">任务进度</ElText></template>
          <ElEmpty v-if="tasks.length === 0" description="暂无任务" />
          <article v-for="task in tasks" :key="task.task_id" class="run-detail__item">
            <strong>{{ task.task_id }}</strong>
            <span>{{ task.current }} / {{ task.total }}</span>
            <p>{{ task.message }}</p>
          </article>
        </ElCard>

        <ElCard shadow="never">
          <template #header><ElText tag="h2">Artifact</ElText></template>
          <ElEmpty v-if="artifacts.length === 0" description="暂无 Artifact" />
          <article
            v-for="artifact in artifacts"
            :key="artifact.artifact_id"
            class="run-detail__item"
          >
            <strong>{{ artifact.name }}</strong>
            <span>{{ artifact.content_type }} · {{ artifact.size }} bytes</span>
          </article>
        </ElCard>
      </div>

      <ElCard shadow="never">
        <template #header><ElText tag="h2">Warning</ElText></template>
        <ElEmpty v-if="projection?.warnings.length === 0" description="暂无 Warning" />
        <ElAlert
          v-for="warning in projection?.warnings"
          :key="`${warning.code}-${warning.message}`"
          :title="warning.message"
          :description="warning.code"
          type="warning"
          :closable="false"
          show-icon
        />
      </ElCard>
    </template>
  </section>
</template>

<style scoped>
.run-detail {
  display: grid;
  gap: var(--ap-space-5);
}
.run-detail__heading,
.run-detail__item-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ap-space-3);
}
.run-detail__heading p {
  margin-bottom: 0;
  color: var(--ap-text-secondary);
  overflow-wrap: anywhere;
}
.run-detail__grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--ap-space-4);
}
.run-detail__meta {
  display: grid;
  gap: var(--ap-space-2);
}
.run-detail__meta div {
  display: grid;
  grid-template-columns: 130px minmax(0, 1fr);
  gap: var(--ap-space-3);
}
.run-detail__meta dt {
  color: var(--ap-text-secondary);
}
.run-detail__meta dd {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
}
.run-detail__item {
  display: grid;
  gap: var(--ap-space-2);
  padding: var(--ap-space-3) 0;
  border-bottom: 1px solid var(--ap-border-color);
}
.run-detail__item:last-child {
  border-bottom: 0;
}
.run-detail__item p {
  margin: 0;
}
.run-detail__preserve-text {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.run-detail__list {
  display: grid;
  gap: var(--ap-space-2);
  padding-left: var(--ap-space-5);
}
.run-detail__list li {
  display: flex;
  align-items: center;
  gap: var(--ap-space-2);
}
details summary {
  cursor: pointer;
}
@media (max-width: 767px) {
  .run-detail__heading {
    align-items: flex-start;
    flex-direction: column;
  }
  .run-detail__grid {
    grid-template-columns: 1fr;
  }
  .run-detail__meta div {
    grid-template-columns: 1fr;
    gap: var(--ap-space-1);
  }
}
</style>
