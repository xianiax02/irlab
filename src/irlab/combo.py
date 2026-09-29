"""조합 패널이 쏘는 에어컨 상태 — 펌웨어 IRac 상태 명령(on/off · m · t · f · w)으로 옮긴다.

⚠️ 모드·fan 번호는 **펌웨어 인덱스**다 (tempio `ir_sender.cpp` 의 mapMode/mapFan 순서).
`decode._MODES` 는 Samsung 바이트 순서라 2~4 가 다르다(Heat·Dry·Fan 이 돈다).
그 표를 가져다 쓰면 Heat 를 골랐는데 Dry 가 나간다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

MODES = ("Auto", "Cool", "Heat", "Dry", "Fan")
FANS = ("Auto", "Low", "Med", "High")
TEMP_MIN, TEMP_MAX = 16, 30          # 펌웨어 constrain 과 같은 범위
FIELDS = ("power", "mode", "temp", "fan", "swing")
NAMES = {"power": "전원", "mode": "모드", "temp": "온도", "fan": "fan", "swing": "swing"}


@dataclass(frozen=True)
class AcCombo:
    power: int = 1
    mode: int = 1
    temp: int = 24
    fan: int = 0
    swing: int = 0

    def commands(self) -> list[str]:
        """발사(`s`) 전에 보낼 설정 줄."""
        return ["on" if self.power else "off", f"m {self.mode}", f"t {self.temp}",
                f"f {self.fan}", f"w {self.swing}"]

    def step(self, field: str, delta: int) -> AcCombo:
        if field == "temp":
            v = min(TEMP_MAX, max(TEMP_MIN, self.temp + delta))
        else:
            n = {"power": 2, "mode": len(MODES), "fan": len(FANS), "swing": 2}[field]
            v = (getattr(self, field) + delta) % n
        return replace(self, **{field: v})

    def label(self, field: str) -> str:
        v = getattr(self, field)
        if field == "power":
            return "ON" if v else "OFF"
        if field == "mode":
            return MODES[v] if 0 <= v < len(MODES) else f"?{v}"
        if field == "fan":
            return FANS[v] if 0 <= v < len(FANS) else f"?{v}"
        if field == "temp":
            return f"{v}℃"
        return "on" if v else "off"

    def summary(self) -> str:
        return " · ".join(f"{NAMES[f]} {self.label(f)}" for f in FIELDS)


def from_state(p: list[str]) -> tuple[str, AcCombo]:
    """`@STATE,<txpin>,<rxpin>,<proto>,<power>,<temp>,<mode>,<fan>,<swing>,<ref>` 의 태그 뒤 필드.

    순서가 AcCombo 필드 순서와 다르다(temp 가 mode 앞) — 위치로만 읽는다.
    """
    if len(p) < 8:
        raise IndexError(f"STATE 필드 {len(p)}개 (8 필요)")
    return p[2], AcCombo(power=int(p[3]), temp=int(p[4]), mode=int(p[5]),
                         fan=int(p[6]), swing=int(p[7]))
