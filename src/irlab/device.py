"""기기(irlab 펌웨어)와의 시리얼 통신.

pyserial 을 안 쓰고 stdlib termios 로 직접 연다 — 현장에서 의존성 하나라도 줄이려고.
asyncio 의 add_reader 로 fd 를 감시하므로 스레드가 없다.

펌웨어가 내는 '@' 줄만 이벤트로 올린다. 사람용 줄은 raw 콜백으로 따로 넘긴다.
"""

from __future__ import annotations

import asyncio
import glob
import os
import termios
from collections.abc import Callable

BAUD = 115200

# ESP32-C3 는 네이티브 USB CDC 라 보통 usbmodem 으로 뜬다.
PORT_GLOBS = [
    "/dev/cu.usbmodem*",
    "/dev/cu.wchusbserial*",
    "/dev/cu.SLAB_USBtoUART*",
    "/dev/cu.usbserial*",
    "/dev/ttyACM*",
    "/dev/ttyUSB*",
]


def find_ports() -> list[str]:
    out: list[str] = []
    for g in PORT_GLOBS:
        out.extend(sorted(glob.glob(g)))
    return out


class Device:
    """열림/읽기/쓰기 + '@' 줄 파싱."""

    def __init__(self, on_event: Callable[[str, list[str]], None],
                 on_raw: Callable[[str], None]) -> None:
        self.on_event = on_event
        self.on_raw = on_raw
        self.fd: int | None = None
        self.path: str | None = None
        self._buf = b""

    # ── 연결 ──
    def open(self, path: str, baud: int = BAUD) -> None:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        iflag, oflag, cflag, lflag, _, _, cc = termios.tcgetattr(fd)
        iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
                   | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON)
        oflag &= ~termios.OPOST
        lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON
                   | termios.ISIG | termios.IEXTEN)
        cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
        cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
        speed = getattr(termios, f"B{baud}")
        cc = list(cc)
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, speed, speed, cc])
        self.fd, self.path = fd, path
        asyncio.get_running_loop().add_reader(fd, self._readable)

    def close(self) -> None:
        if self.fd is None:
            return
        try:
            asyncio.get_running_loop().remove_reader(self.fd)
        except Exception:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = self.path = None

    @property
    def connected(self) -> bool:
        return self.fd is not None

    # ── 읽기 ──
    def _readable(self) -> None:
        try:
            chunk = os.read(self.fd, 8192)
        except OSError:
            return
        if not chunk:
            return
        self._buf += chunk
        while b"\n" in self._buf:
            raw, _, self._buf = self._buf.partition(b"\n")
            line = raw.decode("utf-8", "replace").rstrip("\r")
            if not line:
                continue
            if line.startswith("@"):
                p = line[1:].split(",")
                self.on_event(p[0], p[1:])
            else:
                self.on_raw(line)

    # ── 쓰기 ──
    def send(self, cmd: str) -> None:
        if self.fd is not None:
            os.write(self.fd, (cmd + "\n").encode())

    def push_raw(self, timings: list[int], khz: int = 38, chunk: int = 24) -> None:
        """노트북 라이브러리의 raw 를 기기로 내려보내고 발사시킨다.

        한 줄에 다 못 담아 나눠 보낸다 — 펌웨어 줄 버퍼가 512 바이트다.
        chunk 24 × "65535," ≈ 144 바이트라 여유가 있다.
        """
        self.send(f"rawbegin {khz}")
        for i in range(0, len(timings), chunk):
            part = " ".join(str(v) for v in timings[i:i + chunk])
            self.send(f"rawdata {part}")
        self.send("rawsend")
