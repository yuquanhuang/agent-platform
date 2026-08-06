import { createRouter, createWebHistory } from 'vue-router';

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      component: () => import('@/layouts/AppShell.vue'),
      children: [
        {
          path: '',
          name: 'platform-status',
          component: () => import('@/views/PlatformStatusView.vue'),
        },
        {
          path: 'prompts',
          name: 'prompt-list',
          component: () => import('@/views/prompts/PromptListView.vue'),
          meta: { permission: 'prompt:list' },
        },
        {
          path: 'prompts/:id/edit',
          name: 'prompt-edit',
          component: () => import('@/views/prompts/PromptEditorView.vue'),
          meta: { permission: 'prompt:update' },
        },
      ],
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/',
    },
  ],
});
