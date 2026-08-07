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
          path: 'agents',
          name: 'agent-list',
          component: () => import('@/views/agents/AgentListView.vue'),
          meta: { permission: 'agent:list' },
        },
        {
          path: 'agents/:id/edit',
          name: 'agent-edit',
          component: () => import('@/views/agents/AgentEditorView.vue'),
          meta: { permission: 'agent:update' },
        },
        {
          path: 'agents/:id/publish',
          name: 'agent-publish',
          component: () => import('@/views/agents/AgentPublishView.vue'),
          meta: { permission: 'agent:publish' },
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
        {
          path: 'models/providers',
          name: 'model-provider-list',
          component: () => import('@/views/models/ModelProviderView.vue'),
          meta: { permission: 'model_provider:list' },
        },
        {
          path: 'models/configs',
          name: 'model-config-list',
          component: () => import('@/views/models/ModelConfigView.vue'),
          meta: { permission: 'model_config:list' },
        },
      ],
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/',
    },
  ],
});
