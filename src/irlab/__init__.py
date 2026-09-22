"""irlab — 그린고래 IR 진단기."""

from __future__ import annotations

__all__ = ["__version__", "__dist__", "code_fingerprint"]

# 배포명은 ggirlab, import 패키지명은 irlab 이다 (pillow→PIL 과 같은 형태).
__dist__ = "ggirlab"

# 버전은 **설치 메타데이터에서 읽는다** — pyproject 를 단일 출처로 두기 위해서다.
# 여기 숫자를 따로 적어두면 pyproject 와 갈라지고, 갈라진 걸 알 방법이 없다.
try:  # 설치된 경우
    from importlib.metadata import version as _version

    __version__ = _version(__dist__)
except Exception:  # 소스 트리에서 직접 실행 (PYTHONPATH=src)
    __version__ = "0.0.0+source"


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
