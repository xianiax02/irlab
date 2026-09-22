"""irlab — 그린고래 IR 진단기."""

from __future__ import annotations

__all__ = ["__version__", "code_fingerprint"]

__version__ = "0.2.0"


def code_fingerprint() -> str:
    """이 패키지 소스의 내용 해시 12자.

    왜 필요한가: 이 도구는 로컬 경로에서 설치돼 있어서 **버전 번호가 그대로면
    `uv tool upgrade` 가 조용히 건너뛴다**("Nothing to upgrade"). 고쳤는데 설치본은
    구코드인 상태가 되고, 그걸 알 방법이 없었다(2026-09-22).
    버전 번호는 올리는 걸 잊을 수 있지만 이 값은 **코드가 바뀌면 반드시 바뀐다.**
    설치본과 소스 트리에서 각각 `--version` 을 찍어 이 값을 비교하면 된다.
    """
    import hashlib
    from pathlib import Path

    h = hashlib.sha256()
    for f in sorted(Path(__file__).parent.glob("*.py")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:12]
