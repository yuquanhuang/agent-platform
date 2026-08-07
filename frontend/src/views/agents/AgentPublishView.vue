<script setup lang="ts">
import { useMutation, useQuery } from '@tanstack/vue-query';
import { computed, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import type { AgentVersion, SnapshotDiffChangesItem } from '@/api/generated/core-models';
import { agentService } from '@/services/agents';
import {
  TERMINAL_RELEASE_STATUSES,
  normalizeRuntimeTargets,
  releaseService,
} from '@/services/releases';
import {
  ElAlert,
  ElButton,
  ElCard,
  ElCheckbox,
  ElDivider,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
  ElPopconfirm,
  ElSkeleton,
  ElSpace,
  ElSwitch,
  ElTable,
  ElTableColumn,
  ElTag,
  ElText,
} from '@/ui/element-plus';

const route = useRoute();
const router = useRouter();
const agentId = computed(() => String(route.params.id));
const runtimeTargetsInput = ref('rt_agentscope_default');
const releaseNote = ref('');
const runSmokeTest = ref(true);
const activateOnSuccess = ref(true);
const previewedVersion = ref<number | null>(null);
const previewedTargetsSignature = ref('');
const releaseId = ref('');
const releaseAction = ref<'发布' | '回滚'>('发布');
const selectedVersionId = ref('');
const fromVersionId = ref('');
const toVersionId = ref('');
const loadedVersions = ref<AgentVersion[]>([]);
const nextVersionCursor = ref<string | null>(null);

const runtimeTargets = computed(() => normalizeRuntimeTargets(runtimeTargetsInput.value));
const runtimeTargetsSignature = computed(() => JSON.stringify(runtimeTargets.value));
const agentQuery = useQuery({
  queryKey: computed(() => ['agent', agentId.value]),
  queryFn: () => agentService.get(agentId.value),
});
const versionsQuery = useQuery({
  queryKey: computed(() => ['agent-versions', agentId.value]),
  queryFn: ({ signal }) => releaseService.listVersions(agentId.value, { limit: 20 }, signal),
});

watch(
  () => versionsQuery.data.value,
  (page) => {
    if (page === undefined) return;
    loadedVersions.value = [...page.items];
    nextVersionCursor.value = page.next_cursor ?? null;
  },
  { immediate: true },
);

const previewMutation = useMutation({
  mutationFn: async () => {
    const agent = agentQuery.data.value;
    if (agent === undefined) throw new Error('Agent 尚未加载');
    if (runtimeTargets.value.length === 0) throw new Error('至少配置一个 Runtime Target');
    return releaseService.preview(agent.id, {
      expected_agent_version: agent.resource_version,
      runtime_targets: runtimeTargets.value,
    });
  },
  onSuccess: () => {
    previewedVersion.value = agentQuery.data.value?.resource_version ?? null;
    previewedTargetsSignature.value = runtimeTargetsSignature.value;
  },
});

const publishMutation = useMutation({
  mutationFn: async () => {
    const agent = agentQuery.data.value;
    if (agent === undefined) throw new Error('Agent 尚未加载');
    if (!previewCurrent.value) throw new Error('发布前必须基于当前 Draft 重新预览');
    if (releaseNote.value.trim() === '') throw new Error('请输入发布说明');
    return releaseService.publish(agent.id, {
      expected_agent_version: agent.resource_version,
      runtime_targets: runtimeTargets.value,
      release_note: releaseNote.value.trim(),
      run_smoke_test: runSmokeTest.value,
      activate_on_success: activateOnSuccess.value,
    });
  },
  onSuccess: (accepted) => {
    releaseAction.value = '发布';
    releaseId.value = accepted.release_id;
  },
});

const rollbackMutation = useMutation({
  mutationFn: (version: AgentVersion) => {
    if (runtimeTargets.value.length === 0) throw new Error('至少配置一个 Runtime Target');
    return releaseService.rollback(agentId.value, {
      snapshot_id: version.snapshot_id,
      runtime_targets: runtimeTargets.value,
      release_note: `回滚到 Agent v${version.version_no}`,
    });
  },
  onSuccess: (accepted) => {
    releaseAction.value = '回滚';
    releaseId.value = accepted.release_id;
  },
});

const releaseQuery = useQuery({
  queryKey: computed(() => ['release', releaseId.value]),
  enabled: computed(() => releaseId.value !== ''),
  queryFn: ({ signal }) => releaseService.get(releaseId.value, signal),
  refetchInterval: (query) => {
    const status = query.state.data?.status;
    return status !== undefined && TERMINAL_RELEASE_STATUSES.has(status) ? false : 1000;
  },
});
const deploymentIds = computed(() => releaseQuery.data.value?.deployment_ids ?? []);
const deploymentsQuery = useQuery({
  queryKey: computed(() => ['release-deployments', deploymentIds.value]),
  enabled: computed(
    () => releaseQuery.data.value?.status === 'SUCCEEDED' && deploymentIds.value.length > 0,
  ),
  queryFn: ({ signal }) =>
    Promise.all(deploymentIds.value.map((id) => releaseService.getDeployment(id, signal))),
});

const selectedVersionQuery = useQuery({
  queryKey: computed(() => ['agent-version', agentId.value, selectedVersionId.value]),
  enabled: computed(() => selectedVersionId.value !== ''),
  queryFn: ({ signal }) =>
    releaseService.getVersion(agentId.value, selectedVersionId.value, signal),
});
const selectedFromVersion = computed(() =>
  loadedVersions.value.find((item) => item.id === fromVersionId.value),
);
const selectedToVersion = computed(() =>
  loadedVersions.value.find((item) => item.id === toVersionId.value),
);
const historyDiffQuery = useQuery({
  queryKey: computed(() => [
    'agent-version-diff',
    agentId.value,
    selectedFromVersion.value?.snapshot_id,
    selectedToVersion.value?.snapshot_id,
  ]),
  enabled: computed(
    () =>
      selectedFromVersion.value !== undefined &&
      selectedToVersion.value !== undefined &&
      selectedFromVersion.value.id !== selectedToVersion.value.id,
  ),
  queryFn: ({ signal }) =>
    releaseService.diff(
      agentId.value,
      selectedFromVersion.value?.snapshot_id ?? '',
      selectedToVersion.value?.snapshot_id ?? '',
      signal,
    ),
});
const loadMoreMutation = useMutation({
  mutationFn: () =>
    releaseService.listVersions(agentId.value, {
      limit: 20,
      ...(nextVersionCursor.value === null ? {} : { cursor: nextVersionCursor.value }),
    }),
  onSuccess: (page) => {
    const known = new Set(loadedVersions.value.map((item) => item.id));
    loadedVersions.value.push(...page.items.filter((item) => !known.has(item.id)));
    nextVersionCursor.value = page.next_cursor ?? null;
  },
});

const previewCurrent = computed(
  () =>
    previewMutation.data.value !== undefined &&
    previewedVersion.value === agentQuery.data.value?.resource_version &&
    previewedTargetsSignature.value === runtimeTargetsSignature.value,
);
const releaseInProgress = computed(() => {
  const status = releaseQuery.data.value?.status;
  return releaseId.value !== '' && (status === undefined || !TERMINAL_RELEASE_STATUSES.has(status));
});
const publishDisabled = computed(
  () =>
    !previewCurrent.value ||
    previewMutation.data.value?.ready_to_publish !== true ||
    releaseNote.value.trim() === '' ||
    publishMutation.isPending.value ||
    releaseInProgress.value,
);
const previewBlockers = computed(() => {
  const preview = previewMutation.data.value;
  if (preview === undefined || preview.ready_to_publish) return [];
  const bindingTypes = new Set(preview.resolved_bindings.map((item) => item.resource_type));
  const blockers: string[] = [];
  if (!bindingTypes.has('sandbox')) blockers.push('缺少已发布 Sandbox Profile 绑定');
  if (agentQuery.data.value?.runtime_type === 'agentscope' && !bindingTypes.has('model')) {
    blockers.push('AgentScope 缺少已发布 ModelConfig 绑定');
  }
  if (blockers.length === 0) blockers.push('发布前置校验未满足，请检查部署配置');
  return blockers;
});
const resolvedBindings = computed(() => [...(previewMutation.data.value?.resolved_bindings ?? [])]);

watch(runtimeTargetsSignature, () => {
  if (previewedTargetsSignature.value !== runtimeTargetsSignature.value) {
    previewedVersion.value = null;
  }
});

function groupedChanges(changes: ReadonlyArray<SnapshotDiffChangesItem>) {
  const groups = new Map<string, SnapshotDiffChangesItem[]>();
  for (const change of changes) {
    const items = groups.get(change.category) ?? [];
    items.push(change);
    groups.set(change.category, items);
  }
  return [...groups.entries()].sort(([left], [right]) => left.localeCompare(right));
}

function displayValue(value: unknown): string {
  if (value === undefined) return '—';
  return typeof value === 'string' ? value : JSON.stringify(value);
}

function rollbackVersionAt(index: number): void {
  const version = loadedVersions.value[index];
  if (version === undefined) return;
  rollbackMutation.mutate(version);
}
</script>

<template>
  <section class="agent-publish" aria-labelledby="agent-publish-title">
    <header class="agent-publish__heading">
      <div>
        <ElText id="agent-publish-title" tag="h1" size="large">版本与发布</ElText>
        <p>预览只读取当前 Draft；正式发布会重新编译并执行版本并发校验。</p>
      </div>
      <ElSpace>
        <ElButton @click="router.push({ name: 'agent-edit', params: { id: agentId } })">
          返回编辑
        </ElButton>
        <ElButton @click="router.push({ name: 'agent-list' })">返回列表</ElButton>
      </ElSpace>
    </header>

    <ElSkeleton v-if="agentQuery.isPending.value" :rows="8" animated />
    <ElAlert
      v-else-if="agentQuery.isError.value"
      title="Agent Draft 加载失败，无法安全发布"
      type="error"
      :closable="false"
      show-icon
    >
      <ElButton @click="agentQuery.refetch()">重新加载</ElButton>
    </ElAlert>
    <template v-else-if="agentQuery.data.value">
      <ElCard shadow="never">
        <div class="agent-publish__summary">
          <div>
            <strong>{{ agentQuery.data.value.name }}</strong> · {{ agentQuery.data.value.code }}
          </div>
          <ElSpace>
            <ElTag>{{ agentQuery.data.value.runtime_type }}</ElTag>
            <ElTag type="info">Draft rv {{ agentQuery.data.value.resource_version }}</ElTag>
            <ElTag :type="previewCurrent ? 'success' : 'warning'">
              {{ previewCurrent ? '预览有效' : '需要预览' }}
            </ElTag>
          </ElSpace>
        </div>
      </ElCard>

      <ElCard shadow="never" class="agent-publish__panel">
        <ElText tag="h2">发布配置</ElText>
        <ElForm label-position="top">
          <ElFormItem label="Runtime Target ID（多个使用逗号分隔）" required>
            <ElInput v-model="runtimeTargetsInput" placeholder="rt_agentscope_default" />
          </ElFormItem>
          <ElFormItem label="发布说明" required>
            <ElInput
              v-model="releaseNote"
              type="textarea"
              :rows="3"
              maxlength="2000"
              show-word-limit
            />
          </ElFormItem>
          <div class="agent-publish__options">
            <ElCheckbox v-model="runSmokeTest">执行 Smoke Test</ElCheckbox>
            <span>成功后激活</span>
            <ElSwitch v-model="activateOnSuccess" />
          </div>
        </ElForm>
        <ElAlert
          v-if="previewMutation.isError.value"
          :title="
            previewMutation.error.value instanceof Error
              ? previewMutation.error.value.message
              : '发布预览失败'
          "
          type="error"
          :closable="false"
          show-icon
        />
        <ElSpace>
          <ElButton
            :loading="previewMutation.isPending.value"
            :disabled="runtimeTargets.length === 0 || releaseInProgress"
            @click="previewMutation.mutate()"
          >
            {{ previewCurrent ? '重新预览' : '生成发布预览' }}
          </ElButton>
          <ElButton
            type="primary"
            :loading="publishMutation.isPending.value"
            :disabled="publishDisabled"
            @click="publishMutation.mutate()"
          >
            提交发布
          </ElButton>
        </ElSpace>
        <ElAlert
          v-if="publishMutation.isError.value"
          :title="
            publishMutation.error.value instanceof Error
              ? publishMutation.error.value.message
              : '发布请求失败'
          "
          type="error"
          :closable="false"
          show-icon
        />
      </ElCard>

      <ElCard v-if="previewMutation.data.value" shadow="never" class="agent-publish__panel">
        <ElText tag="h2">解析结果与脱敏 Diff</ElText>
        <ElAlert
          v-if="!previewMutation.data.value.ready_to_publish"
          :title="`当前配置不可发布：${previewBlockers.join('；')}`"
          type="warning"
          :closable="false"
          show-icon
        />
        <p class="agent-publish__hash">
          Preview Snapshot: {{ previewMutation.data.value.preview_snapshot_hash }}
        </p>
        <ElTable :data="resolvedBindings" empty-text="没有资源绑定">
          <ElTableColumn prop="resource_type" label="类型" width="120" />
          <ElTableColumn prop="binding_role" label="角色" width="120" />
          <ElTableColumn prop="resource_id" label="资源 ID" min-width="220" />
          <ElTableColumn prop="version_no" label="版本" width="90" />
          <ElTableColumn prop="content_hash" label="内容 Hash" min-width="260" />
        </ElTable>

        <ElDivider />
        <ElCard
          v-for="target in previewMutation.data.value.targets"
          :key="target.runtime_target_id"
          shadow="never"
          class="agent-publish__target"
        >
          <div class="agent-publish__summary">
            <strong>{{ target.runtime_target_id }}</strong>
            <ElTag :type="target.current_deployment_id === null ? 'info' : 'success'">
              {{ target.current_deployment_id === null ? '首次发布' : '已有 ACTIVE Deployment' }}
            </ElTag>
          </div>
          <p>Deployment: {{ target.current_deployment_id ?? '—' }}</p>
          <p>Snapshot: {{ target.current_snapshot_id ?? '—' }}</p>
          <ElEmpty v-if="target.changes.length === 0" description="配置无变化" />
          <div v-for="[category, changes] in groupedChanges(target.changes)" :key="category">
            <ElDivider content-position="left">{{ category }}</ElDivider>
            <div
              v-for="change in changes"
              :key="`${change.path}-${change.change_type}`"
              class="agent-publish__change"
            >
              <ElTag size="small">{{ change.change_type }}</ElTag>
              <code>{{ change.path }}</code>
              <span>{{ displayValue(change.before) }} → {{ displayValue(change.after) }}</span>
              <ElTag v-if="change.sensitive" size="small" type="warning">已脱敏</ElTag>
            </div>
          </div>
        </ElCard>
      </ElCard>

      <ElCard v-if="releaseId" shadow="never" class="agent-publish__panel">
        <ElText tag="h2">{{ releaseAction }}状态</ElText>
        <ElSkeleton v-if="releaseQuery.isPending.value" :rows="3" animated />
        <ElAlert
          v-else-if="releaseQuery.isError.value"
          title="发布状态读取失败，将保留 Release ID 供重试"
          type="error"
          :closable="false"
          show-icon
        >
          <ElButton @click="releaseQuery.refetch()">重试查询</ElButton>
        </ElAlert>
        <template v-else-if="releaseQuery.data.value">
          <ElSpace>
            <ElTag>{{ releaseQuery.data.value.status }}</ElTag>
            <span>Release: {{ releaseQuery.data.value.id }}</span>
          </ElSpace>
          <ElAlert
            v-if="releaseQuery.data.value.status === 'FAILED'"
            :title="releaseQuery.data.value.error?.message ?? `${releaseAction}失败`"
            type="error"
            :closable="false"
            show-icon
          />
          <ElAlert
            v-else-if="releaseQuery.data.value.status === 'SUCCEEDED'"
            :title="`${releaseAction}成功`"
            type="success"
            :closable="false"
            show-icon
          />
          <ElTable
            v-if="deploymentsQuery.data.value"
            :data="deploymentsQuery.data.value"
            empty-text="本次发布未激活 Deployment"
          >
            <ElTableColumn prop="runtime_target_id" label="Runtime Target" />
            <ElTableColumn prop="id" label="Deployment ID" min-width="220" />
            <ElTableColumn prop="status" label="状态" width="120" />
            <ElTableColumn prop="snapshot_id" label="Snapshot ID" min-width="220" />
          </ElTable>
        </template>
      </ElCard>

      <ElCard shadow="never" class="agent-publish__panel">
        <ElText tag="h2">版本历史</ElText>
        <ElAlert
          v-if="versionsQuery.isError.value"
          title="版本历史加载失败"
          type="error"
          :closable="false"
          show-icon
        />
        <ElAlert
          v-if="rollbackMutation.isError.value"
          :title="
            rollbackMutation.error.value instanceof Error
              ? rollbackMutation.error.value.message
              : '回滚请求失败'
          "
          type="error"
          :closable="false"
          show-icon
        />
        <ElTable v-else :data="loadedVersions" empty-text="尚无已发布版本">
          <ElTableColumn prop="version_no" label="版本" width="90" />
          <ElTableColumn prop="release_note" label="发布说明" min-width="180" />
          <ElTableColumn prop="content_hash" label="Snapshot Hash" min-width="260" />
          <ElTableColumn prop="created_at" label="发布时间" min-width="180" />
          <ElTableColumn label="操作" width="120" fixed="right">
            <template #default="scope">
              <ElPopconfirm
                :title="`确认以 v${scope.row.version_no} 的历史 Snapshot 创建新回滚 Release？`"
                confirm-button-text="确认回滚"
                cancel-button-text="取消"
                @confirm="rollbackVersionAt(scope.$index)"
              >
                <template #reference>
                  <ElButton
                    type="warning"
                    link
                    :loading="
                      rollbackMutation.isPending.value &&
                      rollbackMutation.variables.value?.id === scope.row.id
                    "
                    :disabled="
                      runtimeTargets.length === 0 ||
                      releaseInProgress ||
                      rollbackMutation.isPending.value
                    "
                  >
                    回滚
                  </ElButton>
                </template>
              </ElPopconfirm>
            </template>
          </ElTableColumn>
        </ElTable>
        <ElButton
          v-if="nextVersionCursor"
          :loading="loadMoreMutation.isPending.value"
          @click="loadMoreMutation.mutate()"
          >加载更多</ElButton
        >

        <ElDivider content-position="left">版本详情与比较</ElDivider>
        <div class="agent-publish__version-selectors">
          <select v-model="selectedVersionId" class="agent-publish__select">
            <option value="">查看版本详情</option>
            <option v-for="version in loadedVersions" :key="version.id" :value="version.id">
              v{{ version.version_no }}
            </option>
          </select>
          <select v-model="fromVersionId" class="agent-publish__select">
            <option value="">对比起始版本</option>
            <option v-for="version in loadedVersions" :key="version.id" :value="version.id">
              v{{ version.version_no }}
            </option>
          </select>
          <select v-model="toVersionId" class="agent-publish__select">
            <option value="">对比目标版本</option>
            <option v-for="version in loadedVersions" :key="version.id" :value="version.id">
              v{{ version.version_no }}
            </option>
          </select>
        </div>
        <p v-if="selectedVersionQuery.data.value">
          v{{ selectedVersionQuery.data.value.version_no }} · Snapshot
          {{ selectedVersionQuery.data.value.snapshot_id }} ·
          {{ selectedVersionQuery.data.value.release_note || '无发布说明' }}
        </p>
        <ElAlert
          v-if="historyDiffQuery.isError.value"
          title="历史版本 Diff 加载失败"
          type="error"
          :closable="false"
          show-icon
        />
        <div v-if="historyDiffQuery.data.value">
          <div
            v-for="change in historyDiffQuery.data.value.changes"
            :key="`${change.path}-${change.change_type}`"
            class="agent-publish__change"
          >
            <ElTag size="small">{{ change.category }}</ElTag>
            <code>{{ change.path }}</code>
            <span>{{ displayValue(change.before) }} → {{ displayValue(change.after) }}</span>
          </div>
        </div>
      </ElCard>
    </template>
  </section>
</template>

<style scoped>
.agent-publish {
  display: grid;
  gap: var(--ap-space-4);
}

.agent-publish__heading,
.agent-publish__summary,
.agent-publish__options,
.agent-publish__change,
.agent-publish__version-selectors {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ap-space-3);
}

.agent-publish__select {
  min-width: 180px;
  min-height: 32px;
  border: 1px solid var(--ap-border-color);
  border-radius: var(--ap-radius-md);
  background: var(--ap-surface-color);
  color: var(--el-text-color-primary);
  padding: 0 var(--ap-space-2);
}

.agent-publish__heading p,
.agent-publish__hash,
.agent-publish__target p {
  color: var(--ap-text-secondary);
  overflow-wrap: anywhere;
}

.agent-publish__panel,
.agent-publish__target {
  display: grid;
  gap: var(--ap-space-4);
}

.agent-publish__target + .agent-publish__target {
  margin-top: var(--ap-space-4);
}

.agent-publish__change {
  justify-content: flex-start;
  flex-wrap: wrap;
  padding: var(--ap-space-2) 0;
  border-bottom: 1px solid var(--ap-border-color);
}

.agent-publish__change code,
.agent-publish__change span {
  overflow-wrap: anywhere;
}

.agent-publish__version-selectors {
  justify-content: flex-start;
  flex-wrap: wrap;
}

@media (max-width: 767px) {
  .agent-publish__heading,
  .agent-publish__summary,
  .agent-publish__options {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
