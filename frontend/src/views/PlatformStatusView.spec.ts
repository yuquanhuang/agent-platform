import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query';
import { flushPromises, mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { fetchBackendReadiness } from '@/services/health';
import PlatformStatusView from '@/views/PlatformStatusView.vue';

vi.mock('@/services/health', () => ({
  fetchBackendReadiness: vi.fn(),
}));

describe('PlatformStatusView', () => {
  beforeEach(() => {
    vi.mocked(fetchBackendReadiness).mockResolvedValue({
      status: 'ok',
      serviceName: 'api-test',
    });
  });

  it('renders the backend readiness result', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = mount(PlatformStatusView, {
      global: {
        plugins: [[VueQueryPlugin, { queryClient }]],
      },
    });

    await flushPromises();

    expect(wrapper.text()).toContain('api-test');
    expect(wrapper.text()).toContain('已通过同源 /health/ready 完成连接验证');
  });
});
