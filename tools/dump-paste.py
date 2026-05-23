#!/usr/bin/env python3
"""
dump-paste.py — capture what ghostty actually sends on a paste action.

Run in ghostty, paste, then Ctrl-C. Dumps every byte received as a hexdump.

This tells us what real, native ghostty pastes look like — so we can mimic
the same byte sequence in claude-wrap instead of our current placeholder.
"""
import os
import select
import signal
import sys
import termios
import time
import tty


def main():
    fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    old = termios.tcgetattr(fd)
    tty.setraw(fd)

    def restore(_s=None, _f=None):
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        os.close(fd)

    def hexdump(b, max_bytes=4096):
        shown = b[:max_bytes]
        out = []
        for i in range(0, len(shown), 16):
            row = shown[i:i + 16]
            hexs = " ".join(f"{x:02x}" for x in row)
            asc = "".join(chr(x) if 32 <= x < 127 else "." for x in row)
            out.append(f"  {i:04x}  {hexs:<48}  {asc}")
        if len(b) > max_bytes:
            out.append(f"  ... ({len(b) - max_bytes} more)")
        return "\n".join(out)

    os.write(fd, b"\r\nReady. Paste with Cmd+V, then press Enter to dump+exit.\r\n")
    buf = bytearray()
    try:
        while True:
            r, _, _ = select.select([fd], [], [], 60)
            if not r:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf.extend(chunk)
            # echo bytes count to user
            os.write(fd, f"\r\n[+{len(chunk)}B total {len(buf)}]\r\n".encode())
            # exit on plain Enter (CR or LF) when buffer is small (user hit enter alone)
            if chunk in (b"\r", b"\n", b"\r\n"):
                break
    finally:
        restore()
    # Now print full hexdump to stderr (which is back to cooked mode)
    sys.stderr.write(f"\n=== {len(buf)} bytes captured ===\n")
    sys.stderr.write(hexdump(buf) + "\n")
    # Try to highlight key markers
    markers = [
        (b"\x1b[200~", "BRACKETED-PASTE START"),
        (b"\x1b[201~", "BRACKETED-PASTE END"),
        (b"\x1b]5522", "OSC 5522"),
        (b"\x1b]52;", "OSC 52"),
        (b"\x16", "Ctrl+V"),
    ]
    for m, name in markers:
        idx = buf.find(m)
        if idx != -1:
            sys.stderr.write(f"  found {name!r} at offset {idx}\n")


if __name__ == "__main__":
    main()
