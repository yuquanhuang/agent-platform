<script setup lang="ts">
import { storeToRefs } from 'pinia';

import { useAppShellStore } from '@/stores/app-shell';
import {
  ElAside,
  ElButton,
  ElContainer,
  ElHeader,
  ElMain,
  ElMenu,
  ElMenuItem,
  ElText,
} from '@/ui/element-plus';

const appShellStore = useAppShellStore();
const { sidebarCollapsed } = storeToRefs(appShellStore);
</script>

<template>
  <ElContainer class="app-shell">
    <ElAside :width="sidebarCollapsed ? 'var(--ap-sidebar-collapsed)' : 'var(--ap-sidebar-width)'">
      <div class="app-shell__brand" aria-label="agent平台">
        <span class="app-shell__brand-mark">AP</span>
        <strong v-if="!sidebarCollapsed">agent平台</strong>
      </div>
      <ElMenu default-active="/" :collapse="sidebarCollapsed" router>
        <ElMenuItem index="/">
          <span>系统状态</span>
        </ElMenuItem>
        <ElMenuItem index="/prompts">
          <span>Prompt 管理</span>
        </ElMenuItem>
      </ElMenu>
    </ElAside>

    <ElContainer>
      <ElHeader class="app-shell__header">
        <ElButton plain @click="appShellStore.toggleSidebar">
          {{ sidebarCollapsed ? '展开导航' : '收起导航' }}
        </ElButton>
        <ElText type="info">通用 Agent 平台</ElText>
      </ElHeader>
      <ElMain class="app-shell__main">
        <div class="app-shell__content">
          <RouterView />
        </div>
      </ElMain>
    </ElContainer>
  </ElContainer>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
}

.app-shell :deep(.el-aside) {
  border-right: 1px solid var(--ap-border-color);
  background: var(--ap-surface-color);
  transition: width var(--ap-motion-fast);
}

.app-shell__brand {
  display: flex;
  height: var(--ap-header-height);
  align-items: center;
  gap: var(--ap-space-3);
  padding: 0 var(--ap-space-4);
  white-space: nowrap;
}

.app-shell__brand-mark {
  display: inline-flex;
  width: 32px;
  height: 32px;
  align-items: center;
  justify-content: center;
  border-radius: var(--ap-radius-md);
  background: var(--el-color-primary);
  color: var(--el-color-white);
  font-size: 12px;
  font-weight: 700;
}

.app-shell__header {
  display: flex;
  height: var(--ap-header-height);
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--ap-border-color);
  background: var(--ap-surface-color);
}

.app-shell__main {
  background: var(--ap-page-background);
}

.app-shell__content {
  width: min(100%, var(--ap-content-max-width));
  margin: 0 auto;
}

@media (max-width: 767px) {
  .app-shell :deep(.el-aside) {
    width: var(--ap-sidebar-collapsed) !important;
  }

  .app-shell__main {
    padding: var(--ap-space-4);
  }
}
</style>
