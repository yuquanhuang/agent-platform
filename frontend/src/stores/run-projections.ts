import { defineStore } from 'pinia';
import { shallowRef } from 'vue';

import type { RunEventPage } from '@/api/generated/core-models';
import type { RunEvent } from '@/api/generated/run-event';
import {
  createRunProjection,
  reduceRunEvent,
  reduceRunEventPage,
  runProjectionKeyFromEvent,
  runProjectionKeyId,
  type RunProjection,
  type RunProjectionKey,
  type RunProjectionOptions,
} from '@/stores/run-projection';

export const useRunProjectionsStore = defineStore('run-projections', () => {
  const projections = shallowRef<Readonly<Record<string, RunProjection>>>({});

  function projectionFor(key: RunProjectionKey): RunProjection | undefined {
    return projections.value[runProjectionKeyId(key)];
  }

  function applyEvent(event: RunEvent, options: RunProjectionOptions = {}): RunProjection {
    const key = runProjectionKeyFromEvent(event);
    const projectionId = runProjectionKeyId(key);
    const current = projections.value[projectionId] ?? createRunProjection(key);
    const next = reduceRunEvent(current, event, options);
    if (next !== current) {
      projections.value = { ...projections.value, [projectionId]: next };
    }
    return next;
  }

  function applyPage(
    key: RunProjectionKey,
    page: RunEventPage,
    options: RunProjectionOptions = {},
  ): RunProjection {
    const projectionId = runProjectionKeyId(key);
    const current = projections.value[projectionId] ?? createRunProjection(key);
    const next = reduceRunEventPage(current, page, options);
    projections.value = { ...projections.value, [projectionId]: next };
    return next;
  }

  function removeProjection(key: RunProjectionKey): void {
    const projectionId = runProjectionKeyId(key);
    if (projections.value[projectionId] === undefined) return;
    const retained: Record<string, RunProjection> = {};
    for (const [currentId, projection] of Object.entries(projections.value)) {
      if (currentId !== projectionId) retained[currentId] = projection;
    }
    projections.value = retained;
  }

  function clearTenant(tenantId: string): void {
    const retained: Record<string, RunProjection> = {};
    for (const [projectionId, projection] of Object.entries(projections.value)) {
      if (projection.key.tenantId !== tenantId) retained[projectionId] = projection;
    }
    projections.value = retained;
  }

  function clearAll(): void {
    projections.value = {};
  }

  return {
    projections,
    projectionFor,
    applyEvent,
    applyPage,
    removeProjection,
    clearTenant,
    clearAll,
  };
});
