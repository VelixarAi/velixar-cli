#!/usr/bin/env python3
"""`velixar vou` — the operator's oscilloscope for Velixar's governed operations.

WHY THIS IS NOT A REPORTING UTILITY
-----------------------------------
The requirement is that VOU be understandable WITHOUT the frontend. If you need a
browser to find out what the meter is doing, the implementation is incomplete. So
this is a first-class operational surface: three terminals — one running an MCP
client, one running `velixar vou watch`, one running `velixar vou status` — should
let you watch the governed intelligence economy operate in real time.

THIS FILE COMPUTES NO TOTALS.
Every number here is rendered, never derived. The backend owns aggregation
(`/v1/vou/*`), and the CLI, the MCP tools and the frontend all read the same
endpoints. Three surfaces each summing rows their own way would produce three
numbers, and the interesting question would stop being "how much VOU did this
consume" and become "which screen do you believe". Where you see arithmetic below
it is presentational only — column widths, bar lengths, elapsed seconds.

EVERY SURFACE IS LABELLED. A number that leaves here without BETA / NOT FOR
BILLING attached will be in a slide by Thursday.
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

BETA = "VOU BETA — NOT FOR BILLING"

#: State -> rich colour. UNKNOWN is deliberately not green: a status that cannot
#: measure itself must never LOOK like a healthy one, and colour is the first
#: thing an operator reads.
STATE_STYLE = {
    "GREEN": "bold green",
    "DEGRADED": "bold yellow",
    "BLOCKED": "bold magenta",
    "RED": "bold red",
    "UNKNOWN": "bold white on red",
}

FAMILY_ORDER = ("EXECUTION", "MEMORY", "KNOWLEDGE", "GOVERNANCE", "RESOURCE", "PROVENANCE")


def _api(method, path, **kwargs):
    """The ONE HTTP path. Imported late so this module can live beside the CLI
    without a circular import, and so there is still only one place that knows
    how a Velixar request is authenticated."""
    from velixar_cli import api
    return api(method, path, **kwargs)


def _get(endpoint, **params):
    # NOTE the parameter name: `view` is a QUERY parameter for /v1/vou/mcp, so a
    # positional called `view` collides with it and raises TypeError at runtime.
    query = {k: v for k, v in params.items() if v is not None}
    data = _api("GET", f"/v1/vou/{endpoint}", params=query)
    # The API wraps payloads; unwrap once, tolerantly, rather than guessing twice.
    return data.get("data", data) if isinstance(data, dict) else data


def _emit(payload, fmt):
    """json / jsonl fall through untouched so operators can pipe VOU anywhere."""
    if fmt == "json":
        click.echo(json.dumps(payload, indent=2, default=str))
        return True
    if fmt == "jsonl":
        rows = payload if isinstance(payload, list) else payload.get("entries") or \
            payload.get("rows") or payload.get("groups") or [payload]
        for row in rows:
            click.echo(json.dumps(row, default=str))
        return True
    return False


def _banner(extra=""):
    console.print(Text(f"  {BETA}{extra}", style="black on yellow"))


def _short(value, width=12):
    text = str(value or "—")
    return text if len(text) <= width else text[: width - 1] + "…"


def _clock(ts):
    if not ts:
        return "—"
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).strftime("%H:%M:%S.%f")[:12]
    except Exception:
        return str(ts)[:12]


@click.group()
def vou():
    """VOU beta meter — governed operations, live, from the terminal."""


# ══════════════════════════════════════════════════════════════════════════
# status
# ══════════════════════════════════════════════════════════════════════════
@vou.command()
@click.option("--hours", default=24, help="Window in hours")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
def status(hours, fmt):
    """Is the meter healthy — and can it even measure itself?"""
    data = _get("status", hours=hours)
    if _emit(data, fmt):
        return

    state = data.get("state", "UNKNOWN")
    style = STATE_STYLE.get(state, "bold white on red")

    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", justify="left")
    table.add_column()
    table.add_row("Meter", Text(state, style=style))
    table.add_row("Schedule", f"{data.get('schedule')} ({data.get('weights_evidence')})")
    table.add_row("Billing integration", Text(
        str(data.get("billing_integration")),
        style="green" if data.get("billing_integration") == "DISABLED" else "bold red"))
    table.add_row("Governed operations", str(data.get("governed_operations", "—")))
    table.add_row("Ledger entries", str(data.get("ledger_entries", "—")))
    table.add_row("Unprocessed events", str(data.get("unprocessed_events", "—")))

    p50, p95 = data.get("normalizer_lag_p50_ms"), data.get("normalizer_lag_p95_ms")
    # An unmeasured lag prints as "not measured", never as 0 — 0 would read as
    # "no lag", the most flattering possible reading of a missing measurement.
    lag = f"p50 {p50}ms · p95 {p95}ms" if p50 is not None else "not measured"
    table.add_row("Normalizer lag", lag)
    table.add_row("Last ledger append", str(data.get("last_ledger_append") or "—"))
    table.add_row("Emission failures", _count(data.get("emission_failures")))
    table.add_row("Attribution failures", _count(data.get("attribution_failures")))
    table.add_row("Duplicate suppressions", str(data.get("duplicate_suppressions", 0)))
    table.add_row("Meter gaps", _count(data.get("total_gaps")))

    console.print(Panel(table, title=f"VOU METER — {state}", border_style=style.split()[-1]))
    for reason in data.get("reasons", []):
        console.print(f"  • {reason}", style="dim")
    _banner()

    # A non-green meter exits non-zero so `velixar vou status` composes in CI and
    # in a shell loop without anyone parsing the text.
    if state not in ("GREEN",):
        sys.exit(1 if state in ("RED", "BLOCKED", "UNKNOWN") else 0)


def _count(value):
    if not value:
        return Text("0", style="green")
    return Text(str(value), style="bold red")


# ══════════════════════════════════════════════════════════════════════════
# watch — the live meter
# ══════════════════════════════════════════════════════════════════════════
@vou.command()
@click.option("--interval", default=2.0, help="Poll interval in seconds")
@click.option("--agent", default=None, help="Filter by agent id")
@click.option("--principal", default=None, help="Filter by principal id")
@click.option("--execution", default=None, help="Filter by execution id")
@click.option("--request", "request_id", default=None, help="Filter by request id")
@click.option("--client", default=None, help="Filter by MCP client")
@click.option("--tool", default=None, help="Filter by MCP tool")
@click.option("--family", default=None, help="Filter by operation family")
@click.option("--operation", default=None, help="Filter by operation type")
@click.option("--since", default=None, help="ISO timestamp to start from")
def watch(interval, agent, principal, execution, request_id, client, tool,
          family, operation, since):
    """Stream governed operations as they are normalized.

    Polls rather than streams: the backend has no push channel, and inventing one
    for a beta would be a second transport to maintain. The interval is yours to
    choose; the cost of each poll is one bounded, indexed query.
    """
    filters = {"agent_id": agent, "principal_id": principal, "execution_id": execution,
               "request_id": request_id, "mcp_client": client, "mcp_tool": tool,
               "family": family, "operation": operation}
    active = {k: v for k, v in filters.items() if v}

    cursor = since or (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    seen = set()
    session_total = 0.0
    per_request = {}

    _banner("   ·   watching — ctrl-c to stop")
    if active:
        console.print("  filters: " + "  ".join(f"{k}={v}" for k, v in active.items()),
                      style="dim")
    console.print()

    try:
        while True:
            payload = _get("recent", since=cursor, limit=200)
            entries = sorted(payload.get("entries", []),
                             key=lambda e: e.get("occurred_at") or "")
            # Count what is NEW this cycle. `entries` re-includes rows already
            # shown (the cursor is inclusive), so keying the footer off it
            # reprinted the totals on every empty poll -- a meter that looks busy
            # while nothing is happening is worse than one that looks idle.
            shown = 0
            last_request = None
            for entry in entries:
                key = entry.get("ledger_id")
                if key in seen:
                    continue
                seen.add(key)
                if not _matches(entry, active):
                    continue

                amount = float(entry.get("vou") or 0)
                session_total += amount
                req = entry.get("request_id") or "—"
                per_request[req] = per_request.get(req, 0.0) + amount

                dedup = " [dim](deduplicated)[/dim]" if entry.get("deduplicated") else ""
                origin = entry.get("mcp_client") or entry.get("channel") or "—"
                console.print(
                    f"[dim]{_clock(entry.get('occurred_at'))}[/dim]  "
                    f"[cyan]{_short(req, 10)}[/cyan]  "
                    f"[magenta]{_short(origin, 14)}[/magenta]  "
                    f"{entry.get('operation'):<24} "
                    f"[bold]{amount:+.2f}[/bold]{dedup}")
                detail = (f"             principal {_short(entry.get('principal_id'), 14)}"
                          f"   agent {_short(entry.get('agent_id'), 14)}"
                          f"   exec {_short(entry.get('execution_id'), 14)}")
                console.print(detail, style="dim")
                shown += 1
                last_request = req

                if entry.get("occurred_at"):
                    cursor = entry["occurred_at"]

            if shown:
                console.print(
                    f"  [dim]{'─' * 52}[/dim]\n"
                    f"  request total {per_request.get(last_request, 0):>8.2f} VOU"
                    f"     session total {session_total:>8.2f} VOU\n")
            # Trim the dedupe set so a long watch does not grow without bound.
            if len(seen) > 20000:
                seen.clear()
            time.sleep(max(0.25, interval))
    except KeyboardInterrupt:
        console.print(f"\n  session total: [bold]{session_total:.2f} VOU[/bold]")
        _banner()


def _matches(entry, filters):
    for key, wanted in filters.items():
        if key == "family":
            if (entry.get("family") or "").upper() != wanted.upper():
                return False
        elif key == "operation":
            if entry.get("operation") != wanted:
                return False
        elif entry.get(key) != wanted:
            return False
    return True


# ══════════════════════════════════════════════════════════════════════════
# tail
# ══════════════════════════════════════════════════════════════════════════
@vou.command()
@click.option("--limit", "-n", default=20, help="How many entries")
@click.option("--since", default=None, help="ISO timestamp")
@click.option("--format", "fmt", type=click.Choice(["table", "json", "jsonl"]),
              default="table")
def tail(limit, since, fmt):
    """Recent normalized ledger entries."""
    data = _get("recent", limit=limit, since=since)
    if _emit(data, fmt):
        return

    table = Table(title=f"VOU tail — {BETA}", header_style="bold")
    for col in ("TIME", "OPERATION", "VOU", "AGENT", "EXECUTION", "CLIENT"):
        table.add_column(col, justify="right" if col == "VOU" else "left")
    for e in data.get("entries", []):
        table.add_row(_clock(e.get("occurred_at")), e.get("operation") or "—",
                      f"{float(e.get('vou') or 0):.2f}",
                      _short(e.get("agent_id"), 14), _short(e.get("execution_id"), 16),
                      _short(e.get("mcp_client"), 14))
    console.print(table)
    _banner()


# ══════════════════════════════════════════════════════════════════════════
# explain
# ══════════════════════════════════════════════════════════════════════════
@vou.command()
@click.argument("target")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
def explain(target, fmt):
    """Why did this execution / request / ledger entry consume that much VOU?"""
    data = _get("explain", id=target)
    if _emit(data, fmt):
        return
    if not data.get("found"):
        console.print(f"[yellow]No governed operations recorded for[/yellow] {target}")
        console.print(f"  {data.get('note', '')}", style="dim")
        return

    head = Table.grid(padding=(0, 2))
    head.add_column(style="dim")
    head.add_column()
    for label, key in (("MCP client", "mcp_client"), ("MCP tool", "mcp_tool"),
                       ("Channel", "channel"), ("Workspace", "workspace_id"),
                       ("Principal", "principal_id"), ("Agent", "agent_id"),
                       ("Execution", "execution_id"), ("Schedule", "schedule")):
        if data.get(key):
            head.add_row(label, str(data[key]))
    head.add_row("Weight status", str(data.get("weight_status")))
    console.print(Panel(head, title=f"{data.get('target_kind', 'target')} {target}"))

    table = Table(box=None)
    table.add_column("OPERATION")
    table.add_column("COUNT", justify="right")
    table.add_column("WEIGHT", justify="right")
    table.add_column("VOU", justify="right")
    for line in data.get("lines", []):
        table.add_row(line["operation"], str(line["count"]),
                      f"{line['weight']:.2f}", f"{line['vou']:.2f}")
    table.add_row("", "", "", "")
    table.add_row(Text("TOTAL", style="bold"), "", "",
                  Text(f"{data.get('total_vou', 0):.2f}", style="bold"))
    console.print(table)

    classes = data.get("rating_classes") or {}
    if classes:
        console.print("  " + "   ".join(f"{k} {v:.2f}" for k, v in classes.items()),
                      style="dim")
    completeness = data.get("meter_completeness")
    console.print(f"\n  Commercial status: {data.get('commercial_status')}")
    console.print(f"  Meter completeness: {completeness}",
                  style="green" if completeness == "COMPLETE" else "yellow")
    console.print(f"  [dim]{data.get('completeness_note', '')}[/dim]")
    console.print(f"  [dim]explanation {data.get('derived_from', '')}[/dim]")
    _banner()


# ══════════════════════════════════════════════════════════════════════════
# execution — the tree
# ══════════════════════════════════════════════════════════════════════════
@vou.command()
@click.argument("execution_id")
@click.option("--tree", is_flag=True, help="Show the operation tree")
@click.option("--agents", is_flag=True, help="Show VOU per agent")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
def execution(execution_id, tree, agents, fmt):
    """Inspect one execution, including nested agents and swarms."""
    data = _get("explain", id=execution_id, tree="true")
    if _emit(data, fmt):
        return
    if not data.get("found"):
        console.print(f"[yellow]No operations for execution[/yellow] {execution_id}")
        return

    if agents:
        table = Table(title=f"VOU by agent — {BETA}")
        table.add_column("AGENT")
        table.add_column("VOU", justify="right")
        for name, amount in sorted(data.get("agents", {}).items(),
                                   key=lambda kv: -kv[1]):
            table.add_row(name, f"{amount:.2f}")
        console.print(table)
        console.print(f"  [dim]{data.get('note', '')}[/dim]")
        _banner()
        return

    nodes = data.get("nodes", {})
    console.print(f"\n[bold]{execution_id}[/bold]"
                  f"{'':<8}[bold]{data.get('total_vou', 0):.2f} VOU[/bold]")
    roots = [k for k, n in nodes.items() if not n.get("parent_execution_id")]
    for root in roots or list(nodes):
        _print_node(nodes, root, prefix="")
    console.print(f"\n  [dim]{data.get('note', '')}[/dim]")
    _banner()


def _print_node(nodes, key, prefix):
    node = nodes.get(key) or {}
    children = [k for k, n in nodes.items() if n.get("parent_execution_id") == key]
    ops = node.get("operations", [])
    for i, op in enumerate(ops):
        last = (i == len(ops) - 1) and not children
        branch = "└── " if last else "├── "
        times = f" ×{op['count']}" if op.get("count", 1) > 1 else ""
        console.print(f"{prefix}{branch}{op['operation']}{times}"
                      f"{'':<{max(1, 26 - len(op['operation']) - len(times))}}"
                      f"{op['vou']:.2f}")
    for j, child in enumerate(children):
        last = j == len(children) - 1
        console.print(f"{prefix}{'└── ' if last else '├── '}"
                      f"[cyan]{_short(child, 24)}[/cyan]"
                      f"{'':<4}{nodes[child].get('vou', 0):.2f} VOU"
                      f"   [dim]{nodes[child].get('agent_id') or ''}[/dim]")
        _print_node(nodes, child, prefix + ("    " if last else "│   "))


# ══════════════════════════════════════════════════════════════════════════
# mcp
# ══════════════════════════════════════════════════════════════════════════
@vou.group()
def mcp():
    """MCP-shaped views: which client, which tool, which session."""


def _mcp_view(view, fmt):
    data = _get("mcp", view=view)
    if _emit(data, fmt):
        return
    table = Table(title=f"VOU by MCP {view} — {BETA}")
    table.add_column(view.rstrip("s").upper())
    for col in ("VOU", "OPERATIONS", "REQUESTS", "EXECUTIONS", "FAILURES"):
        table.add_column(col, justify="right")
    table.add_column("LAST SEEN")
    for g in data.get("groups", []):
        table.add_row(str(g["name"]), f"{g['vou']:.2f}", str(g["operations"]),
                      str(g["requests"]), str(g["executions"]),
                      str(g["failures"]), _clock(g.get("last_seen")))
    console.print(table)
    console.print(f"  [dim]{data.get('note', '')}[/dim]")
    _banner()


@mcp.command("clients")
@click.option("--format", "fmt", type=click.Choice(["table", "json", "jsonl"]), default="table")
def mcp_clients(fmt):
    """VOU per connected MCP client."""
    _mcp_view("clients", fmt)


@mcp.command("tools")
@click.option("--format", "fmt", type=click.Choice(["table", "json", "jsonl"]), default="table")
def mcp_tools(fmt):
    """VOU per MCP tool — a ROLLUP over semantic operations, not a unit of metering."""
    _mcp_view("tools", fmt)


@mcp.command("sessions")
@click.option("--format", "fmt", type=click.Choice(["table", "json", "jsonl"]), default="table")
def mcp_sessions(fmt):
    """VOU per MCP session."""
    _mcp_view("sessions", fmt)


@mcp.command("watch")
@click.option("--interval", default=2.0)
@click.pass_context
def mcp_watch(ctx, interval):
    """Watch only MCP-channel activity."""
    ctx.invoke(watch, interval=interval, agent=None, principal=None, execution=None,
               request_id=None, client=None, tool=None, family=None, operation=None,
               since=None)


# ══════════════════════════════════════════════════════════════════════════
# coverage / gaps / reconcile / summary / export
# ══════════════════════════════════════════════════════════════════════════
@vou.command()
@click.option("--hours", default=24)
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
def coverage(hours, fmt):
    """THREE coverage numbers — instrumented, exercised, and calibratable."""
    data = _get("coverage", hours=hours)
    if _emit(data, fmt):
        return

    cat, run, res = data.get("catalog", {}), data.get("runtime", {}), data.get("resource", {})
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim")
    grid.add_column(justify="right")
    grid.add_column()
    grid.add_row("CATALOG", _ratio(cat.get("with_emitter"), cat.get("declared"), cat.get("pct")),
                 "operations with a proved emitter — is the platform instrumented?")
    grid.add_row("RUNTIME", _ratio(run.get("observed"), run.get("expected"), run.get("pct")),
                 "of those emitters, how many actually ran")
    grid.add_row("RESOURCE", _ratio(res.get("events_with_evidence"),
                                    res.get("events_requiring_evidence"), res.get("pct")),
                 "events carrying q_r — can we calibrate the economics?")
    console.print(Panel(grid, title=f"VOU coverage — {BETA}"))
    console.print(f"  [yellow]{res.get('why_it_matters', '')}[/yellow]")

    missing = cat.get("missing_emitter") or []
    if missing:
        console.print(f"\n  No emitter ({len(missing)}):", style="bold")
        console.print("    " + "  ".join(missing), style="dim")
    idle = run.get("not_exercised") or []
    if idle:
        console.print(f"\n  Has an emitter, did not run ({len(idle)}):", style="bold")
        console.print("    " + "  ".join(idle), style="dim")
    unclassified = data.get("unclassified_operations") or []
    if unclassified:
        console.print("\n  Observed but NOT in the catalog:", style="bold yellow")
        for name in unclassified:
            console.print(f"    · {name}")
    _banner()


def _ratio(num, den, pct):
    if den in (None, 0):
        return Text("— / —", style="dim")
    style = "green" if (pct or 0) >= 80 else "yellow" if (pct or 0) >= 40 else "red"
    return Text(f"{num}/{den}  {pct}%", style=style)


@vou.command()
@click.option("--hours", default=24)
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
def resources(hours, fmt):
    """q_r completeness — by resource AND by operation."""
    data = _get("completeness", hours=hours)
    if _emit(data, fmt):
        return

    by_state = data.get("by_state") or {}
    console.print(Panel(
        "  ".join(f"[bold]{k}[/bold] {v}" for k, v in by_state.items()),
        title=f"Resource evidence — {BETA}"))

    table = Table(title="BY RESOURCE", box=None)
    table.add_column("RESOURCE")
    table.add_column("OBSERVED", justify="right")
    table.add_column("EXPECTED", justify="right")
    table.add_column("%", justify="right")
    table.add_column("")
    for kind, v in (data.get("by_resource") or {}).items():
        pct = v.get("pct")
        table.add_row(kind, str(v.get("observed")), str(v.get("expected_events")),
                      "—" if pct is None else f"{pct}%",
                      Text("█" * int((pct or 0) / 5), style=_pct_style(pct)))
    console.print(table)

    ops = Table(title="OPERATION RESOURCE COMPLETENESS", box=None)
    ops.add_column("OPERATION")
    ops.add_column("STATUS")
    ops.add_column("%", justify="right")
    ops.add_column("EVENTS", justify="right")
    ops.add_column("MISSING DIMENSIONS")
    for name, v in (data.get("by_operation") or {}).items():
        pct = v.get("pct")
        missing = ", ".join(f"{k}×{n}" for k, n in (v.get("missing_dimensions") or {}).items())
        ops.add_row(name, Text(str(v.get("status")), style=_status_style(v.get("status"))),
                    "—" if pct is None else f"{pct}%", str(v.get("events")), missing or "—")
    console.print(ops)
    console.print(f"  [dim]{data.get('denominator', '')}[/dim]")

    drift = data.get("profile_drift") or {}
    for key, label in (("observed_but_not_declared", "profile too NARROW"),
                       ("declared_but_never_observed", "profile too WIDE / layer uninstrumented")):
        rows = drift.get(key) or {}
        if rows:
            console.print(f"\n  [yellow]{label}[/yellow]", style="bold")
            for op, dims in rows.items():
                console.print(f"    {op}: {', '.join(dims)}", style="dim")
    _banner()


def _pct_style(pct):
    return "green" if (pct or 0) >= 80 else "yellow" if (pct or 0) >= 30 else "red"


def _status_style(status):
    return {"COMPLETE": "green", "PARTIAL": "yellow",
            "NOT_REQUIRED": "dim", "UNMEASURED": "red"}.get(status, "white")


@vou.group()
def calibrate():
    """Is VOU an economically measured unit yet?"""


@calibrate.command("status")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
def calibrate_status(fmt):
    """q_r versus k_r readiness. Makes 'working meter' and 'calibrated unit' distinct."""
    data = _get("calibrate")
    if _emit(data, fmt):
        return
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim")
    grid.add_column(justify="right")
    qr = data.get("q_r_completeness_pct")
    grid.add_row("q_r completeness", "—" if qr is None else f"{qr}%")
    grid.add_row("k_r measured", f"{data.get('k_r_measured')} / {data.get('k_r_total')}")
    total = data.get("weights_total")
    grid.add_row("beta weights empirical", f"{data.get('weights_empirical')} / {total}")
    grid.add_row("beta weights assumption", f"{data.get('weights_assumption')} / {total}")
    grid.add_row("beta weights unruled", f"{data.get('weights_unruled')} / {total}")
    grid.add_row("anchor", f"{data.get('anchor')} = {data.get('anchor_weight')}")
    grid.add_row("anchor status", str(data.get("anchor_status")))
    console.print(Panel(grid, title=f"VOU CALIBRATION — {BETA}"))

    ready = data.get("economic_calibration") == "READY"
    console.print(Text(f"  ECONOMIC CALIBRATION: {data.get('economic_calibration')}",
                       style="bold green" if ready else "bold red"))
    console.print(f"  reason: {data.get('reason')}", style="dim")

    for key, label in (("waiting_for_cost", "waiting only on k_r (no engineering needed)"),
                       ("waiting_for_resource_evidence", "waiting on q_r (needs an emitter)")):
        names = data.get(key) or []
        if names:
            console.print(f"\n  {label} — {len(names)}:", style="bold")
            console.print("    " + "  ".join(names), style="dim")
    _banner()


@calibrate.command("unruled")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
def calibrate_unruled(fmt):
    """The unruled weights, with the beta evidence a ruling would rest on."""
    data = _get("calibrate", view="unruled")
    if _emit(data, fmt):
        return
    table = Table(title=f"Unruled weights ({data.get('count')}) — {BETA}")
    for col in ("OPERATION", "PRODUCTION STATE", "PROVISIONAL", "OBSERVATIONS",
                "q_r", "CALIBRATION STATE"):
        table.add_column(col, justify="right" if col in ("PROVISIONAL", "OBSERVATIONS", "q_r") else "left")
    for row in data.get("operations", []):
        qr = row.get("q_r_available")
        table.add_row(row["operation"], str(row["production_state"]),
                      f"{row['provisional_weight']:.2f}", str(row["beta_observations"]),
                      "—" if qr is None else f"{qr}%", str(row["calibration_state"]))
    console.print(table)
    console.print(f"  [yellow]{data.get('note', '')}[/yellow]")
    _banner()


@vou.command()
@click.option("--hours", default=24)
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
def gaps(hours, fmt):
    """Where the meter FAILED to measure. A total without this is a claim."""
    data = _get("gaps", hours=hours)
    if _emit(data, fmt):
        return
    total = data.get("total", 0)
    console.print(Panel(
        Text(f"{total} gap(s) detected", style="green" if not total else "bold red"),
        title=f"VOU gaps — {BETA}"))
    for reason, count in (data.get("by_reason") or {}).items():
        style = "bold red" if reason in ("no_authorized_context", "emit_failed") else "yellow"
        console.print(f"  {count:>4}  [{style}]{reason}[/{style}]")
    if data.get("unnormalized_events"):
        console.print(f"  {data['unnormalized_events']:>4}  [yellow]events awaiting "
                      f"normalization[/yellow]")
    if data.get("operations_without_resource_evidence"):
        console.print(f"  {data['operations_without_resource_evidence']:>4}  "
                      f"[yellow]operations with no resource evidence[/yellow]")
    for row in (data.get("recent") or [])[:10]:
        console.print(f"    [dim]{_clock(row.get('occurred_at'))}  {row.get('reason')}  "
                      f"{row.get('operation_type') or ''}  {(row.get('detail') or '')[:60]}[/dim]")
    console.print(f"\n  [dim]{data.get('note', '')}[/dim]")
    _banner()


@vou.command()
@click.option("--hours", default=24)
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
def reconcile(hours, fmt):
    """Do events, ledger, rollups and the legacy meter agree?"""
    data = _get("reconcile", hours=hours)
    if _emit(data, fmt):
        return
    ok = data.get("status") == "OK"
    console.print(Panel(
        Text(data.get("status", "?"), style="bold green" if ok else "bold red"),
        title=f"VOU reconcile — {BETA}"))
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim")
    grid.add_column(justify="right")
    grid.add_row("Governed operations", str(data.get("governed_operations")))
    grid.add_row("Ledger entries", str(data.get("ledger_entries")))
    grid.add_row("Ledger total VOU", f"{data.get('ledger_total_vou', 0):.4f}")
    rollup = data.get("rollup_total_vou")
    grid.add_row("Rollup total VOU", "—" if rollup is None else f"{rollup:.4f}")
    console.print(grid)

    for finding in data.get("findings", []):
        console.print(f"\n  [bold red]{finding['code']}[/bold red]  ×{finding['count']}")
        console.print(f"    {finding['what']}", style="dim")
        console.print(f"    [yellow]→ {finding['recommended_action']}[/yellow]")

    legacy = data.get("legacy_comparison") or {}
    if legacy.get("available"):
        console.print(f"\n  [dim]{legacy.get('explained_difference', '')}[/dim]")
        console.print(f"  [dim]{legacy.get('caveat', '')}[/dim]")
    else:
        console.print(f"\n  [dim]legacy comparison unavailable: "
                      f"{legacy.get('reason', 'unknown')}[/dim]")
    console.print(f"\n  [dim]{data.get('note', '')}[/dim]")
    _banner()
    if not ok:
        sys.exit(1)


@vou.command()
@click.option("--since", default=None)
@click.option("--until", default=None)
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
def summary(since, until, fmt):
    """This workspace's VOU, by family and operation."""
    data = _get("summary", since=since, until=until)
    if _emit(data, fmt):
        return
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim")
    grid.add_column(justify="right")
    grid.add_row("Total VOU", f"{data.get('total_vou', 0):.2f}")
    grid.add_row("Governed operations", str(data.get("governed_operations")))
    grid.add_row("Zero-rated by policy", f"{data.get('zero_rated_by_policy_vou', 0):.2f}")
    grid.add_row("Held by beta interlock",
                 f"{data.get('beta_interlock_suppressed_vou', 0):.2f}")
    grid.add_row("Deduplicated operations", str(data.get("deduplicated_operations", 0)))
    grid.add_row("Meter gaps", str(data.get("meter_gaps", 0)))
    console.print(Panel(grid, title=f"VOU summary — {BETA}"))

    families = data.get("vou_by_family") or {}
    if families:
        peak = max(families.values()) or 1
        for family in FAMILY_ORDER:
            amount = families.get(family, 0)
            bar = "█" * int(24 * amount / peak) if amount else ""
            console.print(f"  {family:<12} {amount:>9.2f}  [cyan]{bar}[/cyan]")

    table = Table(title="Top operations", box=None)
    table.add_column("OPERATION")
    table.add_column("VOU", justify="right")
    table.add_column("COUNT", justify="right")
    for name, agg in list((data.get("vou_by_operation") or {}).items())[:12]:
        table.add_row(name, f"{agg['vou']:.2f}", str(agg["count"]))
    console.print(table)
    console.print(f"  [dim]{data.get('coverage_note', '')}[/dim]")
    _banner()


@vou.command("export")
@click.option("--since", default=None)
@click.option("--until", default=None)
@click.option("--limit", default=1000)
@click.option("--format", "fmt", type=click.Choice(["jsonl", "csv", "json"]),
              default="jsonl")
@click.option("--out", type=click.Path(), default=None, help="Write to a file")
def export_cmd(since, until, limit, fmt, out):
    """Export this workspace's ledger entries. Metadata only, never payloads."""
    data = _get("export", since=since, until=until, limit=limit)
    rows = data.get("rows", [])

    if fmt == "csv":
        import csv
        import io
        buf = io.StringIO()
        if rows:
            keys = sorted({k for row in rows for k in row})
            writer = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        payload = buf.getvalue()
    elif fmt == "json":
        payload = json.dumps(rows, indent=2, default=str)
    else:
        payload = "\n".join(json.dumps(r, default=str) for r in rows)

    if out:
        with open(out, "w") as handle:
            handle.write(payload)
        console.print(f"  {len(rows)} rows → {out}")
        console.print(f"  [dim]{data.get('format_note', '')}[/dim]")
        _banner()
    else:
        click.echo(payload)


@vou.command()
@click.argument("ledger_id")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="json")
def inspect(ledger_id, fmt):
    """Full attribution record for one ledger entry."""
    data = _get("explain", id=ledger_id)
    if _emit(data, fmt):
        return
    console.print(json.dumps(data, indent=2, default=str))
