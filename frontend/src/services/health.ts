const HEALTH_REQUEST_TIMEOUT_MS = 5_000;

export interface BackendReadiness {
  readonly status: 'ok';
  readonly serviceName: string;
}

function parseBackendReadiness(payload: unknown): BackendReadiness {
  if (
    typeof payload !== 'object' ||
    payload === null ||
    !('status' in payload) ||
    payload.status !== 'ok' ||
    !('service_name' in payload) ||
    typeof payload.service_name !== 'string' ||
    payload.service_name.length === 0
  ) {
    throw new Error('后端健康响应格式不符合预期');
  }

  return {
    status: payload.status,
    serviceName: payload.service_name,
  };
}

export async function fetchBackendReadiness(signal?: AbortSignal): Promise<BackendReadiness> {
  const controller = new AbortController();
  const abortFromCaller = (): void => controller.abort(signal?.reason);
  signal?.addEventListener('abort', abortFromCaller, { once: true });
  const timeout = window.setTimeout(
    () => controller.abort('health request timeout'),
    HEALTH_REQUEST_TIMEOUT_MS,
  );

  try {
    const response = await fetch('/health/ready', {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error(`后端健康检查失败（HTTP ${response.status}）`);
    }
    const payload: unknown = await response.json();
    return parseBackendReadiness(payload);
  } finally {
    window.clearTimeout(timeout);
    signal?.removeEventListener('abort', abortFromCaller);
  }
}
