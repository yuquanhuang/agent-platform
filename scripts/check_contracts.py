#!/usr/bin/env python3
"""Validate the frozen Agent Platform contract baseline without mutating it."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from jsonschema_path import SchemaPath
from jsonschema_specifications import REGISTRY as JSON_SCHEMA_SPECIFICATIONS
from openapi_spec_validator import OpenAPIV31SpecValidator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

DEFAULT_BASELINE = "agent-platform-baseline.yaml"
DRAFT_2020_12_URI = "https://json-schema.org/draft/2020-12/schema"
EXAMPLE_SCHEMA_KEYS = {
    "run_spec": "run_spec",
    "run_event_text_delta": "run_event",
    "skill_manifest": "skill_manifest",
    "bundle_manifest": "bundle_manifest",
}


class ContractValidationError(Exception):
    """Raised when a contract gate cannot complete successfully."""


class OfflineContractRefHandler:
    """Resolve OpenAPI references only from the local contract tree."""

    def __init__(self, contract_root: Path) -> None:
        self.contract_root = contract_root.resolve()

    def __call__(self, uri: str) -> Any:
        clean_uri = uri.rstrip("#")
        if clean_uri in JSON_SCHEMA_SPECIFICATIONS:
            return JSON_SCHEMA_SPECIFICATIONS.contents(clean_uri)

        parsed = urlparse(clean_uri)
        if parsed.scheme == "file":
            path = Path(unquote(parsed.path)).resolve()
        elif parsed.scheme == "https" and parsed.netloc == "agent-platform.local":
            path = (self.contract_root / parsed.path.lstrip("/")).resolve()
        else:
            raise ValueError(f"offline contract resolver rejected URI: {uri}")

        try:
            path.relative_to(self.contract_root)
        except ValueError as exc:
            raise ValueError(
                f"contract reference escapes baseline directory: {uri}"
            ) from exc
        if not path.is_file():
            raise FileNotFoundError(path)
        return load_yaml(path)


class UniqueKeyLoader(yaml.SafeLoader):
    """YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_yaml(path: Path) -> Any:
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ContractValidationError(f"cannot load YAML {path}: {exc}") from exc


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractValidationError(f"cannot load JSON {path}: {exc}") from exc


def require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{label} must be a mapping")
    return value


def manifest_filename(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractValidationError(f"{label} must be a non-empty string")
    return value.split("@", maxsplit=1)[0]


def resolve_contract_path(contract_root: Path, relative_path: str) -> Path:
    candidate = (contract_root / relative_path).resolve()
    try:
        candidate.relative_to(contract_root)
    except ValueError as exc:
        raise ContractValidationError(
            f"contract path escapes baseline directory: {relative_path}"
        ) from exc
    return candidate


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as contract_file:
            for chunk in iter(lambda: contract_file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ContractValidationError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def validate_integrity(baseline: Mapping[str, Any], contract_root: Path) -> int:
    if baseline.get("integrity_algorithm") != "sha256":
        raise ContractValidationError("baseline integrity_algorithm must be sha256")

    integrity = require_mapping(baseline.get("integrity"), "baseline integrity")
    if not integrity:
        raise ContractValidationError("baseline integrity must not be empty")

    failures: list[str] = []
    for relative_path, expected_digest in integrity.items():
        if not isinstance(relative_path, str) or not isinstance(expected_digest, str):
            failures.append(f"invalid integrity entry: {relative_path!r}")
            continue
        contract_path = resolve_contract_path(contract_root, relative_path)
        if not contract_path.is_file():
            failures.append(f"missing file: {relative_path}")
            continue
        actual_digest = sha256(contract_path)
        if actual_digest != expected_digest.lower():
            failures.append(
                f"SHA-256 mismatch: {relative_path} "
                f"(expected {expected_digest}, got {actual_digest})"
            )

    if failures:
        raise ContractValidationError("\n".join(failures))
    return len(integrity)


def iter_operation_ids(
    value: Any, location: tuple[str, ...] = ()
) -> Iterator[tuple[str, str]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_location = (*location, str(key))
            if key == "operationId":
                if not isinstance(child, str) or not child:
                    raise ContractValidationError(
                        f"invalid operationId at {'/'.join(child_location)}"
                    )
                yield child, "/".join(child_location)
            else:
                yield from iter_operation_ids(child, child_location)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from iter_operation_ids(child, (*location, str(index)))


def validate_openapi(baseline: Mapping[str, Any], contract_root: Path) -> int:
    machine_readable = require_mapping(
        baseline.get("machine_readable"), "baseline machine_readable"
    )
    openapi_keys = ("openapi_core", "openapi_resources")
    seen_operation_ids: dict[str, str] = {}

    for key in openapi_keys:
        relative_path = manifest_filename(machine_readable.get(key), key)
        openapi_path = resolve_contract_path(contract_root, relative_path)
        document = require_mapping(load_yaml(openapi_path), relative_path)
        version = document.get("openapi")
        if not isinstance(version, str) or not version.startswith("3.1."):
            raise ContractValidationError(
                f"{relative_path} must declare OpenAPI 3.1.x, got {version!r}"
            )
        handlers = {
            "<all_urls>": OfflineContractRefHandler(contract_root),
            "file": OfflineContractRefHandler(contract_root),
            "http": OfflineContractRefHandler(contract_root),
            "https": OfflineContractRefHandler(contract_root),
        }
        try:
            schema_path = SchemaPath.from_dict(
                document,
                base_uri=f"{openapi_path.parent.as_uri()}/",
                handlers=handlers,
                ref_resolver_handlers=handlers,
            )
            OpenAPIV31SpecValidator(schema_path).validate()
        except Exception as exc:  # validator exposes several exception types
            raise ContractValidationError(
                f"OpenAPI validation failed for {relative_path}: {exc}"
            ) from exc

        for operation_id, location in iter_operation_ids(document):
            current = f"{relative_path}:{location}"
            previous = seen_operation_ids.get(operation_id)
            if previous is not None:
                raise ContractValidationError(
                    f"duplicate operationId {operation_id!r}: {previous} and {current}"
                )
            seen_operation_ids[operation_id] = current

    return len(seen_operation_ids)


def iter_references(value: Any) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "$ref" and isinstance(child, str):
                yield child
            yield from iter_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_references(child)


def load_schemas(contract_root: Path) -> dict[Path, Mapping[str, Any]]:
    schema_paths = sorted((contract_root / "schemas").glob("*.schema.json"))
    if not schema_paths:
        raise ContractValidationError("no JSON Schema files found")

    schemas: dict[Path, Mapping[str, Any]] = {}
    for schema_path in schema_paths:
        schema = require_mapping(load_json(schema_path), schema_path.name)
        if schema.get("$schema") != DRAFT_2020_12_URI:
            raise ContractValidationError(
                f"{schema_path.name} must declare JSON Schema Draft 2020-12"
            )
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise ContractValidationError(
                f"invalid JSON Schema {schema_path.name}: {exc.message}"
            ) from exc
        schemas[schema_path.resolve()] = schema
    return schemas


def build_schema_registry(
    schemas: Mapping[Path, Mapping[str, Any]],
) -> Registry[Any]:
    meta_resource = Resource.from_contents(
        Draft202012Validator.META_SCHEMA,
        default_specification=DRAFT202012,
    )
    registry = Registry().with_resource(DRAFT_2020_12_URI, meta_resource)

    for schema_path, schema in schemas.items():
        resource = Resource.from_contents(schema, default_specification=DRAFT202012)
        registry = registry.with_resource(schema_path.as_uri(), resource)
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str) or not schema_id:
            raise ContractValidationError(f"{schema_path.name} must declare a $id")
        registry = registry.with_resource(schema_id, resource)
    return registry


def validate_schema_references(
    schemas: Mapping[Path, Mapping[str, Any]],
    registry: Registry[Any],
) -> int:
    reference_count = 0
    for schema_path, schema in schemas.items():
        base_uri = str(schema["$id"])
        resolver = registry.resolver(base_uri)
        for reference in iter_references(schema):
            reference_count += 1
            try:
                resolver.lookup(reference)
            except Exception as exc:  # referencing exposes resolution-specific errors
                raise ContractValidationError(
                    f"unresolvable $ref in {schema_path.name}: {reference}: {exc}"
                ) from exc
    return reference_count


def validation_error_message(error: Any) -> str:
    instance_path = "/".join(str(part) for part in error.absolute_path)
    schema_path = "/".join(str(part) for part in error.absolute_schema_path)
    return (
        f"instance /{instance_path or '<root>'}: {error.message} "
        f"(schema /{schema_path or '<root>'})"
    )


def validate_examples(
    baseline: Mapping[str, Any],
    contract_root: Path,
    schemas: Mapping[Path, Mapping[str, Any]],
    registry: Registry[Any],
) -> int:
    examples = require_mapping(baseline.get("examples"), "baseline examples")
    machine_readable = require_mapping(
        baseline.get("machine_readable"), "baseline machine_readable"
    )

    checked = 0
    for example_key, schema_key in EXAMPLE_SCHEMA_KEYS.items():
        example_relative = manifest_filename(examples.get(example_key), example_key)
        schema_relative = manifest_filename(
            machine_readable.get(schema_key), schema_key
        )
        example_path = resolve_contract_path(contract_root, example_relative)
        schema_path = resolve_contract_path(contract_root, schema_relative).resolve()
        schema = schemas.get(schema_path)
        if schema is None:
            raise ContractValidationError(
                f"example {example_relative} references an unloaded schema: {schema_relative}"
            )

        instance = (
            load_yaml(example_path)
            if example_path.suffix in {".yaml", ".yml"}
            else load_json(example_path)
        )
        validator = Draft202012Validator(
            schema,
            registry=registry,
            format_checker=FormatChecker(),
        )
        errors = sorted(
            validator.iter_errors(instance), key=lambda item: list(item.absolute_path)
        )
        if errors:
            details = "\n".join(
                f"  - {validation_error_message(error)}" for error in errors[:20]
            )
            suffix = "\n  - ... more errors omitted" if len(errors) > 20 else ""
            raise ContractValidationError(
                f"example validation failed: {example_relative} -> {schema_relative}\n"
                f"{details}{suffix}"
            )
        checked += 1
    return checked


def validate_contracts(baseline_path: Path) -> dict[str, int | str]:
    baseline_path = baseline_path.resolve()
    if not baseline_path.is_file():
        raise ContractValidationError(f"baseline file does not exist: {baseline_path}")
    contract_root = baseline_path.parent.resolve()
    baseline = require_mapping(load_yaml(baseline_path), "baseline")

    integrity_files = validate_integrity(baseline, contract_root)
    operation_ids = validate_openapi(baseline, contract_root)
    schemas = load_schemas(contract_root)
    registry = build_schema_registry(schemas)
    schema_references = validate_schema_references(schemas, registry)
    examples = validate_examples(baseline, contract_root, schemas, registry)

    baseline_id = baseline.get("baseline_id")
    if not isinstance(baseline_id, str) or not baseline_id:
        raise ContractValidationError("baseline_id must be a non-empty string")
    return {
        "baseline_id": baseline_id,
        "integrity_files": integrity_files,
        "operation_ids": operation_ids,
        "schemas": len(schemas),
        "schema_references": schema_references,
        "examples": examples,
    }


def parse_args(arguments: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path(DEFAULT_BASELINE),
        help=f"Path to the baseline manifest (default: {DEFAULT_BASELINE})",
    )
    return parser.parse_args(arguments)


def main(arguments: Iterable[str] | None = None) -> int:
    args = parse_args(arguments)
    try:
        summary = validate_contracts(args.baseline)
    except ContractValidationError as exc:
        print(f"Contract check FAILED:\n{exc}", file=sys.stderr)
        return 1

    print(f"Contract check PASSED: {summary['baseline_id']}")
    print(
        "Validated "
        f"{summary['integrity_files']} integrity files, "
        "2 OpenAPI 3.1 documents with "
        f"{summary['operation_ids']} unique operationIds, "
        f"{summary['schemas']} Draft 2020-12 schemas, "
        f"{summary['schema_references']} schema references, and "
        f"{summary['examples']} golden examples."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
