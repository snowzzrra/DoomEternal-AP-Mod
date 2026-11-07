import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from native_transport_support import codec, evidence_result, fixtures, split

from doom_eap.runtime.native_transport import (
    IdentityMismatch,
    NativeTransport,
    PreparedCommand,
    ProtocolError,
    TransportError,
)

ROOT = Path(__file__).resolve().parents[1]


def link(tmp_path, mode="normal", timeout=2.0):
    scope = (evidence_result("hello_evidence")["scope"] if mode == "evidence" else
             codec.decode("hello_result", fixtures()["hello_result"][32:])["scope"])
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT), str(ROOT / "tests"))))
    trace = tmp_path / "wire.jsonl"
    command = (sys.executable, "-B", str(ROOT / "tests/fixtures/native_transport_worker.py"), mode, str(trace))
    return NativeTransport(command, scope, env=env, timeout=timeout), trace


def submission():
    return split("submission", fixtures()["submit"][32:])


def test_persistent_partial_frames_preserve_ledger_bytes_and_do_not_ack(tmp_path):
    async def run():
        transport, trace = link(tmp_path)
        try:
            await transport.connect()
            pid = transport._process.pid
            value, payload = submission()
            prepared = await transport.prepare(value, payload)
            assert prepared.envelope == fixtures()["submit"][32:]
            assert prepared.reference == fixtures()["terminal_query"][32:]
            result = await transport.submit(prepared)
            assert result.result["balance_after"] == 3
            assert transport._process.pid == pid
            await transport.query(prepared.reference)
            await transport.cancel(prepared.reference)
            token = bytes(result.response["reply"]["dimensions"]["result_token"])
            await transport.ack(prepared.reference, token)
            messages = [json.loads(line) for line in trace.read_text().splitlines()]
            assert [m["operation"] for m in messages] == [6, 0, 0, 2, 3, 4, 5]
            assert bytes.fromhex(messages[3]["body"]) == prepared.envelope
        finally:
            await transport.close()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["correlation", "identity", "oversized", "truncated"])
def test_bad_responses_close_connection_without_resubmission(tmp_path, mode):
    async def run():
        transport, trace = link(tmp_path, mode)
        try:
            await transport.connect()
            with pytest.raises(IdentityMismatch if mode == "identity" else ProtocolError):
                await transport.query(fixtures()["terminal_query"][32:])
            assert transport._process is None
            assert [json.loads(line)["operation"] for line in trace.read_text().splitlines()] == [6, 3]
        finally:
            await transport.close()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["timeout", "partial"])
def test_timeout_requires_explicit_reconnect_and_query(tmp_path, mode):
    async def run():
        transport, trace = link(tmp_path, mode, timeout=0.5)
        try:
            await transport.connect()
            with pytest.raises(asyncio.TimeoutError):
                await transport.submit(PreparedCommand(fixtures()["submit"][32:], fixtures()["terminal_query"][32:]))
            with pytest.raises(TransportError, match="Connect"):
                await transport.query(fixtures()["terminal_query"][32:])
            assert [json.loads(line)["operation"] for line in trace.read_text().splitlines()] == [6, 0, 2]
        finally:
            await transport.close()

    asyncio.run(run())


def test_task_cancellation_closes_inflight_connection_without_native_ack(tmp_path, monkeypatch):
    spawn = asyncio.create_subprocess_exec

    async def spawn_with_signal(*args, **kwargs):
        kwargs["stderr"] = asyncio.subprocess.PIPE
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_with_signal)

    async def run():
        transport, trace = link(tmp_path, "timeout")
        try:
            await transport.connect()
            signals = transport._process.stderr
            assert (await signals.readline()).strip() == b"6"
            task = asyncio.create_task(transport.query(fixtures()["terminal_query"][32:]))
            assert (await signals.readline()).strip() == b"3"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert transport._process is None
            assert len(trace.read_text().splitlines()) == 2
        finally:
            await transport.close()

    asyncio.run(run())


def test_reconnect_queries_original_reference_after_generation_change(tmp_path):
    async def run():
        transport, trace = link(tmp_path)
        reference = fixtures()["terminal_query"][32:]
        await transport.connect()
        await transport.close()
        transport.scope["generation"] += 1
        try:
            await transport.connect()
            result = await transport.query(reference)
            assert result.response["reply"]["reference"] == codec.decode("command_reference", reference)
            assert [json.loads(line)["operation"] for line in trace.read_text().splitlines()] == [6, 6, 3]
        finally:
            await transport.close()

    asyncio.run(run())


def test_read_identity_and_stored_reference_are_checked_before_send(tmp_path):
    async def run():
        transport, trace = link(tmp_path)
        try:
            await transport.connect()
            ref = codec.decode("command_reference", fixtures()["terminal_query"][32:])
            envelope = {key: ref[key] for key in ("scope", "namespace_id", "payload_revision")}
            envelope.update(command_type=12, save_binding=bytes(16))
            result = await transport.read(envelope, codec.encode_variant(12, 1, 0, {}))
            assert result["observation"]["namespace_id"] == [0] * 65
            bad = dict(ref, command_id="different")
            with pytest.raises(IdentityMismatch):
                await transport.submit(
                    PreparedCommand(fixtures()["submit"][32:], codec.encode("command_reference", bad))
                )
            assert [json.loads(line)["operation"] for line in trace.read_text().splitlines()] == [6, 1, 0]
        finally:
            await transport.close()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["normal", "evidence"])
def test_evidence_is_negotiated_per_pair_and_cleared_on_disconnect(tmp_path, mode):
    async def run():
        transport, trace = link(tmp_path, mode)
        try:
            await transport.connect()
            assert all(transport.supports(command) == (mode == "evidence") for command in (53, 54, 55))
            assert not transport.supports(53, 2)
            for command, payload in ((53, dict(currency=4)), (54, {}), (55, dict(stage_id=2))):
                result = await transport.observe(command, b"a" * 64, bytes([9]) + bytes(15), **payload)
                assert (result is not None) == (mode == "evidence")
            operations = [json.loads(line)["operation"] for line in trace.read_text().splitlines()]
            assert operations == ([6, 1, 1, 1] if mode == "evidence" else [6])
        finally:
            await transport.close()
        assert not transport.capabilities
    asyncio.run(run())
