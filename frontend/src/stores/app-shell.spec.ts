import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import { useAppShellStore } from '@/stores/app-shell';

describe('app shell store', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('toggles the navigation state', () => {
    const store = useAppShellStore();

    store.toggleSidebar();

    expect(store.sidebarCollapsed).toBe(true);
  });
});
