"""irlab TUI — 리딩 모드 · 신호 라이브러리 · 송신 테스트."""

from __future__ import annotations

import datetime as dt

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable, Footer, Header, Input, Label, ListItem, ListView, Log, Static,
)

from .device import Device, find_ports
from .library import Library, Signal, list_stores

MAX_STREAM = 200


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
        ("t", "test_selected", "반복10"),
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
        self.dev = Device(self.dev_event, self.dev_raw)
        self.reading = False
        self.last: dict | None = None      # 마지막 수신 프레임 요약
        self.pending: dict | None = None   # dump 로 받는 중인 raw
        self.pending_name: str | None = None
        self.test_left = 0

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
        except OSError as e:
            self.log_line(f"열기 실패 {port}: {e}")
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
        store = (f"매장 [b]{self.lib.store}[/]   신호 {len(self.lib.signals)}개   "
                 f"파일 {self.lib.path}") if self.lib else "[yellow]매장 미선택 — m[/]"
        self.query_one("#bar", Static).update(f"{conn}   {mode}   {store}")

    def refresh_lib(self) -> None:
        t = self.query_one("#lib", DataTable)
        t.clear()
        if self.lib is None:
            return
        for i, s in enumerate(self.lib.signals, 1):
            t.add_row(str(i), s.name, s.protocol,
                      "✓" if s.server_ok else "✗", str(len(s.raw)))

    def log_line(self, s: str) -> None:
        self.query_one("#stream", Log).write_line(s)

    def detail(self, s: str) -> None:
        self.query_one("#detail", Static).update(s)

    # ── 기기 이벤트 ──
    def dev_raw(self, line: str) -> None:
        if line.startswith("#"):
            self.log_line(line)

    def dev_event(self, tag: str, p: list[str]) -> None:
        if tag == "RX":
            proto, bits, payload, sup, rawlen = p[1], p[2], p[3], p[4] == "1", p[5]
            self.last = {"proto": proto, "bits": int(bits or 0),
                         "payload": payload, "sup": sup, "rawlen": int(rawlen or 0)}
            ts = dt.datetime.now().strftime("%H:%M:%S")
            self.log_line(f"{ts}  {proto:<12} {bits:>4}b  raw={rawlen}"
                          + ("" if sup else "  (IRac 미지원)"))
            self.show_last()
            if self.reading:
                # 항상 마지막 프레임이 slot 0 에 있도록 재무장한다.
                self.dev.send("learn 0")
            if self.test_left:
                self.test_left -= 1

        elif tag == "RAWBEG":
            self.pending = {"khz": int(p[1]), "n": int(p[2]),
                            "trunc": p[3] == "1", "raw": []}
        elif tag == "RAWCHK" and self.pending is not None:
            self.pending["raw"].extend(int(v) for v in p[1:] if v)
        elif tag == "RAWEND" and self.pending is not None:
            self.finish_capture()
        elif tag == "RAWERR":
            self.log_line("기기 오류: " + ",".join(p))
            self.pending = None
        elif tag == "ACT":
            # 해독은 실패했지만 신호는 들어온다 — "안 온다" 와 구별되는 상태다.
            self.log_line(f"[신호 감지] 엣지 {p[1]}회/초 — 수신은 되는데 해독 실패")
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
    def finish_capture(self) -> None:
        """dump 로 raw 를 다 받았다 → 이름을 물어보고 라이브러리에 넣는다."""
        pend, self.pending = self.pending, None
        if pend is None:
            return
        got, want = len(pend["raw"]), pend["n"]
        if got != want:
            self.log_line(f"⚠ raw 누락 {got}/{want} — 저장 안 함")
            return
        L = self.last or {}
        sig = Signal(
            name=self.pending_name or "이름없음",
            protocol=L.get("proto", "UNKNOWN"),
            bits=L.get("bits", 0),
            payload=L.get("payload", ""),
            raw=pend["raw"],
            carrier_khz=pend["khz"],
            truncated=pend["trunc"],
        )
        self.lib.add(sig)
        self.refresh_lib()
        self.refresh_bar()
        self.log_line(f"저장 «{sig.name}»  raw {len(sig.raw)}"
                      + ("  ⚠잘림" if sig.truncated else ""))
        self.pending_name = None

    # ── 액션 ──
    def action_toggle_read(self) -> None:
        if not self.dev.connected:
            self.log_line("연결 안 됨")
            return
        self.reading = not self.reading
        if self.reading:
            self.dev.send("learn 0")
            self.log_line("리딩 시작 — 리모컨을 수신부에 대고 누르세요")
        else:
            self.log_line("리딩 정지")
        self.refresh_bar()

    @work
    async def action_save_signal(self) -> None:
        if self.lib is None:
            self.log_line("매장을 먼저 고르세요 (m)")
            return
        if not self.last:
            self.log_line("저장할 신호가 없다 — 먼저 리딩으로 하나 받으세요")
            return
        name = await self.push_screen_wait(NameModal())
        if not name:
            return
        self.pending_name = name
        self.dev.send("dump 0")   # 기기에서 raw 를 끌어온다

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

    def action_test_selected(self) -> None:
        i = self.selected_index()
        if i is None:
            return
        s = self.lib.signals[i]
        self.test_left = 10
        self.log_line(f"반복 송신 10회 «{s.name}» — 수신 카운트로 도달을 본다")
        self.repeat_send(s)

    @work
    async def repeat_send(self, s: Signal) -> None:
        import asyncio
        ok = 0
        for k in range(10):
            try:
                await self.dev.push_raw(s.raw, s.carrier_khz)
                ok += 1
                self.log_line(f"  [{k + 1}/10] 발사")
            except (TimeoutError, RuntimeError) as e:
                self.log_line(f"  [{k + 1}/10] ✕ {e}")
            await asyncio.sleep(2.0)
        self.log_line(f"반복 송신 끝 — 성공 {ok}/10")

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
        self.dev.close()
