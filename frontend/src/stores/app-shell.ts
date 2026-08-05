import { ref } from 'vue';
import { defineStore } from 'pinia';

export const useAppShellStore = defineStore('app-shell', () => {
  const sidebarCollapsed = ref(false);

  function toggleSidebar(): void {
    sidebarCollapsed.value = !sidebarCollapsed.value;
  }

  return { sidebarCollapsed, toggleSidebar };
});
