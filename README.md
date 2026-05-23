# claude-clipboard-ssh

Image paste over SSH for Claude Code, by speaking
[OSC 5522](https://sw.kovidgoyal.net/kitty/clipboard/) to kitty or ghostty
on the user's local machine. No patches to Claude Code itself.

## What this is

Claude Code shells out to `xclip` to read the system clipboard on Linux.
That works locally, but over SSH there's no display server on the remote
box and the paste silently fails. Upstream
[claude-code#42712](https://github.com/anthropics/claude-code/issues/42712)
tracks this; it's closed as "not planned."

This repo is a two-piece userspace workaround:

- **`bin/xclip`** — a drop-in replacement for `xclip` that PATH-shadows the
  real one. It serves bytes from a small cache directory written by the
  wrapper.
- **`bin/claude-wrap`** — a PTY proxy that wraps `claude`. It enables
  `CSI ? 5522 h` on the outer terminal, intercepts the OSC 5522 paste-event
  packets ghostty/kitty send, fetches the clipboard data, writes it to the
  cache, then sends Ctrl+V to the inner `claude` so its xclip flow fires
  and the cache gets read.

Together they let Cmd+V (or your normal paste shortcut) attach screenshots
to a `claude` session that's running over SSH.

## Install

```sh
# Put both on PATH, with the local user prefix
mkdir -p ~/.local/bin
cp bin/xclip       ~/.local/bin/xclip
cp bin/claude-wrap ~/.local/bin/claude-wrap
chmod +x ~/.local/bin/xclip ~/.local/bin/claude-wrap

# Make sure ~/.local/bin is in PATH and earlier than any system xclip
echo $PATH | tr ':' '\n' | head -3
```

Optional, but recommended: silence the kitty clipboard popup. In
`~/.config/kitty/kitty.conf` on the Mac side:

```
clipboard_control write-clipboard write-primary read-clipboard read-primary
```

Reload kitty (`Ctrl+Shift+F5` or restart). Without this, kitty pops up a
confirm dialog for every paste, which gets annoying.

## Usage

SSH into your remote Linux box from a kitty or ghostty terminal on your
Mac, then:

```sh
claude-wrap
```

`claude-wrap` auto-detects ghostty/kitty and engages the OSC 5522 bridge.
On any other terminal it just `exec`s the real `claude` with zero
overhead, so it's safe to alias as `claude`:

```sh
# ~/.bashrc
alias claude=claude-wrap
```

Paste a screenshot the same way you would locally. It attaches.

## Architecture

```
[kitty OR ghostty]  ←tty→  [claude-wrap]  ←pty→  [real claude]
                              │ writes bytes
                              ↓
                       $XDG_RUNTIME_DIR/xclip-shim-<uid>/
                              ↑
                              │ reads bytes
                          [xclip stub]  ← claude shells out
```

The wrapper:

1. Detects the terminal via `$GHOSTTY_RESOURCES_DIR`, `$KITTY_WINDOW_ID`,
   `$TERM_PROGRAM`, `$TERM`. Exits to real `claude` on others.
2. `pty.fork()` real `claude` as a child.
3. Sends `CSI ? 5522 h` to the outer terminal so it switches to OSC 5522
   paste mode.
4. Runs a `select()` proxy loop forwarding bytes in both directions, with
   a state machine sniffing for OSC 5522 packets on the inbound side.
5. On a paste event:
   - Captures the password (ghostty) or notes absence (kitty)
   - Picks the highest-priority MIME from the advertisement
   - Fires the `type=read` request back, with password if present
   - Accumulates the chunked data response
   - Writes the decoded bytes to the cache directory
   - Sends `\x16` (Ctrl+V) to `claude`'s PTY

Ctrl+V is Claude Code's paste binding, so it triggers Claude's normal
xclip flow — which hits our stub, which serves from the freshly-written
cache. Image attaches.

The shim is ~170 lines and contains no OSC logic — that's all in the
wrapper. See `BLOG.md` for the longer write-up.

## Compatibility

Tested with:

- macOS host running kitty (latest)
- macOS host running ghostty with
  [PR #12030](https://github.com/ghostty-org/ghostty/pull/12030) (OSC 5522
  read-path support; not yet merged at time of writing)
- Linux remote (Arch) running Claude Code over SSH

Should work with any terminal that implements OSC 5522. Falls back gracefully
to a direct `exec` of `claude` on terminals that don't.

## Known limitations

- **Depends on Ctrl+V being Claude Code's paste binding.** This is
  [documented behavior](https://code.claude.com/docs/en/interactive-mode)
  (use Ctrl+V, not Cmd+V — the terminal eats Cmd+V before Claude sees it).
  If Anthropic ever changes the binding, the wrapper needs the keystroke
  updated.
- **Image-write to clipboard isn't supported.** `xclip -i` falls back to
  OSC 52 which is text-only. Claude Code doesn't seem to need it.
- **One MIME per paste in ghostty.** ghostty's password is single-use, so
  the wrapper fetches exactly one MIME (PNG preferred). If your clipboard
  has both an image and a URL, only the image comes through.
- **No tmux/screen support.** OSC 5522 won't pass through a multiplexer's
  byte filter by default. The wrapper would need different routing.
- **Keystrokes typed during the OSC round-trip can be eaten** in some
  edge cases involving slow clipboard popups. Set `clipboard_control` to
  drop the `-ask` modifier to minimize the window.

## Debug tools

In `tools/`:

- `probe.py` — raw OSC 5522 sender/receiver, no claude. Useful for
  exercising the protocol against any terminal.
- `ghostty-test.py` — minimal mode-5522 paste capture. Run it, paste, get
  a file. No proxy, no claude. Reproduces the protocol in isolation.
- `dump-paste.py` — dumps every byte the terminal sends on a paste. Useful
  for figuring out what trigger a TUI app actually expects.

## Why

ghostty's OSC 5522 is per-paste authenticated: the terminal hands the TUI
app a single-use token, the app has 5 seconds to use it. Closed-source
TUIs that don't speak the protocol natively can't participate in this
model — by the time the app shells out to xclip, the token's already
gone. The PTY-proxy wrapper exists to bridge that gap: it captures the
token at the user-gesture moment, on behalf of an app that doesn't know
to expect one.

kitty has a simpler ambient-capability model, but the same wrapper works
there too (with the password path skipped), so a single mechanism covers
both terminals.

See `BLOG.md` for the full debugging journey.

## License

MIT. See LICENSE.
