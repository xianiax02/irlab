"""신호 라이브러리 — 매장별 JSON 파일.

왜 매장별로 나누나: 매장 하나 나갈 때 그 파일 하나만 들고 가면 되고,
한 파일에 섞여 있으면 **남의 매장 신호를 잘못 쏠 수 있다.**

왜 노트북에 두나: 기기 메모리는 전원이 빠지면 날아가고 용량도 작다.
파일로 두면 백업되고, 나중에 서버에 raw 트랙을 넘길 때 그대로 쓸 자료가 된다.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_DIR = Path.home() / ".irlab" / "stores"

# 백엔드가 등록을 받아주는 프로토콜 이름과 IRremoteESP8266 decode_type 값.
# ⚠️ 이 목록에 없으면 **서버에 등록이 안 된다** — 노드가 쏠 수 있는지와는 다른 축이다.
# 배포처마다 다르므로 자기 서버 목록에 맞춰 고쳐 쓸 것.
SERVER_PROTOCOLS: dict[str, int] = {
    "TCL112AC": 57,
    "SAMSUNG_AC": 46,
    "LG": 10,
    "LG2": 51,
    "DAIKIN": 16,
    "COOLIX": 15,
    "HAIER_AC": 38,
    "MITSUBISHI_AC": 20,
    "GREE": 24,
}


def server_ok(protocol: str) -> bool:
    return protocol in SERVER_PROTOCOLS


def decode_type_of(protocol: str) -> int | None:
    return SERVER_PROTOCOLS.get(protocol)


@dataclass
class Signal:
    """캡처한 신호 하나. 이름은 자유 입력."""

    name: str
    protocol: str = "UNKNOWN"
    bits: int = 0
    payload: str = ""            # AC 상태 바이트열 hex (없으면 빈 문자열)
    raw: list[int] = field(default_factory=list)
    carrier_khz: int = 38        # ⚠️ 가정값 — 복조 수신이라 실제로는 못 잰다
    truncated: bool = False      # raw 가 기기 버퍼에서 잘렸는가
    captured_at: str = ""

    @property
    def server_ok(self) -> bool:
        return server_ok(self.protocol)

    @property
    def decode_type(self) -> int | None:
        return decode_type_of(self.protocol)

    def summary(self) -> str:
        tag = self.protocol if self.protocol != "UNKNOWN" else f"raw {len(self.raw)}"
        return f"{self.name}  ·  {tag}"


def slugify(name: str) -> str:
    """매장 이름 → 파일명. 한글은 그대로 두고 경로에 위험한 문자만 턴다."""
    s = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name).strip().strip(".")
    return s or "무제"


class Library:
    """한 매장의 신호 묶음."""

    def __init__(self, store: str, base: Path | None = None) -> None:
        self.store = store
        self.base = Path(base) if base else DEFAULT_DIR
        self.signals: list[Signal] = []
        self.load()

    @property
    def path(self) -> Path:
        return self.base / f"{slugify(self.store)}.json"

    # ── 입출력 ──
    def load(self) -> None:
        self.signals = []
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for d in data.get("signals", []):
            known = {k: v for k, v in d.items() if k in Signal.__annotations__}
            self.signals.append(Signal(**known))

    def save(self) -> Path:
        """**원자적으로** 쓴다 — 임시 파일에 다 쓰고 os.replace 로 갈아끼운다.

        write_text 는 먼저 자르고 그다음 쓴다. 현장에서 그 사이에 죽거나 USB
        허브째 전원이 나가면 매장 신호가 통째로 빈 파일이 된다. 다시 캡처하려면
        매장에 다시 가야 하는 자료라 그 위험을 지지 않는다.
        """
        self.base.mkdir(parents=True, exist_ok=True)
        payload = {
            "store": self.store,
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "signals": [asdict(s) for s in self.signals],
        }
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        return self.path

    # ── 편집 ──
    def add(self, sig: Signal) -> None:
        if not sig.captured_at:
            sig.captured_at = dt.datetime.now().isoformat(timespec="seconds")
        self.signals.append(sig)
        self.save()

    def remove(self, index: int) -> Signal | None:
        if 0 <= index < len(self.signals):
            s = self.signals.pop(index)
            self.save()
            return s
        return None

    def rename(self, index: int, name: str) -> None:
        if 0 <= index < len(self.signals):
            self.signals[index].name = name
            self.save()


def list_stores(base: Path | None = None) -> list[str]:
    d = Path(base) if base else DEFAULT_DIR
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")).get("store", p.stem))
        except (OSError, json.JSONDecodeError):
            out.append(p.stem)
    return out
