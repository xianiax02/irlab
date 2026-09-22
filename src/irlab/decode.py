"""캡처한 payload 가 **무슨 명령인지** 사람 말로 푼다.

왜 필요한가: 2026-09-22 현장에서 "turn off" 로 이름 붙여 저장한 프레임이 실제로는
**전원 켜기** 명령이었다. payload hex 만 보여주면 그걸 알 방법이 없고, 되쏴도
에어컨이 띵 소리를 내며 수락하기 때문에 **증상으로도 안 드러난다.**
저장 직후 화면에서 바로 읽히게 하려고 붙였다.

지금은 SAMSUNG_AC 만 푼다(센터 실값). 모르는 프로토콜은 None 을 돌려주고,
호출부는 그때 아무것도 주장하지 않는다 — 억지로 해석하면 그게 더 나쁘다.

필드 위치는 IRremoteESP8266 2.9.0 `ir_Samsung.h` 의 SamsungProtocol union 에서 옮겼다.
  byte6  bits4-5 = Power1      byte13 bits4-5 = Power2
  getPower() = (Power1 == 0b11 && Power2 == 0b11)
  byte11 bits4-7 = Temp + 16   byte12 bits1-3 = Fan   byte12 bits4-6 = Mode
extended(21바이트)는 sec1 + sec3 을 이어 붙이면 표준 14바이트 상태가 된다
(`IRSamsungAc::sendExtended` 가 sec2 를 sec3 으로 복사하고 고정 middle 을 끼운다).
"""

from __future__ import annotations

SAMSUNG_MIDDLE = bytes([0x01, 0xD2, 0x0F, 0x00, 0x00, 0x00, 0x00])
_MODES = {0: "Auto", 1: "Cool", 2: "Dry", 3: "Fan", 4: "Heat"}
_FANS = {0: "Auto", 2: "Low", 4: "Med", 5: "High", 7: "Turbo"}


def _popcount(x: int) -> int:
    return bin(x).count("1")


def _section_checksum(s: bytes) -> int:
    """ir_Samsung.cpp calcSectionChecksum 과 같은 계산."""
    t = _popcount(s[0])
    t += _popcount(s[1] & 0x0F)
    t += _popcount((s[2] >> 4) & 0x0F)
    t += sum(_popcount(b) for b in s[3:7])
    return t ^ 0xFF


def samsung_ac(payload: str) -> dict | None:
    try:
        b = bytes.fromhex(payload)
    except ValueError:
        return None
    if len(b) not in (14, 21):
        return None

    extended = len(b) == 21
    if extended:
        if b[7:14] != SAMSUNG_MIDDLE:
            return None                 # extended 인데 고정 middle 이 아니다 → 해석 보류
        st = b[0:7] + b[14:21]
    else:
        st = b

    ok = True
    for i, off in enumerate((0, 7)):
        sec = st[off:off + 7]
        want = ((sec[2] & 0x0F) << 4) | ((sec[1] >> 4) & 0x0F)
        if _section_checksum(sec) != want:
            ok = False

    p1 = (st[6] >> 4) & 0b11
    p2 = (st[13] >> 4) & 0b11
    return {
        "power": p1 == 0b11 and p2 == 0b11,
        "power_bits": f"{p1:02b}/{p2:02b}",
        "temp": ((st[11] >> 4) & 0x0F) + 16,
        "mode": _MODES.get((st[12] >> 4) & 0b111, "?"),
        "fan": _FANS.get((st[12] >> 1) & 0b111, "?"),
        "extended": extended,
        "checksum_ok": ok,
    }


def describe(protocol: str, payload: str) -> str | None:
    """한 줄 요약. 모르면 None — 모르는 것을 아는 척하지 않는다."""
    if protocol != "SAMSUNG_AC" or not payload:
        return None
    d = samsung_ac(payload)
    if d is None:
        return None
    out = [f"전원 {'ON' if d['power'] else 'OFF'}", f"{d['temp']}℃",
           d["mode"], f"fan {d['fan']}"]
    if d["extended"]:
        out.append("extended(전원 전환)")
    if not d["checksum_ok"]:
        out.append("⚠checksum 불일치")
    return "  ·  ".join(out)
