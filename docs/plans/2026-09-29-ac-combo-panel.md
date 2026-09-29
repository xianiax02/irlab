# 조합 패널 Implementation Plan

> **For agentic workers:** Implement this plan one task at a time, in order. Steps use checkbox (`- [ ]`) syntax — check a step off only after its verification step actually passed, and stop at each review checkpoint rather than running ahead.

**Goal:** TUI 에서 전원·모드·온도·fan·swing 을 키로 조합해 펌웨어 IRac 합성 송신으로 쏜다.

**Architecture:** 순수 모듈 `combo.py` 가 상태와 명령 변환을 갖고, `Device.fire_ac` 가 명령마다 `@STATE` ack 로
적용을 확인한 뒤 `s` 로 발사한다. `app.py` 의 `ComboPanel` 위젯이 키 입력을 받아 메시지로 올린다.

**Tech Stack:** Python 3.11, Textual, pty 기반 `--selftest`.

Spec: `docs/specs/2026-09-29-ac-combo-panel-design.md`

## Global Constraints

- 모드 인덱스는 펌웨어 `mapMode` 순서 `Auto, Cool, Heat, Dry, Fan` — `decode._MODES` 재사용 금지
- fan `Auto, Low, Med, High` (0~3) · 온도 16~30 · swing 0/1
- `@STATE` 필드 순서: `txpin, rxpin, proto, power, temp, mode, fan, swing, ref`
- 펌웨어 수정 없음

---

### Task 1: `combo.py` + `Device.fire_ac` + selftest

**Files:**
- Create: `src/irlab/combo.py`
- Modify: `src/irlab/device.py` (`push_raw` 뒤에 `fire_ac`)
- Modify: `src/irlab/selftest.py` (가짜 펌웨어 상태 명령 · 시험 6번 추가, 분리 시험은 7번으로)

**Interfaces — Produces:**
- `AcCombo(power, mode, temp, fan, swing)` frozen dataclass · `.commands() -> list[str]` ·
  `.step(field: str, delta: int) -> AcCombo` · `.label(field) -> str` · `.summary() -> str`
- `FIELDS = ("power", "mode", "temp", "fan", "swing")`
- `from_state(p: list[str]) -> tuple[str, AcCombo]` (`p` = `@STATE` 의 태그 뒤 필드)
- `async Device.fire_ac(combo: AcCombo) -> tuple[str, int]` — (프로토콜, seq). 실패는 `RuntimeError`/`TimeoutError`

- [ ] Step 1: selftest 에 시험 추가 (아래 검사) → `PYTHONPATH=src python3 -m irlab.cli --selftest` 가 ImportError 로 실패하는지 확인
  - `from_state` 가 `power,temp,mode` 순서를 지킨다
  - `step`: 온도 30 에서 +1 → 30, 모드 Fan(4) 에서 +1 → Auto(0)
  - ON·Heat·22·High·swing → 보낸 줄이 정확히 `on, m 2, t 22, f 3, w 1, s` (음성 대조: `m 4` 없음)
  - 가짜 펌웨어가 `t` 를 무시하면 `RuntimeError` 이고 `s` 가 안 나간다
  - `@TXERR` 가 `RuntimeError`
- [ ] Step 2: `combo.py`, `fire_ac` 구현
- [ ] Step 3: selftest 전부 통과 확인
- [ ] Step 4: 커밋 `feat: IRac 조합 송신 — 명령마다 @STATE 로 적용 확인`

### Task 2: `ComboPanel` + 앱 연결

**Files:**
- Modify: `src/irlab/app.py` — `ComboPanel` 위젯, 우측 레이아웃, `@STATE` 동기화, `Fire` 처리, 수신 로그 포커스 제외
- Modify: `README.md` — 키 표에 조합 패널

**Interfaces — Consumes:** Task 1 전부.

- `ComboPanel(Static, can_focus=True)`: `←→` 칸, `↑↓` 값, `enter` → `ComboPanel.Fire(combo)` 메시지.
  `dirty` 가 참이면 `@STATE` 가 편집 중 값을 덮지 않는다(프로토콜 표시는 항상 갱신). 발사 성공 시 `dirty=False`.
- 앱: `@on(ComboPanel.Fire)` → `fire_combo` worker → `dev.fire_ac`, 결과를 로그에 "발사 확인까지다, 도달은 t" 와 함께.

- [ ] Step 1: Textual pilot 스크립트(scratch)로 기대 동작 작성 — 패널 포커스 후 `up` 이 전원 토글, `right,right,up` 이 온도 +1, `enter` 가 미연결 로그
- [ ] Step 2: 구현
- [ ] Step 3: pilot 통과 + selftest 재통과
- [ ] Step 4: 커밋 `feat: 조합 패널 — 버튼 조합으로 IRac 상태를 골라 쏜다`

### Task 3: 실기기 (사람)

- [ ] `uv tool install --force ~/irlab` → 센터 SAMSUNG_AC 에 ON·Cool·24 발사, 에어컨 반응 확인
