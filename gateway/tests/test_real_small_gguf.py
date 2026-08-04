"""Recette opt-in contre un vrai binaire llama-server et un petit GGUF épinglé."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
from pathlib import Path

import httpx
import pytest


_BIN_ENV = "EVARUNTIME_REAL_LLAMA_BIN"
_GGUF_ENV = "EVARUNTIME_REAL_GGUF"
_SHA_ENV = "EVARUNTIME_REAL_GGUF_SHA256"
_API_KEY = "evaruntime-real-test-internal"


if not os.environ.get(_BIN_ENV) and not os.environ.get(_GGUF_ENV):
    pytest.skip(
        f"recette réelle opt-in : définir {_BIN_ENV}, {_GGUF_ENV} et {_SHA_ENV}",
        allow_module_level=True,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _drain(stream: asyncio.StreamReader | None, tail: list[str]) -> None:
    if stream is None:
        return
    while line := await stream.readline():
        tail.append(line.decode(errors="replace").rstrip())
        del tail[:-40]


async def _wait_ready(
    client: httpx.AsyncClient,
    process: asyncio.subprocess.Process,
    base_url: str,
    tail: list[str],
) -> None:
    deadline = asyncio.get_running_loop().time() + 180.0
    while asyncio.get_running_loop().time() < deadline:
        if process.returncode is not None:
            raise AssertionError(
                f"llama-server s'est arrêté avec {process.returncode}:\n"
                + "\n".join(tail)
            )
        try:
            response = await client.get(f"{base_url}/health", timeout=2.0)
            if response.status_code == 200:
                return
        except httpx.RequestError:
            pass
        await asyncio.sleep(0.25)
    raise AssertionError("llama-server n'est pas devenu ready en 180 s:\n" + "\n".join(tail))


@pytest.mark.anyio
async def test_real_small_gguf_produces_a_streamed_token() -> None:
    binary = Path(os.environ.get(_BIN_ENV, ""))
    gguf = Path(os.environ.get(_GGUF_ENV, ""))
    expected_sha = os.environ.get(_SHA_ENV, "").lower()

    assert binary.is_absolute() and binary.is_file() and os.access(binary, os.X_OK)
    assert gguf.is_absolute() and gguf.is_file()
    assert len(expected_sha) == 64 and all(c in "0123456789abcdef" for c in expected_sha)
    assert _sha256(gguf) == expected_sha, "le GGUF réel ne correspond pas au SHA-256 épinglé"

    port = _free_loopback_port()
    environment = os.environ.copy()
    environment["LLAMA_API_KEY"] = _API_KEY
    process = await asyncio.create_subprocess_exec(
        str(binary),
        "--model", str(gguf),
        "--host", "127.0.0.1",
        "--port", str(port),
        "--ctx-size", "512",
        "--parallel", "1",
        "--n-gpu-layers", os.environ.get("EVARUNTIME_REAL_N_GPU_LAYERS", "0"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    tail: list[str] = []
    drains = [
        asyncio.create_task(_drain(process.stdout, tail)),
        asyncio.create_task(_drain(process.stderr, tail)),
    ]
    base_url = f"http://127.0.0.1:{port}"
    headers = {"Authorization": f"Bearer {_API_KEY}"}

    try:
        async with httpx.AsyncClient(trust_env=False, headers=headers) as client:
            await _wait_ready(client, process, base_url, tail)
            saw_content = False
            saw_done = False
            async with client.stream(
                "POST",
                f"{base_url}/v1/chat/completions",
                json={
                    "model": "real-small-gguf",
                    "messages": [{"role": "user", "content": "Réponds uniquement: OK"}],
                    "max_tokens": 8,
                    "temperature": 0,
                    "stream": True,
                },
                timeout=60.0,
            ) as response:
                assert response.status_code == 200, await response.aread()
                async for line in response.aiter_lines():
                    if line == "data: [DONE]":
                        saw_done = True
                    elif line.startswith("data: "):
                        chunk = json.loads(line[6:])
                        saw_content = saw_content or any(
                            bool(choice.get("delta", {}).get("content"))
                            for choice in chunk.get("choices", [])
                        )
            assert saw_content, "aucun delta SSE avec contenu produit"
            assert saw_done, "terminaison SSE [DONE] absente"
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        await asyncio.gather(*drains, return_exceptions=True)
