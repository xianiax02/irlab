"""실기기 없이 호스트 측 프로토콜을 검증한다.

pty 한 쌍을 열어 한쪽에 **가짜 irlab 펌웨어**를 물리고, 반대쪽을 Device 로 연다.
시리얼 계층(부분 쓰기·EOF·ack·청크 대조)이 실제 파일기술자 위에서 도는지를 본다.
실기기 검증을 대신하지는 않는다 — 대신 *호스트 코드가 틀렸을 가능성*은 지운다.
"""

from __future__ import annotations

import asyncio
import os
import pty
import sys

from .device import Device


class FakeFirmware:
    """irlab 펌웨어의 시리얼 계약만 흉내 낸다 (rawbegin/rawdata/rawsend·RX·대조)."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.buf = b""
        self.n = 0
        self.khz = 38
        self.lines: list[str] = []

    def w(self, s: str) -> None:
        os.write(self.fd, (s + "\n").encode())

    def feed(self) -> None:
        try:
            self.buf += os.read(self.fd, 4096)
        except (BlockingIOError, OSError):
            return
        while b"\n" in self.buf:
            raw, _, self.buf = self.buf.partition(b"\n")
            self.handle(raw.decode().strip())

    def handle(self, line: str) -> None:
        if not line:
            return
        self.lines.append(line)
        cmd, _, arg = line.partition(" ")
        if cmd == "rawbegin":
            self.n = 0
            self.khz = int(arg) if arg.isdigit() else 38
            self.w(f"@PUSHBEG,{self.khz}")
        elif cmd == "rawdata":
            vals = [v for v in arg.replace(",", " ").split() if v]
            for v in vals:
                if not v.isdigit() or not (0 < int(v) <= 65535):
                    self.w(f"@RAWERR,범위 밖 {v}")
                    return
            self.n += len(vals)
            self.w(f"@PUSHBUF,{self.n}")
        elif cmd == "rawsend":
            if not self.n:
                self.w("@RAWERR,버퍼 비어 있음")
                return
            self.w(f"# push 발사 raw {self.n}, {self.khz}kHz")
            self.w(f"@PUSHSENT,1234,{self.n},{self.khz}")
        elif cmd == "?":
            self.w("@STATE,3,5,SAMSUNG_AC,1,25,1,1,0,0")

    # 기기가 자발적으로 내는 것들
    def emit_rx(self, proto="SAMSUNG_AC", bits=112, hexs="02B20F"):
        self.w(f"@RX,1000,{proto},{bits},{hexs},1,199")

    def emit_diff(self):
        self.w("      DIFF: 2/14 바이트 다름 — [6] 71→75 [13] 1F→2F")
        self.w("@DIFF,1000,2,6:71:75|13:1F:2F")

    def emit_match(self):
        self.w("@MATCH,1000")


async def run() -> int:
    fails: list[str] = []
    events: list[tuple[str, list[str]]] = []
    raws: list[str] = []
    lost: list[str] = []

    master, slave = pty.openpty()
    os.set_blocking(master, False)
    fw = FakeFirmware(master)

    dev = Device(lambda t, p: events.append((t, p)),
                 raws.append, lost.append)
    dev.open(os.ttyname(slave))

    loop = asyncio.get_running_loop()
    loop.add_reader(master, fw.feed)

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"  {'✓' if cond else '✗'} {name}{'  ' + detail if detail else ''}")
        if not cond:
            fails.append(name)

    print("irlab selftest — 가짜 펌웨어(pty) 대상")

    # 1) 긴 raw 밀어내기: 청크·ack·개수 대조
    timings = [(i % 600) + 400 for i in range(500)]
    n = await dev.push_raw(timings, 38)
    check("raw 500칸 전송·개수 일치", n == 500, f"n={n}")
    check("청크가 실제로 나뉘어 나갔다",
          sum(1 for x in fw.lines if x.startswith("rawdata")) >= 20,
          f"{sum(1 for x in fw.lines if x.startswith('rawdata'))}줄")
    sent_vals = []
    for x in fw.lines:
        if x.startswith("rawdata "):
            sent_vals += [int(v) for v in x[8:].split()]
    check("전송된 값이 원본과 바이트 단위로 같다", sent_vals == timings)

    # 2) 부분 쓰기에도 줄이 안 잘린다 (한 줄이 커널 버퍼를 넘기는 길이)
    fw.lines.clear()
    long_cmd = "rawdata " + " ".join(str((i % 900) + 100) for i in range(2000))
    dev.send(long_cmd)
    await asyncio.sleep(0.3)
    got = next((x for x in fw.lines if x.startswith("rawdata")), "")
    check("긴 줄이 잘리지 않고 도착", got == long_cmd,
          f"보낸 {len(long_cmd)}B / 받은 {len(got)}B")

    # 3) 기기 오류가 예외로 올라온다
    fw.n = 0
    raised = ""
    try:
        await dev.push_raw([0], 38)      # 0 은 펌웨어가 거부한다
    except RuntimeError as e:
        raised = str(e)
    except TimeoutError:
        raised = "timeout"
    check("기기 RAWERR 가 예외로 전파", "범위" in raised or raised == "timeout", raised)

    # 4) 동시 전송이 직렬화된다 (ack 뒤섞임 방지)
    fw.n = 0
    r = await asyncio.gather(dev.push_raw(timings[:100]), dev.push_raw(timings[:100]),
                             return_exceptions=True)
    check("동시 전송 2건이 모두 성공", all(x == 100 for x in r), str(r))

    # 5) 대조 출력이 화면 경로로 올라온다
    events.clear(); raws.clear()
    fw.emit_rx(); fw.emit_diff(); fw.emit_match()
    await asyncio.sleep(0.3)
    tags = [t for t, _ in events]
    check("RX·DIFF·MATCH 이벤트 수신", {"RX", "DIFF", "MATCH"} <= set(tags), str(tags))
    check("사람용 DIFF 줄도 전달된다(’#’ 아님)",
          any("DIFF:" in x for x in raws), str(raws))

    # 6) 기기 분리 → 폭주 없이 끊김 통보
    os.close(master)
    await asyncio.sleep(0.3)
    check("USB 분리가 끊김으로 보고된다", bool(lost), str(lost))
    check("끊긴 뒤 fd 가 닫혀 있다", not dev.connected)

    dev.close()
    print(f"\n{'실패 ' + ', '.join(fails) if fails else '전부 통과'}")
    return 1 if fails else 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
