"""irlab TUI — 리딩 모드 · 신호 라이브러리 · 도달 시험.

⚠️ 이 리그는 **자기가 쏜 것을 자기가 받지 못한다** (펌웨어가 송신 중 수신을 끈다).
그래서 "쏘기" 의 성공은 *발사* 성공이지 *도달* 이 아니다. 도달을 재는 것은
`수신 창 시험` 뿐이다 — 노드나 리모컨이 쏘는 동안 리그가 몇 개나 받는지 센다.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable, Footer, Header, Input, Label, ListItem, ListView, Log, Static,
)

from pathlib import Path

from .device import Device, find_ports
from .library import DEFAULT_DIR, Library, Signal, list_stores

MAX_STREAM = 200
TEST_SECONDS = 20          # 수신 창 시험 기본 길이
# 굴림 캡처가 들어가는 작업 슬롯. ref(0) 와 갈라야 대조가 살아 있다.
SCRATCH_SLOT = 3


class NameModal(ModalScreen[str | None]):
    """신호 이름 입력. 자유 입력 — 현장에서 예외가 나와도 막히면 안 된다."""

    BINDINGS = [("escape", "cancel", "취소")]

    def __init__(self, suggestion: str = "") -> None:
        super().__init__()
        self.suggestion = suggestion

    def compose(self) -> ComposeResult:
        with Vertical(id="modal"):
            yield Label("신호 이름  (예: 냉방 24도 / OFF / 사장님 기본설정)")
            yield Input(value=self.suggestion, id="nameinput")

    def on_mount(self) -> None:
        self.query_one("#nameinput", Input).focus()

    @on(Input.Submitted)
    def submit(self, e: Input.Submitted) -> None:
        self.dismiss(e.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class StoreModal(ModalScreen[str | None]):
    """매장 선택. 기존 것을 고르거나 새 이름을 친다.

    매장이 파일 단위라 먼저 정해져야 한다 — 안 그러면 남의 매장에 신호가 섞인다.
    """

    BINDINGS = [("escape", "cancel", "취소")]

    def __init__(self, base=None, allow_cancel: bool = False) -> None:
        super().__init__()
        self.base = base
        self.allow_cancel = allow_cancel

    def compose(self) -> ComposeResult:
        with Vertical(id="storebox"):
            yield Label("매장을 고르세요  (↑↓ + Enter, 또는 새 이름을 입력)")
            names = list_stores(self.base)
            yield ListView(*[ListItem(Label(n), name=n) for n in names], id="storelist")
            yield Input(placeholder="새 매장 이름", id="storenew")

    def on_mount(self) -> None:
        self.query_one("#storenew", Input).focus()

    @on(ListView.Selected)
    def picked(self, e: ListView.Selected) -> None:
        self.dismiss(e.item.name)

    @on(Input.Submitted)
    def typed(self, e: Input.Submitted) -> None:
        v = e.value.strip()
        if v:
            self.dismiss(v)

    def action_cancel(self) -> None:
        if self.allow_cancel:
            self.dismiss(None)


class LabelModal(ModalScreen[str | None]):
    """측정 위치 라벨. 시험 **전에** 박는다 — 안 박으면 나중에 어느 자리였는지 못 되짚는다."""

    BINDINGS = [("escape", "cancel", "취소")]

    def __init__(self, suggestion: str = "") -> None:
        super().__init__()
        self.suggestion = suggestion

    def compose(self) -> ComposeResult:
        with Vertical(id="modal"):
            yield Label("측정 위치  (예: 3.0m 30도 후보A / 천장반사)")
            yield Input(value=self.suggestion, id="labelinput")

    def on_mount(self) -> None:
        self.query_one("#labelinput", Input).focus()

    @on(Input.Submitted)
    def submit(self, e: Input.Submitted) -> None:
        self.dismiss(e.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class IrlabApp(App[None]):
    CSS = """
    Screen { layout: vertical; }
    #bar { height: 3; padding: 0 1; background: $panel; }
    #cols { height: 1fr; }
    #left { width: 3fr; border: round $primary; }
    #right { width: 2fr; border: round $secondary; }
    #detail { height: 7; border: round $accent; padding: 0 1; }
    #storebox { align: center middle; width: 66; height: 18;
            border: thick $primary; background: $surface; padding: 1 2; }
    #storelist { height: 9; border: round $panel; }
    #modal { align: center middle; width: 70; height: 7;
             border: thick $primary; background: $surface; padding: 1 2; }
    .title { text-style: bold; }
    """

    BINDINGS = [
        ("r", "toggle_read", "리딩"),
        ("s", "save_signal", "저장"),
        ("enter", "send_selected", "쏘기"),
        ("a", "set_label", "위치라벨"),
        ("t", "reach_test", "수신시험"),
        ("c", "export_csv", "CSV"),
        ("d", "delete_signal", "삭제"),
        ("p", "apply_protocol", "프로토콜적용"),
        ("m", "pick_store", "매장"),
        ("q", "quit", "종료"),
    ]

    def __init__(self, store: str | None = None, port: str | None = None, base=None) -> None:
        super().__init__()
        self.base = base
        self.lib = Library(store, base) if store else None
        self.want_port = port
        self.dev = Device(self.dev_event, self.dev_raw, self.dev_lost)
        self.reading = False
        self.last: dict | None = None      # 마지막 수신 프레임 요약
        self.pending: dict | None = None   # dump 로 받는 중인 raw
        self._dump_fut = None              # dump 완료를 기다리는 곳
        # 기기 작업 슬롯에 **실제로 들어 있는** 프레임. @SLOT 이 정본이다.
        # self.last(=마지막 수신)와 갈라야 하는 이유는 저장 경로 주석 참조.
        self.slot: dict | None = None
        self._hold = False                 # 저장 중 굴림 재무장 정지
        self.pos_label = ""                # 현재 측정 위치
        self.rows: list[dict] = []         # 시험 기록 (CSV 로 나간다)
        self._tally: dict | None = None    # 시험 중 계수기

    # ── 화면 ──
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="bar")
        with Horizontal(id="cols"):
            with Vertical(id="left"):
                yield Label("수신 스트림", classes="title")
                yield Log(id="stream", highlight=False)
            with Vertical(id="right"):
                yield Label("신호 라이브러리", classes="title")
                yield DataTable(id="lib", cursor_type="row")
        yield Static(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#lib", DataTable)
        t.add_columns("#", "이름", "프로토콜", "서버", "raw")
        self.refresh_lib()
        self.refresh_bar()
        self.connect()
        if self.lib is None:
            self.pick_store_flow(first=True)

    # ── 연결 ──
    def connect(self) -> None:
        port = self.want_port
        if not port:
            ports = find_ports()
            if not ports:
                self.log_line("포트를 못 찾았다 — USB 연결과 pio monitor 를 확인.")
                self.refresh_bar()
                return
            port = ports[0]
        try:
            self.dev.open(port)
            self.log_line(f"연결 {port}")
            self.dev.send("?")
        except (OSError, ValueError) as e:
            # 열기 실패로 앱 전체가 죽으면 안 된다 — 포트만 다시 잡으면 되는 상태다.
            self.log_line(f"[red]열기 실패[/] {port}: {e}")
        self.refresh_bar()

    # ── 매장 ──
    def action_pick_store(self) -> None:
        self.pick_store_flow(first=False)

    @work
    async def pick_store_flow(self, first: bool) -> None:
        name = await self.push_screen_wait(StoreModal(self.base, allow_cancel=not first))
        if not name:
            return
        self.lib = Library(name, self.base)
        self.refresh_lib()
        self.refresh_bar()
        self.log_line(f"매장 «{name}»  —  {self.lib.path}")

    # ── 표시 ──
    def refresh_bar(self) -> None:
        conn = f"[green]● {self.dev.path}[/]" if self.dev.connected else "[red]● 끊김[/]"
        mode = "[b green]READ 리딩중[/]" if self.reading else "[dim]READ 정지[/]"
        store = (f"매장 [b]{self.lib.store}[/]   신호 {len(self.lib.signals)}개")\
            if self.lib else "[yellow]매장 미선택 — m[/]"
        pos = (f"위치 [b]{self.pos_label}[/]" if self.pos_label
               else "[yellow]위치 미지정 — a[/]")
        self.query_one("#bar", Static).update(
            f"{conn}   {mode}   {store}   {pos}   기록 {len(self.rows)}")

    def refresh_lib(self) -> None:
        t = self.query_one("#lib", DataTable)
        t.clear()
        if self.lib is None:
            return
        for i, s in enumerate(self.lib.signals, 1):
            t.add_row(str(i), s.name, s.protocol,
                      "✓" if s.server_ok else "✗", str(len(s.raw)))

    def log_line(self, s: str) -> None:
        # 종료 중(위젯 해체 후)에도 불린다 — 기기 콜백과 on_unmount 의 CSV 저장이
        # 그렇다. 화면이 없다고 종료가 예외로 끝나면 안 된다.
        try:
            self.query_one("#stream", Log).write_line(s)
        except NoMatches:
            pass

    def detail(self, s: str) -> None:
        self.query_one("#detail", Static).update(s)

    # ── 기기 이벤트 ──
    def dev_raw(self, line: str) -> None:
        # 대조 결과("      DIFF: …"·"      MATCH ref")는 '#' 로 시작하지 않는다.
        # 종전처럼 '#' 만 통과시키면 **이 리그의 핵심 출력이 화면에서 통째로 사라진다.**
        self.log_line(line)

    def dev_lost(self, why: str) -> None:
        self.log_line(f"[red]연결 끊김[/] {why}")
        if self._tally is not None:
            self._tally["lost"] = True
        self.refresh_bar()

    def dev_event(self, tag: str, p: list[str]) -> None:
        try:
            self._dev_event(tag, p)
        except (IndexError, ValueError) as e:
            # 줄이 잘려 들어오면 여기서 터진다. 터지면 이벤트 루프 콜백이 죽으므로
            # 삼키되 **보이게** 남긴다 — 조용히 버리면 데이터 손실을 모른다.
            self.log_line(f"[yellow]해석 불가[/] @{tag},{','.join(p)}  ({e})")

    def _dev_event(self, tag: str, p: list[str]) -> None:
        if tag == "RX":
            if len(p) < 6:
                raise IndexError(f"RX 필드 {len(p)}개 (6 필요)")
            proto, bits, payload, sup, rawlen = p[1], p[2], p[3], p[4] == "1", p[5]
            self.last = {"proto": proto, "bits": int(bits or 0),
                         "payload": payload, "sup": sup, "rawlen": int(rawlen or 0)}
            ts = dt.datetime.now().strftime("%H:%M:%S")
            self.log_line(f"{ts}  {proto:<12} {bits:>4}b  raw={rawlen}"
                          + ("" if sup else "  (IRac 미지원)"))
            self.show_last()
            if self._tally is not None:
                self._tally["rx"] += 1
                self._tally["protos"].add(proto)
            if self.reading and not self._hold:
                # 굴림 캡처는 **ref(slot0) 가 아닌 작업 슬롯**으로 받는다.
                # slot0 로 받으면 리모컨 기준값이 매 프레임 덮어써져서 대조가 무의미해진다.
                # _hold 중에는 재무장하지 않는다 — 저장할 프레임이 밑에서 갈리면 안 된다.
                self.dev.send(f"learn {SCRATCH_SLOT}")

        elif tag == "SLOT":
            # @SLOT,<i>,<used>,<proto>,<bits>,<hex>,<rawlen>,<trunc>,<isref>
            # **저장은 이 값으로 한다.** self.last 는 '마지막으로 들어온 프레임' 이라
            # 슬롯에 실제로 담긴 것과 다를 수 있다(리딩 꺼진 뒤 들어온 프레임 등).
            if len(p) >= 7 and int(p[0]) == SCRATCH_SLOT:
                self.slot = ({"proto": p[2], "bits": int(p[3] or 0), "payload": p[4],
                              "rawlen": int(p[5] or 0), "trunc": p[6] == "1"}
                             if p[1] == "1" else None)

        elif tag == "MATCH":
            if self._tally is not None:
                self._tally["match"] += 1
            self.log_line("  [green]MATCH ref[/] — ref 와 전 바이트 일치")
        elif tag == "DIFF":
            if self._tally is not None:
                self._tally["diff"] += 1
            n = p[1] if len(p) > 1 else "?"
            detail = p[2] if len(p) > 2 else ""
            pretty = "  ".join(
                f"[{i}] {a}→{b}" for i, a, b in
                (x.split(":") for x in detail.split("|") if x.count(":") == 2))
            self.log_line(f"  [red]DIFF {n}바이트[/]  {pretty}")
        elif tag in ("DIFFPROTO", "DIFFBITS", "DIFFVAL"):
            if self._tally is not None:
                self._tally["diff"] += 1
            self.log_line(f"  [red]DIFF[/] {tag[4:].lower()}: ref={p[1]} got={p[2]}")

        elif tag == "RAWBEG":
            self.pending = {"khz": int(p[1]), "n": int(p[2]),
                            "trunc": p[3] == "1", "raw": []}
        elif tag == "RAWCHK" and self.pending is not None:
            off = int(p[0])
            if off != len(self.pending["raw"]):
                # 청크가 빠지거나 순서가 어긋난 것. 개수만 맞으면 통과하던 자리다.
                self.log_line(f"⚠ raw 청크 어긋남 off={off} ≠ {len(self.pending['raw'])}")
                self.pending = None
                return
            self.pending["raw"].extend(int(v) for v in p[1:] if v)
        elif tag == "RAWEND" and self.pending is not None:
            pend, self.pending = self.pending, None
            if self._dump_fut is not None and not self._dump_fut.done():
                got, want = len(pend["raw"]), pend["n"]
                if got != want:
                    self._dump_fut.set_exception(
                        RuntimeError(f"raw 누락 {got}/{want}"))
                else:
                    self._dump_fut.set_result(pend)
        elif tag == "RAWERR":
            self.log_line("기기 오류: " + ",".join(p))
            self.pending = None
            if self._dump_fut is not None and not self._dump_fut.done():
                self._dump_fut.set_exception(RuntimeError(",".join(p)))
        elif tag == "TXERR":
            self.log_line(f"[red]발사 거부[/] {p[1] if len(p) > 1 else ''} — 프로토콜 적용 실패")
        elif tag == "ACT":
            # @ACT,<ms>,<엣지수>,<같은 창에서 해독됨 0/1>
            # ⚠️ 엣지가 있다 ≠ 해독 실패다. 정상 해독된 프레임도 엣지를 만든다.
            #    종전에는 조건 없이 "해독 실패" 를 붙여서, 리모컨이 잘 읽힌 줄
            #    바로 밑에 실패라고 찍혔다. 판정은 펌웨어가 실어 보낸 값으로 한다.
            edges = p[1] if len(p) > 1 else "?"
            decoded = (p[2] == "1") if len(p) > 2 else None
            if decoded:
                return          # 방금 찍힌 프레임이 만든 엣지다 — 조용히 넘긴다
            if decoded is None:
                self.log_line(f"[신호 활동] 엣지 {edges}회/초 "
                              "(구버전 펌웨어 — 해독 여부를 같이 안 보낸다)")
                return
            # 여기만이 진짜 "닿는데 못 읽는다" 다. ①안 닿음 과 구별되는 상태다.
            self.log_line(f"[yellow]신호는 들어오는데 해독이 안 된다[/] "
                          f"엣지 {edges}회/초 — 캐리어 주파수·거리·전원 노이즈 의심")
        elif tag == "PUSHSENT":
            self.log_line(f"발사 완료 raw {p[1]} @ {p[2]}kHz")

    def show_last(self) -> None:
        if not self.last:
            return
        L = self.last
        from .library import server_ok
        ok = server_ok(L["proto"])
        self.detail(
            f"[b]{L['proto']}[/]  {L['bits']}b  raw {L['rawlen']}\n"
            f"{L['payload'][:64]}\n"
            f"쏠 수 있음 {'[green]✓[/]' if L['sup'] else '[red]✗[/]'}    "
            f"서버 등록 {'[green]✓[/]' if ok else '[red]✗ (SUPPORTED_PROTOCOLS 에 없음)[/]'}")

    # ── 저장 ──
    async def pull_raw(self, timeout: float = 5.0) -> dict:
        """작업 슬롯의 raw 를 기기에서 끌어온다."""
        fut = asyncio.get_running_loop().create_future()
        self._dump_fut = fut
        self.dev.send(f"dump {SCRATCH_SLOT}")
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._dump_fut = None

    # ── 액션 ──
    def action_toggle_read(self) -> None:
        if not self.dev.connected:
            self.log_line("연결 안 됨")
            return
        self.reading = not self.reading
        if self.reading:
            self.dev.send(f"learn {SCRATCH_SLOT}")
            self.log_line(f"리딩 시작 (slot{SCRATCH_SLOT} 로 굴림) — "
                          "리모컨을 수신부에 대고 누르세요")
        else:
            self.log_line("리딩 정지")
        self.refresh_bar()

    @work
    async def action_save_signal(self) -> None:
        """작업 슬롯에 담긴 프레임을 이름 붙여 라이브러리에 넣는다.

        순서가 중요하다 — **이름을 묻기 전에 먼저 얼려서 끌어온다.**
        종전에는 이름 모달을 먼저 띄웠는데, 그동안에도 리딩이 계속 돌아
        슬롯이 새 프레임으로 덮였다. 이름을 타이핑하는 몇 초 사이에 리모컨이
        한 번 더 들어오면 **내가 누른 버튼의 이름이 다른 신호에 붙는다.**
        """
        if self.lib is None:
            self.log_line("매장을 먼저 고르세요 (m)")
            return
        if not self.slot:
            self.log_line(f"작업 슬롯(slot{SCRATCH_SLOT})이 비어 있다 — "
                          "리딩(r)을 켜고 리모컨을 한 번 받으세요")
            return

        meta = dict(self.slot)          # 키를 누른 그 순간의 슬롯 내용
        self._hold = True               # 밑에서 갈리지 않게 굴림 정지
        try:
            self.log_line(f"고정 «{meta['proto']} {meta['bits']}b raw {meta['rawlen']}»"
                          " — 이름을 입력하세요")
            try:
                pend = await self.pull_raw()
            except (TimeoutError, RuntimeError) as e:
                self.log_line(f"[red]raw 를 못 가져왔다[/] {e} — 저장 안 함")
                return
            name = await self.push_screen_wait(NameModal())
            if not name:
                self.log_line("저장 취소")
                return
            sig = Signal(
                name=name,
                protocol=meta["proto"],
                bits=meta["bits"],
                payload=meta["payload"],
                raw=pend["raw"],
                carrier_khz=pend["khz"],
                truncated=pend["trunc"] or meta["trunc"],
            )
            self.lib.add(sig)
            self.refresh_lib()
            self.refresh_bar()
            self.log_line(f"저장 «{sig.name}»  {sig.protocol} {sig.bits}b  "
                          f"raw {len(sig.raw)}" + ("  ⚠잘림" if sig.truncated else ""))
        finally:
            self._hold = False
            if self.reading:
                self.dev.send(f"learn {SCRATCH_SLOT}")   # 굴림 재개

    def selected_index(self) -> int | None:
        t = self.query_one("#lib", DataTable)
        if self.lib is None or t.cursor_row is None or not self.lib.signals:
            return None
        return int(t.cursor_row)

    def action_send_selected(self) -> None:
        i = self.selected_index()
        if i is None:
            self.log_line("라이브러리에서 하나 고르세요")
            return
        s = self.lib.signals[i]
        if not s.raw:
            self.log_line("raw 가 없는 신호다")
            return
        self.send_one(s)

    @work
    async def send_one(self, s: Signal) -> None:
        self.log_line(f"쏘기 «{s.name}»  raw {len(s.raw)} @ {s.carrier_khz}kHz")
        try:
            n = await self.dev.push_raw(s.raw, s.carrier_khz)
            self.log_line(f"  전송 {n}칸 확인 후 발사")
        except TimeoutError:
            self.log_line("  ✕ 기기 응답 없음 — 연결 확인")
        except RuntimeError as e:
            self.log_line(f"  ✕ {e}")

    # ── 위치 라벨 ──
    @work
    async def action_set_label(self) -> None:
        v = await self.push_screen_wait(LabelModal(self.pos_label))
        if v:
            self.pos_label = v
            self.log_line(f"위치 «{v}»")
            self.refresh_bar()

    # ── 도달 시험 ──
    # ⚠️ 이 리그는 자기 발사를 자기가 못 받는다. 그래서 "쏘고 몇 번 성공했나" 는
    #    도달이 아니라 **발사** 계수다. 도달은 이렇게만 잰다: 창을 열어두고
    #    노드/리모컨이 쏘는 동안 실제로 들어온 프레임을 센다.
    def action_reach_test(self) -> None:
        if not self.dev.connected:
            self.log_line("연결 안 됨")
            return
        if self._tally is not None:
            self.log_line("시험이 이미 돌고 있다")
            return
        if not self.pos_label:
            self.log_line("먼저 위치를 박으세요 (a) — 안 박으면 어느 자리였는지 못 되짚는다")
            return
        self.reach_test(TEST_SECONDS)

    @work
    async def reach_test(self, seconds: int) -> None:
        self._tally = {"rx": 0, "match": 0, "diff": 0, "protos": set(), "lost": False}
        self.log_line(f"── 수신 창 시험 {seconds}초 «{self.pos_label}» — "
                      "지금 노드/리모컨을 쏘게 하세요")
        try:
            for left in range(seconds, 0, -1):
                if self._tally.get("lost"):
                    break
                if left % 5 == 0:
                    self.log_line(f"   … {left}초  수신 {self._tally['rx']}")
                await asyncio.sleep(1.0)
            t = self._tally
        finally:
            self._tally = None

        row = {
            "time": dt.datetime.now().isoformat(timespec="seconds"),
            "store": self.lib.store if self.lib else "",
            "위치": self.pos_label,
            "창(초)": seconds,
            "수신": t["rx"],
            "MATCH": t["match"],
            "DIFF": t["diff"],
            "프로토콜": "|".join(sorted(t["protos"])) or "-",
            "중단": "연결끊김" if t["lost"] else "",
        }
        self.rows.append(row)
        self.log_line(
            f"── 결과 «{self.pos_label}»  수신 {t['rx']}  MATCH {t['match']}  "
            f"DIFF {t['diff']}  [{row['프로토콜']}]"
            + ("  [red]연결끊김으로 중단[/]" if t["lost"] else ""))
        self.log_line("   ※ 이 값은 '창 시간 동안 받은 프레임 수' 다. "
                      "자리끼리 같은 조건으로 비교할 때만 뜻이 있다.")
        self.refresh_bar()

    # ── 기록 내보내기 ──
    def action_export_csv(self) -> Path | None:
        if not self.rows:
            self.log_line("기록이 없다")
            return None
        d = Path(self.base) if self.base else DEFAULT_DIR
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"irlab-{dt.date.today():%Y-%m-%d}.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(self.rows[0].keys()))
            w.writeheader()
            w.writerows(self.rows)
        self.log_line(f"CSV 저장 {path}  ({len(self.rows)}행)")
        return path

    def action_delete_signal(self) -> None:
        i = self.selected_index()
        if i is None:
            return
        s = self.lib.remove(i)
        if s:
            self.log_line(f"삭제 «{s.name}»")
            self.refresh_lib()
            self.refresh_bar()

    def action_apply_protocol(self) -> None:
        """판별된 프로토콜을 리그 TX 에 적용 + 서버에 넣을 값을 띄운다."""
        if not self.last:
            return
        from .library import decode_type_of
        proto = self.last["proto"]
        self.dev.send(f"p {proto}")
        dt_ = decode_type_of(proto)
        if dt_ is None:
            self.log_line(f"{proto} → 리그 TX 에 적용. "
                          "⚠ 서버 SUPPORTED_PROTOCOLS 에 없어 대시보드 등록 불가")
        else:
            self.log_line(f"{proto} → 리그 TX 적용. "
                          f"대시보드에 넣을 값: {proto} (decode_type {dt_})")

    def on_unmount(self) -> None:
        # 현장을 떠나기 전에 기록을 파일로 떨군다. 잊고 나가면 다시 재야 한다.
        try:
            self.action_export_csv()
        except Exception as e:      # 종료 경로다 — 여기서 터지면 기록만 잃는다
            print(f"irlab: CSV 저장 실패 — {e}")
        self.dev.close()
