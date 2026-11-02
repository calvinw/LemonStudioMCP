# Development notes

How LemonStudioMCP and its Lemon Studio changes are put together, where each piece lives, and what is left to do.

## Repositories

| Repository | Branch | What it holds |
|---|---|---|
| [calvinw/LemonStudioMCP](https://github.com/calvinw/LemonStudioMCP) | `main` | This MCP server |
| [calvinw/Lemon-Studio](https://github.com/calvinw/Lemon-Studio) (fork of [ItsAlphaNeon/Lemon-Studio](https://github.com/ItsAlphaNeon/Lemon-Studio)) | `control-plane-transport` | The Lemon Studio changes this server needs (commit `ad065b7b`) |

The fork's `main` is left identical to upstream, so the change stays one branch that can later be offered upstream as a
pull request. **No pull request has been opened.**

## Local layout

```
~/develop/
├── Lemon-Studio/      clone of upstream, on branch control-plane-transport
│                        origin → ItsAlphaNeon/Lemon-Studio (upstream)
│                        fork   → calvinw/Lemon-Studio      (push here)
└── LemonStudioMCP/    this repository
                         origin → calvinw/LemonStudioMCP
```

Pushing Lemon Studio work: `git push fork control-plane-transport`. Picking up upstream changes:
`git fetch origin && git rebase origin/main` on the branch, then rebuild.

## The Lemon Studio change

Lemon Studio already has a control plane: JSON-RPC 2.0 over a local socket (`--control <socket>` beside the window,
`--serve <socket>` without one), documented in its `docs/control-plane.md`. Clients could edit a project but could not
play it or show it. The branch adds five methods to `crates/os-app/src/control/rpc.rs`:

| Method | Level | Does |
|---|---|---|
| `transport` | read | state (playing / paused / stopped / waiting), PAT or SONG mode, current pattern id, position, ppq, bpm |
| `play` | edit | as the Play button (resumes when paused); optional `mode`, `pattern` (makes it current), `from` (ticks) |
| `pause` | edit | as the Pause button |
| `stop` | edit | as the Stop button (rewinds to where playback started) |
| `select` | edit | optional `pattern` (made current), `channel` (selected), `piano_roll` (true opens, false closes); scrolls the piano roll to the channel's notes |

The transport methods go through the same `App::act(Action::Play / Stop / PatSong)` paths as the toolbar. `select`
uses the pattern picker's `patterns::select`, the channel rack's `focus_channel` and the piano roll's `center_keys`,
and checks every id before changing anything. None of them makes an undo step or an event. Tests in `crates/os-app/src/control/tests.rs`:

```bash
cargo test -p os-app --release --bin lemonstudio control::                                  # includes the transport and select tests
cargo test -p os-app --release --bin lemonstudio clients_play_pause_and_stop -- --ignored    # needs an audio device
```

Build: `cargo build -p os-app --release` → `target/release/lemonstudio`. (`cargo run -p os-app --release` also builds
and installs Lemon Studio's own VST3/CLAP plugins into `~/Library/Audio/Plug-Ins`; `cargo build` does not.)

## This server

| File | Role |
|---|---|
| `src/lemon_studio_mcp/client.py` | JSON-RPC client: one request per line over `/tmp/lemon.sock` (or a local port + token). Sends `hello` with the client name, reconnects once if the app restarted. `LemonError` subclasses the MCP SDK's `ToolError`, so Lemon Studio's messages ("no pattern 99999") reach Claude instead of a generic "Error executing tool". |
| `src/lemon_studio_mcp/server.py` | 41 tools on the MCP SDK 2.x `MCPServer`. Converts beats ↔ ticks with the project's `ppq`; every edit is one `apply` (one undo step named "Claude: …"). `lemon_launch` starts the app; `render_project` shells out to `lemonstudio --render`. |
| `tests/test_end_to_end.py` | Starts `lemonstudio --serve` on a temporary socket and drives it through a real MCP client over stdio: builds a beat, reads notes back, plays / pauses / stops, undoes, and checks error messages. |
| `install.sh` | Creates `.venv`, installs the package, registers it with Claude Code. |

Registered with Claude Code at user scope:

```bash
claude mcp add --transport stdio -s user lemon-studio \
  -e LEMON_STUDIO_BIN=$HOME/develop/Lemon-Studio/target/release/lemonstudio \
  -- $HOME/develop/LemonStudioMCP/.venv/bin/python -m lemon_studio_mcp
```

Claude Code loads new MCP servers at startup: restart it (`claude --continue` keeps the conversation) after registering.

### Remote (Claude.ai)

```bash
.venv/bin/python -m lemon_studio_mcp --transport http --port 8000 --host 0.0.0.0
cloudflared tunnel --url http://localhost:8000      # then add https://<random>.trycloudflare.com/mcp as a connector
```

`--host 0.0.0.0` matters: on `127.0.0.1` the SDK turns on DNS-rebinding protection, which rejects the tunnel's Host
header. HTTP mode was tested locally (all tools listed, calls answered); the tunnel path to Claude.ai has not been
tested yet. Start Lemon Studio with `--control-level propose` to make every change wait for Apply in the app.

## How hard it was

Not very. Lemon Studio has a socket protocol built in, so the server is a thin client over it (about 500 lines of
Python), and the Lemon Studio side is about 90 lines of Rust plus tests. Most of the work was mapping tools onto
commands and testing against the real app.

What took extra care:

- **MCP SDK 2.x** renamed `FastMCP` to `MCPServer` (host/port moved to `run()`); the server targets 2.x rather than
  pinning 1.x.
- **Hidden error messages**: SDK 2.x replaces ordinary exceptions with "Error executing tool"; the end-to-end test
  caught it and `LemonError` became a `ToolError`.
- An unmodified build of Lemon Studio was checked to confirm the app closing by itself during testing was not caused
  by these changes (it behaves the same).

## Known gaps

Found while making music with it:

- **The piano roll shows one channel in the current pattern**, which caused repeated "where are my notes?"
  confusion before `select` existed. The server's instructions now tell Claude to `select` what it just wrote.
- **No presets.** Plugin presets can't be loaded over the control plane; sounds are chosen by hand.
- **No view of the screen.** The tools can't see what the user sees (scroll position, open windows).
- From Lemon Studio's own "Not yet" list: automation clips, markers, channel envelopes, note fields beyond
  key/velocity/pan, rendering the open project without saving, pushed events (`get_changes` polls).

## Next steps

1. Test the Claude.ai path through the tunnel.
2. When the transport and select methods have settled, open a pull request from `calvinw/Lemon-Studio:control-plane-transport`
   to `ItsAlphaNeon/Lemon-Studio`.
