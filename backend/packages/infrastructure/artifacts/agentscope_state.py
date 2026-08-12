"""Encrypted-object-boundary AgentScope checkpoint State Store."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from minio import Minio
from pydantic import SecretStr

from packages.application.temporal import RunExecutionRequest
from packages.contracts.public import TenantContext
from packages.runtimes.agentscope import (
    AgentScopeCheckpointMetadataStore,
    AgentScopeStateStore,
)


class MinioAgentScopeStateStore(AgentScopeStateStore):
    """Persist checkpoint bytes in private MinIO and facts in PostgreSQL."""

    def __init__(
        self,
        client: Minio,
        metadata_store: AgentScopeCheckpointMetadataStore,
        *,
        bucket: str,
        encryption_key: SecretStr,
        retention: timedelta = timedelta(hours=24),
        max_state_bytes: int = 10_485_760,
    ) -> None:
        if retention <= timedelta(0):
            raise ValueError("Checkpoint retention must be positive")
        if max_state_bytes < 1:
            raise ValueError("Checkpoint max_state_bytes must be positive")
        self._client = client
        self._metadata = metadata_store
        self._bucket = bucket
        self._encryption_key = _decode_encryption_key(encryption_key)
        self._retention = retention
        self._max_state_bytes = max_state_bytes

    async def save(
        self,
        context: TenantContext,
        *,
        request: RunExecutionRequest,
        state_json: bytes,
    ) -> str:
        _validate_state(state_json, self._max_state_bytes)
        now = datetime.now(UTC)
        content_hash = "sha256:" + hashlib.sha256(state_json).hexdigest()
        fencing_token_hash = _fencing_token_hash(request.fencing_token)
        record = await self._metadata.reserve(
            context,
            request=request,
            content_hash=content_hash,
            size_bytes=len(state_json),
            fencing_token_hash=fencing_token_hash,
            expires_at=now + self._retention,
        )
        try:
            encrypted_state = _encrypt_state(
                self._encryption_key,
                state_json,
                associated_data=_associated_data(
                    record.state_ref,
                    record.content_hash,
                ),
            )
            await asyncio.to_thread(
                self._put,
                record.object_key,
                encrypted_state,
                content_hash,
            )
            await self._metadata.mark_available(
                context,
                checkpoint_id=record.id,
                available_at=datetime.now(UTC),
            )
        except asyncio.CancelledError:
            await self._metadata.mark_failed(context, checkpoint_id=record.id)
            raise
        except Exception:
            await self._metadata.mark_failed(context, checkpoint_id=record.id)
            raise
        return record.state_ref

    async def load_latest(
        self,
        context: TenantContext,
        *,
        request: RunExecutionRequest,
    ) -> bytes | None:
        record = await self._metadata.load_latest(
            context,
            request=request,
            fencing_token_hash=_fencing_token_hash(request.fencing_token),
            now=datetime.now(UTC),
        )
        if record is None:
            return None
        try:
            encrypted_state = await asyncio.to_thread(
                self._read,
                record.object_key,
                record.size_bytes + 28,
            )
        except Exception as error:
            raise RuntimeError("AgentScope checkpoint object is unavailable") from error
        try:
            state_json = _decrypt_state(
                self._encryption_key,
                encrypted_state,
                associated_data=_associated_data(
                    record.state_ref,
                    record.content_hash,
                ),
            )
        except Exception as error:
            raise RuntimeError("AgentScope checkpoint decryption failed") from error
        if (
            len(state_json) != record.size_bytes
            or "sha256:" + hashlib.sha256(state_json).hexdigest() != record.content_hash
        ):
            raise RuntimeError("AgentScope checkpoint content hash mismatch")
        _validate_state(state_json, self._max_state_bytes)
        return state_json

    def _put(self, object_key: str, encrypted_state: bytes, content_hash: str) -> None:
        self._client.put_object(
            self._bucket,
            object_key,
            io.BytesIO(encrypted_state),
            len(encrypted_state),
            content_type="application/octet-stream",
            metadata={"content-hash": content_hash, "encryption": "aes-256-gcm"},
        )

    def _read(self, object_key: str, expected_size: int) -> bytes:
        response = self._client.get_object(self._bucket, object_key)
        data = bytearray()
        try:
            while len(data) <= expected_size:
                chunk = response.read(min(1_048_576, expected_size + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
        finally:
            response.close()
            response.release_conn()
        if len(data) != expected_size:
            raise ValueError("AgentScope checkpoint object size mismatch")
        return bytes(data)


def _fencing_token_hash(token: SecretStr) -> str:
    return "sha256:" + hashlib.sha256(token.get_secret_value().encode()).hexdigest()


def _validate_state(state_json: bytes, max_state_bytes: int) -> None:
    if not state_json or len(state_json) > max_state_bytes:
        raise ValueError("AgentScope checkpoint size is invalid")
    try:
        json.loads(state_json)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("AgentScope checkpoint must contain JSON") from error


def _decode_encryption_key(secret: SecretStr) -> bytes:
    value = secret.get_secret_value().encode()
    try:
        key = base64.urlsafe_b64decode(value + b"=" * (-len(value) % 4))
    except ValueError as error:
        raise ValueError("Checkpoint encryption key must be base64url") from error
    if len(key) != 32:
        raise ValueError("Checkpoint encryption key must contain 32 bytes")
    return key


def _associated_data(state_ref: str, content_hash: str) -> bytes:
    return f"{state_ref}\n{content_hash}".encode()


def _encrypt_state(key: bytes, state_json: bytes, *, associated_data: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, state_json, associated_data)


def _decrypt_state(key: bytes, payload: bytes, *, associated_data: bytes) -> bytes:
    if len(payload) < 28:
        raise ValueError("Encrypted checkpoint payload is truncated")
    nonce = payload[:12]
    return AESGCM(key).decrypt(nonce, payload[12:], associated_data)
