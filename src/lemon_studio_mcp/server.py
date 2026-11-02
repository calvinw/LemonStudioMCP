"""lemon-studio-mcp: an MCP server that drives Lemon Studio over its control socket.

Run with:
    python -m lemon_studio_mcp                                   # stdio (Claude Code)
    python -m lemon_studio_mcp --transport http --host 0.0.0.0   # streamable HTTP on :8000 (Claude.ai via a tunnel)
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from .client import LemonClient, LemonError, socket_path

logging.basicConfig(level=os.environ.get("LEMON_MCP_LOG_LEVEL", "INFO").upper(), stream=sys.stderr,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lemon_studio_mcp")

client = LemonClient(name=os.environ.get("LEMON_CLIENT_NAME", "Claude"))

Unit = Literal["beats", "ticks"]

INSTRUCTIONS = """\
Controls Lemon Studio, an open-source FL Studio-style DAW, directly over its control socket.

How the project works:
- Things have ids (channels, patterns, mixer tracks, clips, effect slots). Read them with get_project_state;
  ids stay the same when other things are added, moved or deleted.
- Positions and lengths are in beats (quarter notes) by default; pass unit="ticks" for raw ticks
  (get_project_state's `ppq` ticks per beat).
- Note keys are MIDI numbers: 60 = middle C (Lemon Studio, like FL, names it C5). Velocity 0-128 (default 100).
- To hear notes: a channel needs a sound. add_channel with a plugin from list_plugins (e.g. "Lemon Trio", a synth),
  or with a sample file. A channel added with neither is an empty sampler and plays nothing.
- A song is patterns placed in the playlist (place_pattern). play(mode="pattern") loops the current pattern;
  play(mode="song") plays the playlist.
- Every edit tool is one undo step named after this client; the user can undo it with Cmd+Z. apply_commands runs
  several raw commands as one step, all or nothing.
If a call says Lemon Studio is not listening, call lemon_launch.
"""


def _ppq() -> int:
    return int(client.call("state")["ppq"])


def _to_ticks(v: float | None, unit: Unit, ppq: int) -> int | None:
    if v is None:
        return None
    return int(round(v * ppq)) if unit == "beats" else int(round(v))


def _from_ticks(t: float | None, unit: Unit, ppq: int) -> float | None:
    if t is None:
        return None
    return round(t / ppq, 6) if unit == "beats" else t


def _edit(name: str, *commands: dict[str, Any]) -> list[Any]:
    return client.apply([{k: v for k, v in c.items() if v is not None} for c in commands], name=name)


def _binary() -> str | None:
    env = os.environ.get("LEMON_STUDIO_BIN")
    if env:
        return env
    return shutil.which("lemonstudio")


def build_app() -> MCPServer:
    mcp = MCPServer("lemon-studio", instructions=INSTRUCTIONS)

    # ---- connection -----------------------------------------------------------------------------------------
    @mcp.tool()
    def lemon_launch(project: str | None = None, headless: bool = False) -> dict:
        """Start Lemon Studio listening on the control socket (does nothing if it already is).

        project: an .flp file to open. headless: run with no window (--serve) instead of the app with its window.
        """
        if client.connected():
            return {"running": True, "already": True, "socket": client.where}
        if client.port:
            raise LemonError("LEMON_PORT is set: start Lemon Studio yourself with --control <port>")
        exe = _binary()
        if not exe:
            raise LemonError("Lemon Studio's binary was not found: set LEMON_STUDIO_BIN or put `lemonstudio` on PATH")
        path = socket_path()
        args = [exe, "--serve" if headless else "--control", path]
        if project:
            args.append(str(Path(project).expanduser()))
        log_path = Path(os.environ.get("TMPDIR", "/tmp")) / "lemon-studio-mcp-app.log"
        with open(log_path, "ab") as out:
            subprocess.Popen(args, stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + 45
        while time.time() < deadline:
            time.sleep(0.5)
            if client.connected():
                return {"running": True, "socket": path, "log": str(log_path)}
        raise LemonError(f"Lemon Studio did not start listening at {path} within 45 s; see {log_path}")

    @mcp.tool()
    def lemon_status() -> dict:
        """Whether Lemon Studio is connected, the app's version, this client's permission level, and the transport."""
        transport = client.call("transport")
        return {"socket": client.where, "hello": client.hello, "transport": transport}

    # ---- reading --------------------------------------------------------------------------------------------
    @mcp.tool()
    def get_project_state() -> dict:
        """The whole project: tempo, ppq, time signature, channels, mixer tracks (with effect slots and sends),
        patterns, the playlist's clips, the current pattern and song/pattern mode. Positions here are raw ticks."""
        return client.call("state")

    @mcp.tool()
    def get_notes(pattern: int, unit: Unit = "beats") -> dict:
        """The notes of pattern `pattern` (an id): channel id, position, length, key, velocity, pan."""
        ppq = _ppq()
        r = client.call("notes", pattern=pattern)
        for n in r["notes"]:
            n["position"] = _from_ticks(n["position"], unit, ppq)
            n["length"] = _from_ticks(n["length"], unit, ppq)
        r["unit"] = unit
        return r

    @mcp.tool()
    def list_plugins() -> dict:
        """The instrument and effect plugin names that add_channel and add_effect take."""
        return client.call("plugins")

    @mcp.tool()
    def get_plugin_params(owner: int) -> dict:
        """The parameters (id, name, value 0..1, value text, ...) of the plugin playing channel id or effect slot id `owner`."""
        return client.call("params", owner=owner)

    @mcp.tool()
    def get_history() -> dict:
        """The undo history: each step's name and whether it is undone."""
        return client.call("history")

    @mcp.tool()
    def get_changes(since: int = 0) -> dict:
        """What changed after event number `since` (0: all kept), including the user's own edits in the app, and
        `last`, the newest event number to pass next time."""
        return client.call("events", since=since)

    # ---- notes ----------------------------------------------------------------------------------------------
    @mcp.tool()
    def write_notes(pattern: int, notes: list[dict], channel: int | None = None, replace: bool = True,
                    unit: Unit = "beats", name: str = "Write notes") -> dict:
        """Write notes into pattern `pattern` (an id).

        notes: [{"channel": id, "position": 0, "length": 0.5, "key": 60, "velocity": 100, "pan": 64}, ...];
        position/length in `unit` (beats by default). `channel` fills in notes that leave theirs out.
        replace=True: these become the notes of `channel` in the pattern (or all of its notes when channel is None);
        replace=False: they are added to what is there.
        """
        ppq = _ppq()
        out = []
        for n in notes:
            n = dict(n)
            if "channel" not in n:
                if channel is None:
                    raise LemonError("each note needs a `channel`, or give `channel` for all of them")
                n["channel"] = channel
            n["position"] = _to_ticks(n.get("position", 0), unit, ppq)
            if "length" in n:
                n["length"] = _to_ticks(n["length"], unit, ppq)
            out.append(n)
        if replace:
            cmd = {"op": "set_notes", "pattern": pattern, "notes": out, "channel": channel}
        else:
            cmd = {"op": "add_notes", "pattern": pattern, "notes": out}
        _edit(name, cmd)
        return {"pattern": pattern, "written": len(out), "replaced": replace}

    @mcp.tool()
    def clear_notes(pattern: int, channel: int | None = None) -> dict:
        """Remove the notes of `channel` from pattern `pattern` (or all of its notes when channel is None)."""
        _edit("Clear notes", {"op": "set_notes", "pattern": pattern, "notes": [], "channel": channel})
        return {"pattern": pattern, "cleared": True}

    # ---- channels -------------------------------------------------------------------------------------------
    @mcp.tool()
    def add_channel(name: str | None = None, plugin: str | None = None, sample: str | None = None,
                    mixer_track: int | None = None) -> dict:
        """Add a channel played by instrument `plugin` (a name from list_plugins, e.g. "Lemon Trio"), or a sampler
        with `sample` (an audio file path) loaded. mixer_track: a mixer track id to route it to. Returns its id."""
        r = _edit("Add channel", {"op": "add_channel", "name": name, "plugin": plugin, "sample": sample, "track": mixer_track})
        return r[0]

    @mcp.tool()
    def set_channel(channel: int, name: str | None = None, color: str | None = None, volume: float | None = None,
                    pan: float | None = None, pitch: float | None = None, enabled: bool | None = None,
                    mixer_track: int | None = None) -> dict:
        """Change channel `channel` (an id): volume 0..1, pan -1..1, pitch in cents, color "#rrggbb", enabled
        (False mutes it), mixer_track (a mixer track id). Only the fields given change."""
        _edit("Set channel", {"op": "set_channel", "channel": channel, "name": name, "color": color, "volume": volume,
                              "pan": pan, "pitch": pitch, "enabled": enabled, "track": mixer_track})
        return {"channel": channel}

    @mcp.tool()
    def delete_channel(channel: int) -> dict:
        """Delete channel `channel` (an id), with its notes, clips and automation."""
        _edit("Delete channel", {"op": "delete_channel", "channel": channel})
        return {"deleted": channel}

    @mcp.tool()
    def load_sample(channel: int, path: str) -> dict:
        """Load audio file `path` into sampler channel `channel` (an empty path clears it)."""
        _edit("Load sample", {"op": "load_sample", "channel": channel, "path": str(Path(path).expanduser()) if path else ""})
        return {"channel": channel, "sample": path}

    # ---- patterns and playlist ------------------------------------------------------------------------------
    @mcp.tool()
    def add_pattern(name: str | None = None, color: str | None = None) -> dict:
        """Add a pattern. Returns its id."""
        return _edit("Add pattern", {"op": "add_pattern", "name": name, "color": color})[0]

    @mcp.tool()
    def set_pattern(pattern: int, name: str | None = None, color: str | None = None, length: float | None = None,
                    unit: Unit = "beats") -> dict:
        """Rename, recolor, or set the length of pattern `pattern` (an id). length 0 = automatic."""
        ppq = _ppq() if length else 1
        _edit("Set pattern", {"op": "set_pattern", "pattern": pattern, "name": name, "color": color,
                              "length": _to_ticks(length, unit, ppq)})
        return {"pattern": pattern}

    @mcp.tool()
    def delete_pattern(pattern: int) -> dict:
        """Delete pattern `pattern` (an id) and its playlist clips."""
        _edit("Delete pattern", {"op": "delete_pattern", "pattern": pattern})
        return {"deleted": pattern}

    @mcp.tool()
    def place_pattern(pattern: int, position: float, track: int = 1, length: float | None = None,
                      unit: Unit = "beats") -> dict:
        """Place pattern `pattern` (an id) in the playlist on track `track` (1-based) at `position`. Returns the clip id."""
        ppq = _ppq()
        return _edit("Place pattern", {"op": "add_clip", "pattern": pattern, "track": track,
                                       "position": _to_ticks(position, unit, ppq), "length": _to_ticks(length, unit, ppq)})[0]

    @mcp.tool()
    def set_clip(clip: int, position: float | None = None, track: int | None = None, length: float | None = None,
                 muted: bool | None = None, unit: Unit = "beats") -> dict:
        """Move, resize or mute playlist clip `clip` (an id)."""
        ppq = _ppq()
        _edit("Set clip", {"op": "set_clip", "clip": clip, "position": _to_ticks(position, unit, ppq), "track": track,
                           "length": _to_ticks(length, unit, ppq), "muted": muted})
        return {"clip": clip}

    @mcp.tool()
    def delete_clip(clip: int) -> dict:
        """Remove playlist clip `clip` (an id)."""
        _edit("Delete clip", {"op": "delete_clip", "clip": clip})
        return {"deleted": clip}

    # ---- mixer and plugins ----------------------------------------------------------------------------------
    @mcp.tool()
    def add_mixer_track(at: int | None = None) -> dict:
        """Add a mixer insert (at insert number `at`, else after the last). Returns its id."""
        return _edit("Add mixer track", {"op": "add_track", "at": at})[0]

    @mcp.tool()
    def set_mixer_track(track: int, name: str | None = None, color: str | None = None, volume: float | None = None,
                        pan: float | None = None, stereo: float | None = None, enabled: bool | None = None,
                        effects_enabled: bool | None = None) -> dict:
        """Change mixer track `track` (an id): volume 0..1 (0.8 = unity), pan and stereo -1..1, enabled (False mutes)."""
        _edit("Set mixer track", {"op": "set_track", "track": track, "name": name, "color": color, "volume": volume,
                                  "pan": pan, "stereo": stereo, "enabled": enabled, "effects_enabled": effects_enabled})
        return {"track": track}

    @mcp.tool()
    def route_mixer(from_track: int, to_track: int, level: float | None = 0.8) -> dict:
        """Send mixer track `from_track` to `to_track` (ids) at `level` 0..1; level None removes the route."""
        _edit("Route mixer", {"op": "set_send", "from": from_track, "to": to_track, "level": level})
        return {"from": from_track, "to": to_track, "level": level}

    @mcp.tool()
    def set_eq(track: int, band: int, gain_db: float | None = None, frequency: int | None = None,
               width: int | None = None) -> dict:
        """Set band 0..2 of mixer track `track`'s built-in 3-band EQ: gain in dB (-18..18), frequency and width as
        stored (0..65536)."""
        gain = None if gain_db is None else int(round(gain_db * 100))
        _edit("Mixer EQ", {"op": "set_eq", "track": track, "band": band, "gain": gain, "frequency": frequency, "width": width})
        return {"track": track, "band": band}

    @mcp.tool()
    def add_effect(track: int, plugin: str, slot: int | None = None) -> dict:
        """Add effect `plugin` (a name from list_plugins) to mixer track `track` (an id), in slot 0..9 or the first
        free one. Returns the slot plugin's id (for get_plugin_params / set_plugin_param)."""
        return _edit("Add effect", {"op": "add_effect", "track": track, "plugin": plugin, "slot": slot})[0]

    @mcp.tool()
    def set_effect(track: int, slot: int, enabled: bool | None = None, mix: float | None = None) -> dict:
        """Turn effect slot `slot` (0..9) of mixer track `track` (an id) on or off, or set its mix 0..1."""
        _edit("Set effect", {"op": "set_slot", "track": track, "slot": slot, "enabled": enabled, "mix": mix})
        return {"track": track, "slot": slot}

    @mcp.tool()
    def remove_effect(track: int, slot: int) -> dict:
        """Remove the effect in slot `slot` (0..9) of mixer track `track` (an id)."""
        _edit("Remove effect", {"op": "remove_effect", "track": track, "slot": slot})
        return {"track": track, "slot": slot}

    @mcp.tool()
    def set_plugin_param(owner: int, param: int, value: float) -> dict:
        """Set parameter `param` (an id from get_plugin_params) of the plugin of channel or slot `owner` to `value` 0..1."""
        _edit("Plugin parameter", {"op": "set_param", "owner": owner, "param": param, "value": value})
        return {"owner": owner, "param": param, "value": value}

    # ---- project settings -----------------------------------------------------------------------------------
    @mcp.tool()
    def set_tempo(bpm: float) -> dict:
        """Set the tempo (10..522 BPM)."""
        _edit("Tempo", {"op": "set_tempo", "bpm": bpm})
        return {"bpm": bpm}

    @mcp.tool()
    def set_time_signature(numerator: int, denominator: int) -> dict:
        """Set the time signature (denominator 2, 4, 8 or 16)."""
        _edit("Time signature", {"op": "set_time_signature", "numerator": numerator, "denominator": denominator})
        return {"numerator": numerator, "denominator": denominator}

    @mcp.tool()
    def set_project_settings(swing: float | None = None, main_volume: float | None = None,
                             main_pitch: int | None = None, title: str | None = None, author: str | None = None,
                             genre: str | None = None, comments: str | None = None) -> dict:
        """Swing 0..1, main volume 0..1 (0.8 = unity), main pitch in cents, and the song info."""
        cmds: list[dict[str, Any]] = []
        if swing is not None:
            cmds.append({"op": "set_swing", "swing": swing})
        if main_volume is not None:
            cmds.append({"op": "set_main_volume", "volume": main_volume})
        if main_pitch is not None:
            cmds.append({"op": "set_main_pitch", "cents": main_pitch})
        if any(v is not None for v in (title, author, genre, comments)):
            cmds.append({"op": "set_info", "title": title, "author": author, "genre": genre, "comments": comments})
        if not cmds:
            raise LemonError("give at least one setting")
        _edit("Project settings", *cmds)
        return {"changed": len(cmds)}

    # ---- transport ------------------------------------------------------------------------------------------
    @mcp.tool()
    def play(mode: Literal["pattern", "song"] | None = None, pattern: int | None = None,
             from_position: float | None = None, unit: Unit = "beats") -> dict:
        """Start playback, as the Play button (resumes when paused).

        mode: "pattern" loops the current pattern, "song" plays the playlist (default: the app's PAT/SONG switch).
        pattern: make this pattern (an id) the current one. from_position: where to start.
        """
        ppq = _ppq() if from_position is not None else 1
        return client.call("play", mode=mode, pattern=pattern, **{"from": _to_ticks(from_position, unit, ppq)})

    @mcp.tool()
    def pause() -> dict:
        """Pause playback in place (play resumes)."""
        return client.call("pause")

    @mcp.tool()
    def stop() -> dict:
        """Stop playback and rewind to where it started."""
        return client.call("stop")

    @mcp.tool()
    def transport_status(unit: Unit = "beats") -> dict:
        """Playing, paused or stopped; pattern or song mode; the current pattern; the playhead position; the tempo."""
        t = client.call("transport")
        t["position"] = _from_ticks(t.pop("tick"), unit, int(t["ppq"]))
        t["unit"] = unit
        return t

    # ---- undo, save, raw commands ---------------------------------------------------------------------------
    @mcp.tool()
    def undo() -> dict:
        """Undo the last step (anyone's: this client's or the user's)."""
        return client.call("undo")

    @mcp.tool()
    def redo() -> dict:
        """Redo the last undone step."""
        return client.call("redo")

    @mcp.tool()
    def save_project(path: str | None = None) -> dict:
        """Save the project (to `path`, else its own file). With the app's window open, the user is asked first."""
        return client.call("save", path=str(Path(path).expanduser()) if path else None)

    @mcp.tool()
    def apply_commands(commands: list[dict], name: str = "Edit") -> dict:
        """Run raw control-plane commands (Lemon Studio docs/control-plane.md) as ONE undo step, all or nothing.
        Positions and lengths here are raw ticks. Each command is {"op": ..., fields...}, e.g.
        {"op": "add_channel", "name": "Bass", "plugin": "Lemon Trio"}. Returns each command's result."""
        return {"results": client.apply(commands, name=name)}

    @mcp.tool()
    def propose_commands(commands: list[dict], name: str = "Proposal") -> dict:
        """Like apply_commands, but Lemon Studio asks the user to Apply or Discard. Returns the proposal number and
        what it would change."""
        return client.call("propose", commands=commands, name=name)

    # ---- rendering ------------------------------------------------------------------------------------------
    @mcp.tool()
    def render_project(project: str, out: str, seconds: float | None = None, rate: int | None = None,
                       builtin_only: bool = False) -> dict:
        """Render a saved .flp project to an audio file (.wav), with no window and no audio device. Save the open
        project first (save_project) to render what is in the app."""
        exe = _binary()
        if not exe:
            raise LemonError("Lemon Studio's binary was not found: set LEMON_STUDIO_BIN or put `lemonstudio` on PATH")
        args = [exe, "--render", str(Path(project).expanduser()), str(Path(out).expanduser())]
        if rate:
            args += ["--rate", str(rate)]
        if seconds:
            args += ["--seconds", str(seconds)]
        if builtin_only:
            args.append("--builtin-only")
        r = subprocess.run(args, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise LemonError(f"render failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-800:]}")
        return {"out": str(Path(out).expanduser()), "output": (r.stdout + r.stderr).strip()[-800:]}

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Lemon Studio MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="host for HTTP (0.0.0.0 for a tunnel)")
    parser.add_argument("--port", type=int, default=8000, help="port for HTTP")
    args = parser.parse_args()
    app = build_app()
    if args.transport == "http":
        app.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        app.run()


if __name__ == "__main__":
    main()
