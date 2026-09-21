"""irlab 진입점."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .device import find_ports
from .library import DEFAULT_DIR, Library, list_stores


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="irlab",
        description="그린고래 IR 진단기 — 매장 리모컨 신호를 읽고, 이름 붙여 저장하고, 골라서 쏜다")
    ap.add_argument("store", nargs="?",
                    help="매장 이름 (생략하면 실행 후 화면에서 고른다)")
    ap.add_argument("--port", help="시리얼 포트 (기본: 자동 탐지)")
    ap.add_argument("--dir", default=None, help=f"저장 위치 (기본 {DEFAULT_DIR})")
    ap.add_argument("--list-ports", action="store_true")
    ap.add_argument("--stores", action="store_true", help="저장된 매장 목록")
    ap.add_argument("--show", action="store_true", help="매장 신호 목록만 출력하고 종료")
    a = ap.parse_args()

    base = Path(a.dir) if a.dir else None

    if a.list_ports:
        ports = find_ports()
        print("\n".join(ports) if ports else "후보 포트 없음")
        return 0

    if a.stores:
        names = list_stores(base)
        print("\n".join(names) if names else "저장된 매장 없음")
        return 0

    if a.show and not a.store:
        ap.error("--show 는 매장 이름이 필요하다 (예: irlab <매장> --show)")

    if a.show:
        lib = Library(a.store, base)
        print(f"{lib.store}  —  {lib.path}")
        if not lib.signals:
            print("  (신호 없음)")
        for i, s in enumerate(lib.signals, 1):
            print(f"  {i:>2}  {s.name:<24} {s.protocol:<14} "
                  f"서버{'✓' if s.server_ok else '✗'}  raw {len(s.raw)}"
                  + ("  ⚠잘림" if s.truncated else ""))
        return 0

    from .app import IrlabApp
    IrlabApp(a.store, a.port, base).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
