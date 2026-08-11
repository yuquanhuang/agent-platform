import Ajv2020, { type ErrorObject, type ValidateFunction } from 'ajv/dist/2020';
import addFormats from 'ajv-formats';

import runEventSchema from '@/api/generated/run-event.schema.json';
import type { RunEvent } from '@/api/generated/run-event';

// Variant fragments rely on the root envelope's object constraint. This is valid
// JSON Schema 2020-12, but Ajv's optional strictTypes lint requires each fragment
// to repeat `type: object`, so only that lint is disabled for the frozen schema.
const ajv = new Ajv2020({ allErrors: false, strict: true, strictTypes: false });
addFormats(ajv);

const validateRunEvent = ajv.compile(runEventSchema) as ValidateFunction<RunEvent>;

export class RunEventValidationError extends Error {
  readonly code = 'INVALID_RUN_EVENT';

  constructor(errors: ReadonlyArray<ErrorObject> | null | undefined) {
    super(`RunEvent failed frozen schema validation${formatValidationLocation(errors)}.`);
    this.name = 'RunEventValidationError';
  }
}

export function parseRunEvent(value: unknown): RunEvent {
  if (!validateRunEvent(value)) {
    throw new RunEventValidationError(validateRunEvent.errors);
  }
  return value;
}

function formatValidationLocation(errors: ReadonlyArray<ErrorObject> | null | undefined): string {
  const first = errors?.[0];
  if (first === undefined) return '';
  const location = first.instancePath === '' ? ' at the event root' : ` at ${first.instancePath}`;
  return `${location} (${first.keyword})`;
}
