<script setup lang="ts">
import { useQuery } from '@tanstack/vue-query';
import { computed, ref } from 'vue';
import { useRoute } from 'vue-router';

import type { MessageContentPart } from '@/api/generated/core-models';
import { sessionService } from '@/services/sessions';
import {
  ElAlert,
  ElButton,
  ElCard,
  ElEmpty,
  ElInput,
  ElSkeleton,
  ElSpace,
  ElTag,
  ElText,
} from '@/ui/element-plus';

const route = useRoute();
const sessionId = computed(() => String(route.params.id ?? ''));
const branchInput = ref('');
const branchFilter = ref('');
const cursor = ref<string | undefined>();
const cursorHistory = ref<Array<string | undefined>>([]);

const sessionQuery = useQuery({
  queryKey: computed(() => ['session', sessionId.value]),
  queryFn: () => sessionService.get(sessionId.value),
  enabled: computed(() => sessionId.value !== ''),
});

const messagesQuery = useQuery({
  queryKey: computed(() => [
    'session-messages',
    sessionId.value,
    { branchId: branchFilter.value, cursor: cursor.value },
  ]),
  queryFn: () =>
    sessionService.listMessages(sessionId.value, {
      limit: 50,
      ...(cursor.value === undefined ? {} : { cursor: cursor.value }),
      ...(branchFilter.value === '' ? {} : { branchId: branchFilter.value }),
    }),
  enabled: computed(
    () => sessionQuery.isSuccess.value && sessionQuery.data.value?.status !== 'DELETED',
  ),
});

const messages = computed(() => [...(messagesQuery.data.value?.items ?? [])]);

function searchBranch(): void {
  branchFilter.value = branchInput.value.trim();
  cursor.value = undefined;
  cursorHistory.value = [];
}

function nextPage(): void {
  const nextCursor = messagesQuery.data.value?.next_cursor;
  if (nextCursor === null || nextCursor === undefined) return;
  cursorHistory.value.push(cursor.value);
  cursor.value = nextCursor;
}

function previousPage(): void {
  cursor.value = cursorHistory.value.pop();
}

function contentText(part: MessageContentPart): string {
  if (part.type === 'text') return part.text ?? '';
  if (part.type === 'artifact_reference') return `Artifact: ${part.artifact_id ?? ''}`;
  if (part.type === 'tool_reference') return `Tool call: ${part.tool_call_id ?? ''}`;
  return [part.error_code ?? 'Error', part.text ?? ''].filter(Boolean).join(': ');
}

function formatCreatedAt(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'medium' }).format(
    new Date(value),
  );
}
</script>

<template>
  <section class="session-messages" aria-labelledby="session-messages-title">
    <header class="session-messages__heading">
      <div>
        <ElText id="session-messages-title" tag="h1" size="large">
          {{ sessionQuery.data.value?.title || 'Session 消息历史' }}
        </ElText>
        <p v-if="sessionQuery.data.value">
          Session {{ sessionQuery.data.value.id }} · Deployment
          {{ sessionQuery.data.value.default_deployment_id }}
        </p>
      </div>
    </header>

    <ElSkeleton v-if="sessionQuery.isPending.value" :rows="4" animated />
    <ElAlert
      v-else-if="sessionQuery.isError.value"
      title="Session 信息加载失败"
      type="error"
      :closable="false"
      show-icon
    />
    <ElAlert
      v-else-if="sessionQuery.data.value?.status === 'DELETED'"
      title="已删除 Session 不提供消息正文"
      type="warning"
      :closable="false"
      show-icon
    />
    <template v-else>
      <ElCard shadow="never">
        <ElSpace class="session-messages__filters" wrap>
          <ElInput
            v-model="branchInput"
            aria-label="按分支 ID 筛选"
            placeholder="分支 ID（可选）"
            clearable
            @keyup.enter="searchBranch"
          />
          <ElButton :loading="messagesQuery.isFetching.value" @click="searchBranch">
            查询分支
          </ElButton>
        </ElSpace>
      </ElCard>

      <ElSkeleton v-if="messagesQuery.isPending.value" :rows="8" animated />
      <ElAlert
        v-else-if="messagesQuery.isError.value"
        title="消息历史加载失败"
        type="error"
        :closable="false"
        show-icon
      >
        <ElButton @click="messagesQuery.refetch()">重新加载</ElButton>
      </ElAlert>
      <ElEmpty v-else-if="messages.length === 0" description="暂无消息" />
      <div v-else class="session-messages__list">
        <ElCard v-for="message in messages" :key="message.id" shadow="never">
          <template #header>
            <div class="session-messages__message-heading">
              <ElTag>{{ message.role }}</ElTag>
              <small>{{ formatCreatedAt(message.created_at) }}</small>
            </div>
          </template>
          <dl class="session-messages__meta">
            <div>
              <dt>Message ID</dt>
              <dd>{{ message.id }}</dd>
            </div>
            <div>
              <dt>Branch ID</dt>
              <dd>{{ message.branch_id || '主链' }}</dd>
            </div>
            <div>
              <dt>Parent</dt>
              <dd>{{ message.parent_message_id || '无' }}</dd>
            </div>
          </dl>
          <ul class="session-messages__parts">
            <li v-for="(part, index) in message.content_parts" :key="`${message.id}-${index}`">
              <span class="session-messages__part-type">{{ part.type }}</span>
              <span>{{ contentText(part) }}</span>
            </li>
          </ul>
        </ElCard>
      </div>

      <ElSpace v-if="messages.length > 0" class="session-messages__pagination">
        <ElButton :disabled="cursorHistory.length === 0" @click="previousPage">上一页</ElButton>
        <ElButton :disabled="!messagesQuery.data.value?.has_more" @click="nextPage"
          >下一页</ElButton
        >
      </ElSpace>
    </template>
  </section>
</template>

<style scoped>
.session-messages {
  display: grid;
  gap: var(--ap-space-5);
}
.session-messages__heading p {
  margin-bottom: 0;
  color: var(--ap-text-secondary);
}
.session-messages__filters {
  width: 100%;
}
.session-messages__list {
  display: grid;
  gap: var(--ap-space-4);
}
.session-messages__message-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ap-space-3);
}
.session-messages__meta {
  display: grid;
  gap: var(--ap-space-2);
}
.session-messages__meta div {
  display: grid;
  grid-template-columns: 120px minmax(0, 1fr);
  gap: var(--ap-space-3);
}
.session-messages__meta dt {
  color: var(--ap-text-secondary);
}
.session-messages__meta dd {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
}
.session-messages__parts {
  display: grid;
  gap: var(--ap-space-2);
  padding-left: 1.2rem;
}
.session-messages__part-type {
  margin-right: var(--ap-space-2);
  color: var(--ap-text-secondary);
}
.session-messages__pagination {
  justify-content: flex-end;
}
</style>
