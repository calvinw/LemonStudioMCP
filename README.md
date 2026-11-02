# LemonStudioMCP

An MCP server that lets Claude (or any MCP client) make music in
[Lemon Studio](https://github.com/ItsAlphaNeon/Lemon-Studio), the open-source FL Studio-style DAW written in Rust.

It follows [MacFLStudioMCP](https://github.com/calvinw/MacFLStudioMCP), but talks to the app **directly**. On the
Mac, FL Studio's Python blocks sockets, so MacFLStudioMCP goes through JSON files, an IAC MIDI port, a piano-roll
script and a `Cmd+Opt+Y` keystroke. Lemon Studio has a control socket built in, so this server sends each request
straight to the app and reads the answer on the same connection:

```
Claude ──MCP──▶ lemon-studio-mcp ──JSON-RPC over /tmp/lemon.sock──▶ Lemon Studio
```

Nothing to install inside the app, no MIDI setup, no Accessibility permission, no polling.

Every edit is **one undo step** named after the client ("Claude: Write notes"), so you can undo it with Cmd+Z, and a
batch of commands is applied all or nothing.

## Requirements

- macOS (Linux works too), Python 3.10+
- Lemon Studio built from source, **with the `play` / `pause` / `stop` / `transport` control methods**. Until they
  are upstream, build it from the `control-plane-transport` branch of
  [calvinw/Lemon-Studio](https://github.com/calvinw/Lemon-Studio/tree/control-plane-transport):

  ```bash
  git clone -b control-plane-transport https://github.com/calvinw/Lemon-Studio.git
  cd Lemon-Studio && cargo build -p os-app --release
  ```

  Upstream Lemon Studio works too, without the transport tools (`play`, `pause`, `stop`, `transport_status`).

## Install

```bash
cd LemonStudioMCP
./install.sh /path/to/Lemon-Studio/target/release/lemonstudio
```

The installer creates `.venv`, installs the package, and registers the server with Claude Code (`claude mcp add`)
with `LEMON_STUDIO_BIN` pointing at the app so Claude can start it.

## Use

Start Lemon Studio listening on the socket:

```bash
lemonstudio --control /tmp/lemon.sock            # with its window
lemonstudio --serve /tmp/lemon.sock song.flp     # no window
```

or just ask Claude: the `lemon_launch` tool starts it. Then, for example: *"Make a 4-bar lo-fi beat at 85 BPM with a
Lemon Trio bass line and play it."*

### Claude.ai (remote connector)

As with MacFLStudioMCP, run the server over HTTP and tunnel it:

```bash
.venv/bin/python -m lemon_studio_mcp --transport http --port 8000 --host 0.0.0.0
cloudflared tunnel --url http://localhost:8000
```

Add `https://<random>.trycloudflare.com/mcp` as a custom connector at claude.ai/customize/connectors. Only the MCP
server is exposed; the app's socket stays local. The tunnel URL gives anyone who has it control of the app, so stop
the tunnel when you are done.

## Settings (environment)

| Variable | Default | Meaning |
|---|---|---|
| `LEMON_SOCKET` | `/tmp/lemon.sock` | The app's control socket |
| `LEMON_PORT`, `LEMON_TOKEN` | | Use a local TCP port instead (the app prints the token) |
| `LEMON_STUDIO_BIN` | `lemonstudio` on PATH | The app, for `lemon_launch` and `render_project` |
| `LEMON_CLIENT_NAME` | `Claude` | The name on undo steps |

To have every change confirmed in the app, start Lemon Studio with `--control-level propose` and ask Claude to use
`propose_commands`.

## Tools

| Area | Tools |
|---|---|
| Connection | `lemon_launch`, `lemon_status` |
| Reading | `get_project_state`, `get_notes`, `list_plugins`, `get_plugin_params`, `get_history`, `get_changes` |
| Notes | `write_notes` (replace or add), `clear_notes` |
| Channels | `add_channel`, `set_channel`, `delete_channel`, `load_sample` |
| Patterns, playlist | `add_pattern`, `set_pattern`, `delete_pattern`, `place_pattern`, `set_clip`, `delete_clip` |
| Mixer, plugins | `add_mixer_track`, `set_mixer_track`, `route_mixer`, `set_eq`, `add_effect`, `set_effect`, `remove_effect`, `set_plugin_param` |
| Project | `set_tempo`, `set_time_signature`, `set_project_settings`, `save_project`, `render_project` |
| Transport | `play`, `pause`, `stop`, `transport_status` |
| Undo, raw | `undo`, `redo`, `apply_commands`, `propose_commands` |

Positions and lengths are in beats by default (`unit="ticks"` for raw ticks). Note keys are MIDI numbers, 60 =
middle C.

## How the MacFLStudioMCP pieces map

| MacFLStudioMCP | Here |
|---|---|
| `bridge_client.py` (file bus, 20 ms polling) | `client.py` (one socket, request/answer per line) |
| `device_FLStudioMCP.py` in FL's Hardware folder, IAC Driver port | nothing: the control plane is part of the app |
| `ComposeWithLLM.pyscript` + `keystroke.py` (Cmd+Opt+Y) for notes | `set_notes` / `add_notes` commands |
| per-call FL API wrappers | `state` once, then commands by stable id |

## Lemon Studio changes

The control plane had no transport control. Four methods were added to Lemon Studio in the
[`control-plane-transport`](https://github.com/calvinw/Lemon-Studio/tree/control-plane-transport) branch of the fork
(`crates/os-app/src/control/rpc.rs`, with tests, documented in `docs/control-plane.md`). They are not yet proposed
upstream.

| Method | Level | |
|---|---|---|
| `transport` | read | state (playing / paused / stopped), mode, current pattern, position, ppq, bpm |
| `play` | edit | `mode`, `pattern`, `from` optional; as the Play button |
| `pause`, `stop` | edit | as the Pause and Stop buttons |

## Not yet

What Lemon Studio's control plane doesn't offer yet: automation clips, markers, presets, channel envelopes, note
fields beyond key/velocity/pan, rendering the open project without saving it first, and pushed change events (the
`get_changes` tool polls).

Found while using it:

- **No way to select a pattern or channel.** `play(pattern=...)` followed by `stop` makes a pattern current; nothing
  chooses the channel the piano roll shows. A `select` method is the next thing to add.
- **No presets.** Instrument sounds are picked by hand in each plugin's window.

## Tests

```bash
LEMON_STUDIO_BIN=/path/to/lemonstudio .venv/bin/pytest -v
```

They start Lemon Studio headless on a temporary socket and drive it through a real MCP client over stdio.
