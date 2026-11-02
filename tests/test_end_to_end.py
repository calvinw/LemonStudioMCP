"""End to end: an MCP client calls this server over stdio, which drives a real Lemon Studio started headless
(`--serve`) on a temporary socket. Skipped unless LEMON_STUDIO_BIN names the app.

    LEMON_STUDIO_BIN=/path/to/lemonstudio .venv/bin/pytest -v
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

BIN = os.environ.get("LEMON_STUDIO_BIN")
pytestmark = pytest.mark.skipif(not BIN, reason="set LEMON_STUDIO_BIN to run against Lemon Studio")


@pytest.fixture(scope="module")
def lemon_socket():
    path = f"/tmp/lemon-mcp-test-{os.getpid()}.sock"
    proc = subprocess.Popen([BIN, "--serve", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_UNIX)
            s.connect(path)
            s.close()
            break
        except OSError:
            time.sleep(0.25)
    else:
        proc.kill()
        pytest.fail("Lemon Studio did not start listening")
    yield path
    proc.terminate()
    proc.wait(10)


def run_session(sock: str, body):
    params = StdioServerParameters(command=sys.executable, args=["-m", "lemon_studio_mcp"],
                                   env={**os.environ, "LEMON_SOCKET": sock, "LEMON_CLIENT_NAME": "Test"})

    async def main():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()

            async def call(tool, /, **args):
                res = await session.call_tool(tool, args)
                text = res.content[0].text if res.content else "null"
                if res.is_error:
                    raise RuntimeError(f"{tool}: {text}")
                return json.loads(text)

            await body(call)

    anyio.run(main)


def test_build_a_beat_and_play_it(lemon_socket):
    async def body(call):
        status = await call("lemon_status")
        assert status["hello"]["app"] == "Lemon Studio"
        assert status["hello"]["name"] == "Test"

        await call("set_tempo", bpm=96)
        ch = (await call("add_channel", name="Bass", plugin="Lemon Trio"))["channel"]
        pat = (await call("add_pattern", name="Groove"))["pattern"]
        notes = [{"position": i, "length": 0.5, "key": k} for i, k in enumerate([36, 36, 43, 41])]
        assert (await call("write_notes", pattern=pat, channel=ch, notes=notes))["written"] == 4

        got = await call("get_notes", pattern=pat)
        assert [(n["position"], n["length"], n["key"]) for n in got["notes"]] == [(0, 0.5, 36), (1, 0.5, 36), (2, 0.5, 43), (3, 0.5, 41)]
        assert all(n["channel"] == ch for n in got["notes"])

        clip = (await call("place_pattern", pattern=pat, position=0))["clip"]
        state = await call("get_project_state")
        assert state["tempo"] == 96
        assert any(c["id"] == clip and c["pattern"] == pat for c in state["playlist"]["clips"])

        # Every edit is one named undo step.
        hist = await call("get_history")
        assert any(s["name"] == "Test: write notes" for s in hist["steps"]), hist

        t = await call("play", mode="pattern", pattern=pat)
        assert (t["state"], t["mode"], t["pattern"]) == ("playing", "pattern", pat)
        time.sleep(0.5)
        t = await call("transport_status")
        assert t["state"] == "playing" and t["position"] > 0, t
        assert (await call("pause"))["state"] == "paused"
        assert (await call("play"))["state"] == "playing"
        assert (await call("stop"))["state"] == "stopped"

        t = await call("play", mode="song")
        assert (t["state"], t["mode"]) == ("playing", "song")
        assert (await call("stop"))["state"] == "stopped"

        assert (await call("undo"))["step"] == "Test: place pattern"
        state = await call("get_project_state")
        assert not any(c["id"] == clip for c in (state["playlist"] or {}).get("clips", []))

    run_session(lemon_socket, body)


def test_errors_come_back_as_tool_errors(lemon_socket):
    async def body(call):
        with pytest.raises(RuntimeError, match="no pattern"):
            await call("get_notes", pattern=99999)
        with pytest.raises(RuntimeError, match="channel"):
            await call("write_notes", pattern=1, notes=[{"position": 0, "key": 60}])

    run_session(lemon_socket, body)
