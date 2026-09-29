# 조합 패널 — 리모컨식 IRac 합성 송신

## 왜

지금 신호를 고르는 수단은 라이브러리 표뿐이다. 쏘려는 상태를 전부 리모컨으로 녹음해 둬야 하고,
센터에는 `turn on`·`turn off` 둘만 있다. 펌웨어는 이미 IRac 로 상태를 합성해 쏠 수 있는데
(`on/off · m · t · f · w · s`), TUI 에서 그 경로에 닿는 방법이 없다.

합성 송신은 노드와 **같은 `ir_sender_send_ac` 경로**를 탄다. 그래서 이 패널로 쏜 게 안 먹으면
"우리가 만든 프레임이 틀렸다(②)" 쪽 증거가 되고, 녹음 재생과 나란히 두면 ①②를 가를 수 있다.

## 범위

- 우측 라이브러리 아래 `조합` 패널: 전원 · 모드 · 온도 · fan · swing + 프로토콜(읽기 전용)
- `tab` 으로 포커스 이동. 패널 포커스에서 `←→` 칸, `↑↓` 값, `enter` 쏘기
- 발사 결과(`@TX` / `@TXERR`)를 로그에 남긴다

범위 밖: 쏜 조합을 라이브러리에 저장, 프로토콜 변경 UI(`p` 로 충분), 펌웨어 수정.

## 번호 체계 — 펌웨어 인덱스만 쓴다

| | 0 | 1 | 2 | 3 | 4 |
|---|---|---|---|---|---|
| `m` (펌웨어 `mapMode`) | Auto | Cool | **Heat** | **Dry** | **Fan** |
| `decode._MODES` (Samsung 바이트) | Auto | Cool | Dry | Fan | Heat |

두 표는 다르다. 패널은 `ir_sender.cpp` 의 `mapMode`/`mapFan` 순서를 옮긴 **별도 상수**만 쓰고
`decode.py` 표를 재사용하지 않는다. 재사용하면 Heat 를 골랐는데 Dry 가 나간다.

fan `f`: `0 Auto · 1 Low · 2 Med · 3 High`. 온도 16~30, swing 0/1 (펌웨어 `constrain` 과 같은 범위).

## 구조

- `combo.py` (새 모듈, 순수) — `AcCombo` dataclass, 범위 상수·라벨, `commands()`(보낼 줄 목록),
  `from_state(fields)`(`@STATE` → AcCombo), `summary()`.
- `Device.fire_ac(combo)` — `_txlock` 안에서 명령마다 `@STATE` ack 를 기다리고, 마지막 `@STATE`
  가 요청값과 다르면 예외. 그 뒤 `s` 를 보내고 `@TX`/`@TXERR` 중 먼저 온 것으로 판정.
  `@TX` ok=0 이나 `@TXERR` 는 예외. 반환은 `(protocol, seq)`.
- `app.py` — `ComboPanel` 위젯(포커스 가능, 키 처리), `@STATE` 수신 시 패널 동기화, `@TX`·`@TXERR` 로그.

### 왜 명령마다 `@STATE` 를 기다리나

펌웨어는 설정 명령마다 `@STATE` 를 낸다. 그걸 ack 로 쓰면 구펌웨어·줄 유실로 설정이 안 먹었는데
`s` 만 나가서 **직전 상태가 쏘이는** 경우를 발사 전에 잡는다. 이 도구의 `turn off`=실제 ON 사고와
같은 부류(쏜 것과 믿는 것이 다름)다.

## 검증

`--selftest` 의 가짜 펌웨어에 상태 명령과 `@STATE`/`@TX`/`@TXERR` 응답을 넣고:

1. 조합 → 보낸 명령 순서가 `on|off, m, t, f, w, s`
2. **Heat 선택 시 `m 2`** 가 나간다 (음성 대조: `m 4` 면 실패)
3. 가짜 펌웨어가 온도를 무시하도록 하면 `fire_ac` 가 발사 전에 예외 — `s` 가 안 나간다
4. `@TXERR` 가 예외로 올라온다

실기기: 센터 SAMSUNG_AC 로 ON·Cool·24 를 쏘고 에어컨 반응 확인 (발사 ≠ 도달 유의).
