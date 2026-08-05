<script setup lang="ts">
import { computed } from 'vue';

import { useBackendReadiness } from '@/composables/use-backend-readiness';
import { ElAlert, ElButton, ElCard, ElSkeleton, ElTag, ElText } from '@/ui/element-plus';

const readinessQuery = useBackendReadiness();
const errorMessage = computed(() => {
  const error = readinessQuery.error.value;
  return error instanceof Error ? error.message : '后端服务暂时不可用';
});

function retryReadiness(): void {
  void readinessQuery.refetch();
}
</script>

<template>
  <section class="status-page" aria-labelledby="status-page-title">
    <header class="status-page__heading">
      <div>
        <ElText id="status-page-title" tag="h1" size="large">平台工程状态</ElText>
        <p>验证 Vue 应用壳、状态管理和后端健康检查之间的最小协同链路。</p>
      </div>
      <ElTag type="info" effect="plain">AP-E0-003</ElTag>
    </header>

    <ElCard shadow="never">
      <template #header>
        <strong>后端 API</strong>
      </template>

      <ElSkeleton v-if="readinessQuery.isPending.value" :rows="2" animated />

      <ElAlert
        v-else-if="readinessQuery.isError.value"
        :title="errorMessage"
        type="error"
        :closable="false"
        show-icon
      >
        <template #default>
          <ElButton :loading="readinessQuery.isFetching.value" @click="retryReadiness">
            重新检查
          </ElButton>
        </template>
      </ElAlert>

      <div v-else class="status-page__ready">
        <ElTag type="success">可用</ElTag>
        <div>
          <strong>{{ readinessQuery.data.value?.serviceName }}</strong>
          <p>已通过同源 /health/ready 完成连接验证。</p>
        </div>
      </div>
    </ElCard>

    <ElAlert
      title="业务功能尚未启用"
      description="Agent、Prompt、Run 等页面将在对应任务完成 API Client、权限和业务实现后开放。"
      type="info"
      :closable="false"
      show-icon
    />
  </section>
</template>

<style scoped>
.status-page {
  display: grid;
  gap: var(--ap-space-6);
}

.status-page__heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--ap-space-4);
}

.status-page__heading h1,
.status-page__heading p,
.status-page__ready p {
  margin: 0;
}

.status-page__heading p,
.status-page__ready p {
  margin-top: var(--ap-space-2);
  color: var(--ap-text-secondary);
}

.status-page__ready {
  display: flex;
  align-items: flex-start;
  gap: var(--ap-space-4);
}

@media (max-width: 767px) {
  .status-page__heading {
    flex-direction: column;
  }
}
</style>
