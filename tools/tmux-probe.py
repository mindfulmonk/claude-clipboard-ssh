#!/usr/bin/env python3
"""
tmux-probe.py — inspect what reaches a pane on paste, for the OSC 5522 bridge.

Two jobs, picked by mode:

ACTIVE (default) — "does OSC 5522 survive a tmux round-trip?"
    Enables mode 5522 itself (wrapping the enable in tmux passthrough when
    $TMUX is set) and captures a paste. Tells you whether ghostty's OSC 5522
    paste packets reach this pane through tmux. Answer in practice: only via
    a flaky split-read leak, so the in-band path is not dependable.

PASSIVE (--passive) — "is the tmux-wrap bridge working?"
    Does NOT enable mode 5522 and does NOT disable it on exit. Run this INSIDE
    a `tmux-wrap` session: the wrapper owns mode 5522 between ghostty and tmux,
    so this probe must not touch it. (The active mode's `CSI?5522l` on exit
    would turn the wrapper's mode off — which is exactly what made earlier
    tests temperamental.) Passive mode just watches what the wrapper injects
    into the pane and classifies it.

What you'll see in PASSIVE mode under a working tmux-wrap:
  - image paste -> a lone Ctrl+V (\\x16): the wrapper grabbed the image above
    tmux, stashed the bytes in the clipboard cache / NSPasteboard, and fired a
    paste keystroke. That is the bridge working.
  - text paste  -> a bracketed paste (ESC[200~ ... ESC[201~).
  - raw OSC 5522 -> the wrapper did NOT consume it (you're not actually inside
    a live tmux-wrap, or it's plain tmux leaking packets).

Usage:
  ./tmux-probe.py             # active in-band feasibility test
  ./tmux-probe.py --no-wrap   # active, but don't tmux-wrap the enable (A/B)
  ./tmux-probe.py --passive   # observe only; use INSIDE a tmux-wrap session

Active mode prereq inside tmux:  tmux set -g allow-passthrough on
"""
import os
import select
import sys
import termios
import time
import tty

IDLE_TIMEOUT = 1.5        # stop once bytes go quiet for this long
FIRST_TIMEOUT = 60.0      # how long to wait for the first byte (you paste)


def tmux_wrap(seq: bytes) -> bytes:
    """Wrap an escape sequence so tmux forwards it to the outer terminal.
    Every ESC (0x1b) in the payload must be doubled; framed by ESC P tmux;
    ... ESC \\."""
    return b"\x1bPtmux;" + seq.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"


def hexdump(b: bytes, max_bytes: int = 2048) -> str:
    shown = b[:max_bytes]
    out = []
    for i in range(0, len(shown), 16):
        row = shown[i:i + 16]
        hexs = " ".join(f"{x:02x}" for x in row)
        asc = "".join(chr(x) if 32 <= x < 127 else "." for x in row)
        out.append(f"  {i:04x}  {hexs:<48}  {asc}")
    if len(b) > max_bytes:
        out.append(f"  ... ({len(b) - max_bytes} more bytes)")
    return "\n".join(out)


def detect_terminal() -> str:
    if os.environ.get("GHOSTTY_RESOURCES_DIR") or "ghostty" in os.environ.get("TERM_PROGRAM", "").lower():
        return "ghostty"
    if os.environ.get("KITTY_WINDOW_ID") or os.environ.get("KITTY_PID"):
        return "kitty"
    return "unknown"


def main():
    passive = "--passive" in sys.argv
    wrap = "--no-wrap" not in sys.argv
    in_tmux = bool(os.environ.get("TMUX"))
    term = detect_terminal()

    fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    old = termios.tcgetattr(fd)
    tty.setraw(fd)

    def emit(seq: bytes):
        out = tmux_wrap(seq) if (in_tmux and wrap) else seq
        os.write(fd, out)

    def restore():
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        os.close(fd)

    if passive:
        mode_desc = "PASSIVE (observe only; mode 5522 left untouched)"
    elif in_tmux and wrap:
        mode_desc = "ACTIVE, enable via tmux-passthrough"
    else:
        mode_desc = "ACTIVE, enable raw"
    banner = (
        f"\r\ntmux-probe: TMUX={'yes' if in_tmux else 'no'} term={term}\r\n"
        f"mode: {mode_desc}\r\n"
        f"PASTE now (Cmd+V), then press Enter when done.\r\n"
    )
    os.write(fd, banner.encode())

    if not passive:
        emit(b"\x1b[?5522h")    # CSI ? 5522 h — switch terminal to 5522 paste mode

    buf = bytearray()
    try:
        while True:
            wait = IDLE_TIMEOUT if buf else FIRST_TIMEOUT
            r, _, _ = select.select([fd], [], [], wait)
            if not r:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf.extend(chunk)
            os.write(fd, f"\r\n[+{len(chunk)}B total {len(buf)}]\r\n".encode())
            if chunk in (b"\r", b"\n", b"\r\n"):
                break
    finally:
        if not passive:
            emit(b"\x1b[?5522l")   # leave 5522 mode (only if we enabled it)
        restore()

    sys.stderr.write(f"\n=== {len(buf)} bytes captured ===\n")
    sys.stderr.write(hexdump(buf) + "\n\n")

    markers = [
        (b"\x1b]5522", "OSC 5522 paste-event"),
        (b"\x16", "Ctrl+V (\\x16)"),
        (b"\x1b[200~", "bracketed-paste START"),
        (b"\x1b[201~", "bracketed-paste END"),
        (b"\x1b]52;", "OSC 52"),
        (b"\x1bPtmux;", "tmux passthrough DCS (echoed back!)"),
    ]
    found = {}
    for m, name in markers:
        idx = buf.find(m)
        found[name] = idx
        if idx != -1:
            sys.stderr.write(f"  found {name} at offset {idx}\n")

    sys.stderr.write("\n--- VERDICT ---\n")
    if found["OSC 5522 paste-event"] != -1:
        if passive:
            sys.stderr.write(
                "  Raw OSC 5522 reached this pane — nothing upstream consumed\n"
                "  it. You're either not inside a live tmux-wrap, or it's plain\n"
                "  tmux leaking packets through (a split-read fluke). A working\n"
                "  tmux-wrap would have eaten these and injected Ctrl+V instead.\n")
        else:
            sys.stderr.write(
                "  OSC 5522 packets reached the pane. In-band is observable —\n"
                "  but it's a flaky split-read leak, not dependable. Use\n"
                "  tmux-wrap (out-of-band) for real use.\n")
    elif found["Ctrl+V (\\x16)"] != -1:
        sys.stderr.write(
            "  Ctrl+V injected — an upstream tmux-wrap intercepted the paste\n"
            "  ABOVE this pane and fired a paste keystroke. The bridge is\n"
            "  WORKING: the real bytes are in the clipboard cache / NSPasteboard\n"
            "  and Claude's Ctrl+V flow picks them up.\n")
    elif found["bracketed-paste START"] != -1:
        sys.stderr.write(
            "  A bracketed paste arrived (text). Under tmux-wrap that's a text\n"
            "  paste being translated; bare, it's a normal paste with mode 5522\n"
            "  not engaged.\n")
    else:
        sys.stderr.write("  Inconclusive — see hexdump above.\n")


if __name__ == "__main__":
    main()
