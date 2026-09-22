"""기기(irlab 펌웨어)와의 시리얼 통신.

pyserial 을 안 쓰고 stdlib termios 로 직접 연다 — 현장에서 의존성 하나라도 줄이려고.
asyncio 의 add_reader 로 fd 를 감시하므로 스레드가 없다.

펌웨어가 내는 '@' 줄만 이벤트로 올린다. 사람용 줄은 raw 콜백으로 따로 넘긴다.
"""

from __future__ import annotations

import asyncio
import glob
import os
import subprocess
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


def port_holders(path: str) -> list[str]:
    """이 포트를 열어둔 **다른** 프로세스들을 사람이 읽을 형태로 돌려준다.

    ⚠️ 여기서 중요한 사실 하나 — **macOS 의 `/dev/cu.*` 는 배타 열기가 아니다.**
    다른 프로그램이 이미 열어두고 있어도 `open()` 은 그냥 성공한다. 대신 도착한
    바이트를 **먼저 읽는 쪽이 가져가서**, 양쪽 다 프레임을 띄엄띄엄 놓친다.
    즉 실패가 아니라 **조용한 데이터 손실**이다 — 에러도 안 나고 화면만 빈다.
    (2026-09-22 실측으로 확인. 그 전엔 "Resource busy 가 날 것"이라고 잘못 알았다.)

    그래서 열기 성공/실패와 **무관하게** 매번 물어야 한다.

    죽이지는 않는다. 남의 `pio monitor` 나 다른 사람 세션을 끄는 건 되돌릴 수 없다.
    누구인지만 알려주고 판단은 사람에게 맡긴다.
    """
    try:
        # ⚠️ `-t` 를 같이 주면 안 된다 — `-F` 를 덮어써서 PID 숫자만 나오고
        # 접두어 파싱이 통째로 빈손이 된다(2026-09-22 에 그 버그를 냈다).
        r = subprocess.run(["lsof", "-F", "cp", path],
                           capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return []            # lsof 가 없는 환경 — 진단만 포기한다

    me = str(os.getpid())
    out, pid = [], None
    for line in r.stdout.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("c") and pid and pid != me:
            out.append(f"PID {pid} {line[1:]}")
            pid = None
    return out


class Device:
    """열림/읽기/쓰기 + '@' 줄 파싱."""

    def __init__(self, on_event: Callable[[str, list[str]], None],
                 on_raw: Callable[[str], None],
                 on_lost: Callable[[str], None] | None = None) -> None:
        self.on_event = on_event
        self.on_raw = on_raw
        # 기기가 빠졌을 때 호출된다. 없으면 조용히 끊긴다.
        self.on_lost = on_lost
        self.fd: int | None = None
        self.path: str | None = None
        self._buf = b""
        # 기기 응답을 기다리는 곳. raw 전송은 ack 없이 보내면 조용히 어긋난다.
        self._ack: dict[str, asyncio.Future] = {}
        # 전송 하나가 끝나기 전에 다음이 끼어들면 ack 가 뒤섞이고, 기기 버퍼에는
        # 두 전송이 누적돼 "버퍼 초과" 로 터진다. 직렬화한다.
        self._txlock = asyncio.Lock()
        self._out = b""            # 아직 못 쓴 꼬리
        self._writing = False      # add_writer 등록 여부

    # ── 연결 ──
    def open(self, path: str, baud: int = BAUD) -> None:
        """열기 실패는 전부 OSError 로 올린다.

        termios.error 는 **OSError 의 하위가 아니다.** tty 가 아닌 경로나 뽑힌 뒤
        남은 장치 노드를 열면 여기서 termios.error 가 나는데, 부르는 쪽이
        OSError 만 잡고 있어서 앱이 시작하자마자 트레이스백으로 죽었다.
        """
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            return self._configure(fd, path, baud)
        except Exception as e:
            os.close(fd)               # 실패 경로에서 fd 가 새면 재시도가 막힌다
            if isinstance(e, termios.error):
                raise OSError(f"{path}: 시리얼 장치가 아니다 ({e})") from e
            raise

    def _configure(self, fd: int, path: str, baud: int) -> None:
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
        loop = asyncio.get_running_loop()
        try:
            loop.remove_reader(self.fd)
            if self._writing:
                loop.remove_writer(self.fd)
        except Exception:
            pass
        self._writing = False
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = self.path = None

    @property
    def connected(self) -> bool:
        return self.fd is not None

    # ── 읽기 ──
    def _lost(self, why: str) -> None:
        """기기가 사라졌다. **반드시 reader 를 떼야 한다.**

        tty 가 EOF(b"") 를 돌려주는 상태로 fd 를 등록해 두면 이벤트 루프가
        계속 '읽을 수 있음' 으로 깨워서 100% CPU 로 돌고 화면이 멈춘다.
        현장에서 USB 를 뽑는 건 정상 이벤트라 조용히 무시하면 안 된다.
        """
        path = self.path or "?"
        self._out = b""
        self.close()
        for f in self._ack.values():
            if not f.done():
                f.set_exception(RuntimeError(f"연결 끊김: {why}"))
        self._ack.clear()
        if self.on_lost:
            self.on_lost(f"{path} — {why}")

    def _readable(self) -> None:
        if self.fd is None:
            return
        try:
            chunk = os.read(self.fd, 8192)
        except BlockingIOError:
            return
        except OSError as e:
            # ENXIO/EIO = 장치가 빠졌다. 그 외도 더 읽을 수 없는 상태다.
            self._lost(os.strerror(e.errno or 0) or str(e))
            return
        if not chunk:
            self._lost("EOF (USB 분리 추정)")
            return
        self._buf += chunk
        while b"\n" in self._buf:
            raw, _, self._buf = self._buf.partition(b"\n")
            line = raw.decode("utf-8", "replace").rstrip("\r")
            if not line:
                continue
            if line.startswith("@"):
                p = line[1:].split(",")
                fut = self._ack.pop(p[0], None)
                if fut is not None and not fut.done():
                    fut.set_result(p[1:])
                if p[0] == "RAWERR":
                    for f in self._ack.values():
                        if not f.done():
                            f.set_exception(RuntimeError(",".join(p[1:])))
                    self._ack.clear()
                self.on_event(p[0], p[1:])
            else:
                self.on_raw(line)

    # ── 쓰기 ──
    def send(self, cmd: str) -> None:
        """한 줄을 **끝까지** 보낸다 — 못 쓴 꼬리는 큐에 남기고 루프가 마저 쓴다.

        두 가지를 동시에 지켜야 한다.
        ① fd 가 O_NONBLOCK 이라 os.write 는 일부만 쓰고 그 길이를 돌려준다.
           반환값을 버리면 긴 rawdata 줄이 잘려 나가고, 기기는 짧아진 줄을 정상으로
           파싱한다 — 증상은 엉뚱한 "개수 불일치" 로 나타난다.
        ② 그렇다고 여기서 블로킹하면 안 된다. 버퍼를 비우는 쪽도 같은 이벤트 루프의
           콜백이라, 버퍼가 차는 순간 서로를 기다리며 화면째 멈춘다.
           (selftest 6번에서 실제로 걸렸다.)
        """
        if self.fd is None:
            return
        self._out += (cmd + "\n").encode()
        self._flush()

    def _flush(self) -> None:
        if self.fd is None:
            return
        while self._out:
            try:
                n = os.write(self.fd, self._out)
            except BlockingIOError:
                break
            except OSError as e:
                self._lost(os.strerror(e.errno or 0) or str(e))
                return
            if n <= 0:
                break
            self._out = self._out[n:]

        loop = asyncio.get_running_loop()
        if self._out and not self._writing:
            loop.add_writer(self.fd, self._flush)
            self._writing = True
        elif not self._out and self._writing:
            loop.remove_writer(self.fd)
            self._writing = False

    def _arm(self, tag: str) -> asyncio.Future:
        """응답 대기를 **명령을 보내기 전에** 건다.

        보내고 나서 걸면 그 사이에 도착한 응답을 놓친다 — 실제로 테스트에서
        재현됐다. 등록이 먼저다.
        """
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._ack[tag] = fut
        return fut

    async def _wait(self, tag: str, fut: asyncio.Future,
                    timeout: float = 2.0) -> list[str]:
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._ack.pop(tag, None)

    async def push_raw(self, timings: list[int], khz: int = 38,
                       chunk: int = 24) -> int:
        """노트북 라이브러리의 raw 를 기기로 내려보내고 발사시킨다.

        한 줄에 다 못 담아 나눠 보낸다 — 펌웨어 줄 버퍼가 512 바이트다.
        chunk 24 × "65535," ≈ 144 바이트라 여유가 있다.

        ⚠️ **청크마다 기기가 센 누적 개수를 받아 대조한다.** ack 없이 몰아 보내면
        `rawbegin` 한 줄만 유실돼도 버퍼가 리셋되지 않아 **다음 전송이 누적되고**,
        결국 "버퍼 초과" 로 터진다 (2026-09-21 실제로 겪음). 조용히 어긋나느니
        느려도 매 줄을 확인한다.
        """
        if not timings:
            raise RuntimeError("raw 가 비어 있다")
        if self.fd is None:
            raise RuntimeError("연결 안 됨")

        async with self._txlock:
            fut = self._arm("PUSHBEG")
            self.send(f"rawbegin {khz}")
            await self._wait("PUSHBEG", fut)   # 버퍼가 실제로 비워졌는지 확인

            sent = 0
            for i in range(0, len(timings), chunk):
                part = timings[i:i + chunk]
                fut = self._arm("PUSHBUF")
                self.send("rawdata " + " ".join(str(v) for v in part))
                got = await self._wait("PUSHBUF", fut)
                sent += len(part)
                n = int(got[0]) if got and got[0].isdigit() else -1
                if n != sent:
                    raise RuntimeError(f"개수 불일치: 보낸 {sent} ≠ 기기 {n}")

            fut = self._arm("PUSHSENT")
            self.send("rawsend")
            await self._wait("PUSHSENT", fut, timeout=4.0)
            return sent
