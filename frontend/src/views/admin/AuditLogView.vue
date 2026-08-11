<script setup lang="ts">
import { useQuery } from '@tanstack/vue-query';
import { computed, reactive, ref } from 'vue';

import type { AuditRecord } from '@/api/generated/resources-models';
import { auditService, type AuditListInput } from '@/services/audit';
import {
  ElAlert,
  ElButton,
  ElCard,
  ElEmpty,
  ElInput,
  ElSkeleton,
  ElSpace,
  ElTable,
  ElTableColumn,
  ElTag,
  ElText,
} from '@/ui/element-plus';

const filterInput = reactive({
  action: '',
  resourceType: '',
  actorId: '',
  runId: '',
  occurredFrom: '',
  occurredTo: '',
});
const filters = ref({ ...filterInput });
const cursor = ref<string | undefined>();
const cursorHistory = ref<Array<string | undefined>>([]);

const auditQuery = useQuery({
  queryKey: computed(() => ['audit-logs', filters.value, cursor.value]),
  queryFn: ({ signal }) => {
    const input: AuditListInput = { limit: 100, signal };
    if (cursor.value !== undefined) input.cursor = cursor.value;
    if (filters.value.action !== '') input.action = filters.value.action;
    if (filters.value.resourceType !== '') {
      input.resourceType = filters.value.resourceType;
    }
    if (filters.value.actorId !== '') input.actorId = filters.value.actorId;
    if (filters.value.runId !== '') input.runId = filters.value.runId;
    const occurredFrom = localDateTimeToIso(filters.value.occurredFrom);
    if (occurredFrom !== undefined) input.occurredFrom = occurredFrom;
    const occurredTo = localDateTimeToIso(filters.value.occurredTo);
    if (occurredTo !== undefined) input.occurredTo = occurredTo;
    return auditService.list(input);
  },
});

const rows = computed(() => [...(auditQuery.data.value?.items ?? [])]);

function search(): void {
  filters.value = {
    action: filterInput.action.trim(),
    resourceType: filterInput.resourceType.trim(),
    actorId: filterInput.actorId.trim(),
    runId: filterInput.runId.trim(),
    occurredFrom: filterInput.occurredFrom,
    occurredTo: filterInput.occurredTo,
  };
  cursor.value = undefined;
  cursorHistory.value = [];
}

function reset(): void {
  Object.assign(filterInput, {
    action: '',
    resourceType: '',
    actorId: '',
    runId: '',
    occurredFrom: '',
    occurredTo: '',
  });
  search();
}

function nextPage(): void {
  const nextCursor = auditQuery.data.value?.next_cursor;
  if (nextCursor === null || nextCursor === undefined) return;
  cursorHistory.value.push(cursor.value);
  cursor.value = nextCursor;
}

function previousPage(): void {
  cursor.value = cursorHistory.value.pop();
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium',
    timeStyle: 'medium',
  }).format(new Date(value));
}

function localDateTimeToIso(value: string): string | undefined {
  if (value === '') return undefined;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? undefined : parsed.toISOString();
}

function resultType(result: AuditRecord['result']): 'success' | 'danger' | 'warning' {
  if (result === 'SUCCESS') return 'success';
  if (result === 'DENIED') return 'warning';
  return 'danger';
}
</script>

<template>
  <section class="audit-page" aria-labelledby="audit-page-title">
    <header class="audit-page__heading">
      <div>
        <ElText id="audit-page-title" tag="h1" size="large">审计日志</ElText>
        <p>只展示冻结的最小审计投影；Secret、Ticket nonce、完整参数和原始内容不会返回。</p>
      </div>
      <ElButton :loading="auditQuery.isFetching.value" @click="auditQuery.refetch()">
        刷新
      </ElButton>
    </header>

    <ElCard shadow="never">
      <div class="audit-page__filters">
        <ElInput
          v-model="filterInput.action"
          aria-label="动作"
          placeholder="动作，例如 run.create"
        />
        <ElInput
          v-model="filterInput.resourceType"
          aria-label="资源类型"
          placeholder="资源类型，例如 run"
        />
        <ElInput v-model="filterInput.actorId" aria-label="操作者 ID" placeholder="操作者 UUID" />
        <ElInput v-model="filterInput.runId" aria-label="Run ID" placeholder="Run UUID" />
        <label class="audit-page__date-field">
          <span>开始时间</span>
          <input v-model="filterInput.occurredFrom" type="datetime-local" />
        </label>
        <label class="audit-page__date-field">
          <span>结束时间</span>
          <input v-model="filterInput.occurredTo" type="datetime-local" />
        </label>
      </div>
      <ElSpace class="audit-page__filter-actions">
        <ElButton type="primary" @click="search">查询</ElButton>
        <ElButton @click="reset">重置</ElButton>
      </ElSpace>
    </ElCard>

    <ElSkeleton v-if="auditQuery.isPending.value" :rows="8" animated />
    <ElAlert
      v-else-if="auditQuery.isError.value"
      title="审计日志加载失败"
      type="error"
      :closable="false"
      show-icon
    >
      <ElButton @click="auditQuery.refetch()">重新加载</ElButton>
    </ElAlert>
    <ElEmpty v-else-if="rows.length === 0" description="暂无符合条件的审计记录" />
    <ElCard v-else shadow="never">
      <ElTable :data="rows" row-key="event_id">
        <ElTableColumn label="时间" min-width="180">
          <template #default="scope">{{ formatDate(scope.row.occurred_at) }}</template>
        </ElTableColumn>
        <ElTableColumn prop="action" label="动作" min-width="190" />
        <ElTableColumn label="结果" width="110">
          <template #default="scope">
            <ElTag :type="resultType(scope.row.result)">{{ scope.row.result }}</ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn prop="actor_id" label="操作者" min-width="240" show-overflow-tooltip />
        <ElTableColumn label="资源" min-width="260">
          <template #default="scope">
            {{ scope.row.resource_type }} / {{ scope.row.resource_id || '—' }}
          </template>
        </ElTableColumn>
        <ElTableColumn prop="trace_id" label="Trace" min-width="220" show-overflow-tooltip />
      </ElTable>
    </ElCard>

    <ElSpace class="audit-page__pagination">
      <ElButton :disabled="cursorHistory.length === 0" @click="previousPage">上一页</ElButton>
      <ElButton :disabled="!auditQuery.data.value?.has_more" @click="nextPage">下一页</ElButton>
    </ElSpace>
  </section>
</template>

<style scoped>
.audit-page {
  display: grid;
  gap: var(--ap-space-5);
}

.audit-page__heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ap-space-4);
}

.audit-page__heading p {
  margin-bottom: 0;
  color: var(--ap-text-secondary);
}

.audit-page__filters {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr));
  gap: var(--ap-space-3);
}

.audit-page__date-field {
  display: grid;
  gap: var(--ap-space-1);
  color: var(--ap-text-secondary);
  font-size: 14px;
}

.audit-page__date-field input {
  min-height: 38px;
  padding: 0 var(--ap-space-3);
  border: 1px solid var(--ap-border-color);
  border-radius: var(--ap-radius-sm);
  background: var(--ap-surface-color);
  color: var(--ap-text-primary);
}

.audit-page__filter-actions,
.audit-page__pagination {
  margin-top: var(--ap-space-4);
}

@media (max-width: 767px) {
  .audit-page__heading {
    align-items: flex-start;
  }
}
</style>
