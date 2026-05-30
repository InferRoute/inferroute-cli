"""`ir status` — personal usage view (TUI). Personal-scope siblings of
ir-ops: no Keys / Users / Models admin panels, only the data scoped to
the calling API key.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime

import httpx
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Static

from .config import Credentials, load


REFRESH_INTERVAL_SEC = 5.0


def _fmt_ts(iso_ts: str) -> str:
    if not iso_ts:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return iso_ts[:8]


def _short_model(name: str) -> str:
    return name.split("/")[-1] if "/" in name else name


def _fmt_count(n) -> str:
    if n is None:
        return "—"
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n/1000:.1f}K"
    if n < 1_000_000:
        return f"{n//1000}K"
    if n < 10_000_000:
        return f"{n/1_000_000:.1f}M"
    return f"{n//1_000_000}M"


class CreditsPanel(Static):
    DEFAULT_CSS = """
    CreditsPanel { padding: 1; border: round $primary; height: auto; }
    """

    def render(self) -> str:
        data = getattr(self, "data", None) or {}
        if "error" in data:
            return f"[red]Error: {data['error']}[/red]"
        balance = data.get("credit_balance")
        plan = data.get("plan") or "—"
        total_in = data.get("total_input_tokens", 0)
        total_out = data.get("total_output_tokens", 0)
        total_reqs = data.get("total_requests", 0)
        # cost is in millicents = $0.00001 each
        cost_mc = data.get("total_cost_mc", 0) or 0
        cost_usd = cost_mc / 100_000.0
        return (
            f"[b]Your account[/b]\n"
            f"  plan          {plan}\n"
            f"  credits       {balance if balance is not None else '—'} ¢\n"
            f"\n"
            f"[b]Lifetime usage[/b]\n"
            f"  requests      {_fmt_count(total_reqs)}\n"
            f"  input tokens  {_fmt_count(total_in)}\n"
            f"  output tokens {_fmt_count(total_out)}\n"
            f"  spent         ${cost_usd:.2f}"
        )


class RecentTable(DataTable):
    DEFAULT_CSS = """
    RecentTable { height: 100%; border: round $primary; min-height: 6; }
    """

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_columns("when", "model", "in/out tok", "lat", "$")

    def update_entries(self, entries: list[dict]) -> None:
        self.clear()
        for e in entries[:30]:
            t = e.get("created_at") or ""
            mod = _short_model(e.get("routed_model") or e.get("requested_model") or "?")
            in_t = int(e.get("input_tokens", 0) or 0)
            out_t = int(e.get("output_tokens", 0) or 0)
            io_str = f"{_fmt_count(in_t)}/{_fmt_count(out_t)}"
            lat = e.get("latency_ms")
            lat_str = "—" if lat is None else (f"{int(lat)}ms" if lat < 1000 else f"{lat/1000:.1f}s")
            cost_mc = int(e.get("cost", 0) or 0)
            cost_str = f"${cost_mc/100_000:.3f}"
            self.add_row(_fmt_ts(t), mod, io_str, lat_str, cost_str)


class StatusApp(App):
    CSS = """
    Screen { background: $background; }
    #row-top { height: auto; }
    #row-bot { height: 1fr; min-height: 8; }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("r", "refresh", "refresh"),
    ]

    def __init__(self, creds: Credentials) -> None:
        super().__init__()
        self.creds = creds
        self._client: httpx.AsyncClient | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, name="ir status")
        with Container(id="row-top"):
            yield CreditsPanel(id="credits")
        with Container(id="row-bot"):
            yield RecentTable(id="recent")
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "ir status"
        self.sub_title = self.creds.api_url
        self._client = httpx.AsyncClient(
            base_url=self.creds.api_url,
            headers={"x-api-key": self.creds.api_key},
            timeout=10.0,
        )
        self.set_interval(REFRESH_INTERVAL_SEC, self.refresh_data)
        self.refresh_data()

    async def on_unmount(self) -> None:
        if self._client:
            await self._client.aclose()

    @work(exclusive=True)
    async def refresh_data(self) -> None:
        try:
            r = await self._client.get("/v1/usage")
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as e:
            data = {"error": f"HTTP {e.response.status_code}"}
        except httpx.HTTPError as e:
            data = {"error": str(e)}

        credits = self.query_one("#credits", CreditsPanel)
        credits.data = data
        credits.refresh(layout=True)

        recent = data.get("recent_requests") or data.get("recent") or []
        self.query_one("#recent", RecentTable).update_entries(recent)

    def action_refresh(self) -> None:
        self.refresh_data()


def run(args=None) -> int:
    creds = load()
    if not creds.is_valid:
        sys.stderr.write(
            "\n  ERROR: no inferroute API key found.\n"
            "  Run `ir login` first.\n\n"
        )
        return 2
    StatusApp(creds).run()
    return 0
