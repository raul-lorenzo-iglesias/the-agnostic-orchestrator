"""Human-readable CLI formatting — borderless tables, status icons, compact values.

Imports only from ``models`` (respects the module DAG). Pure functions plus
one ``_Style`` helper. Stdlib only.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import UTC, datetime

# ---------------------------------------------------------------------------
# _Style — ANSI helper
# ---------------------------------------------------------------------------


class _Style:
    """Lazy ANSI color helper. Disables itself for NO_COLOR / non-TTY / dumb."""

    _CODES = {
        "bold": "\033[1m",
        "dim": "\033[2m",
        "red": "\033[31m",
        "green": "\033[32m",
        "yellow": "\033[33m",
        "cyan": "\033[36m",
        "gray": "\033[90m",
        "reset": "\033[0m",
    }

    def __init__(self) -> None:
        self.enabled = self._detect()

    @staticmethod
    def _detect() -> bool:
        if os.environ.get("FORCE_COLOR"):
            return True
        if os.environ.get("NO_COLOR") is not None:
            return False
        if os.environ.get("TERM") == "dumb":
            return False
        try:
            if not sys.stdout.isatty():
                return False
        except AttributeError:
            return False
        return True

    def __getattr__(self, name: str):  # noqa: ANN001
        code = self._CODES.get(name)
        if code is None:
            raise AttributeError(name)
        if self.enabled:
            def _wrap(text: str) -> str:
                return f"{code}{text}\033[0m"
        else:
            def _wrap(text: str) -> str:
                return text
        # cache on instance
        setattr(self, name, _wrap)
        return _wrap


# module-level default instance
style = _Style()


# ---------------------------------------------------------------------------
# Unicode / fallback detection
# ---------------------------------------------------------------------------

def _supports_unicode() -> bool:
    """Check if stdout can handle the status icons."""
    try:
        enc = sys.stdout.encoding or ""
    except AttributeError:
        return False
    return enc.lower().replace("-", "") in ("utf8", "utf16", "utf32", "utf8sig")


_UNICODE = _supports_unicode()

_ICONS_UNICODE = {
    "running": "\u25cf",   # ●
    "completed": "\u2714", # ✔
    "failed": "\u2718",    # ✘
    "blocked": "\u25c6",   # ◆
    "queued": "\u25cb",    # ○
    "stopped": "\u25cb",   # ○
    "cancelled": "\u2718", # ✘
    "succeeded": "\u2714", # ✔
    "pending": "\u25cb",   # ○
}

_ICONS_ASCII = {
    "running": "*",
    "completed": "+",
    "failed": "x",
    "blocked": "!",
    "queued": "-",
    "stopped": "-",
    "cancelled": "x",
    "succeeded": "+",
    "pending": "-",
}


def _icon(status: str) -> str:
    table = _ICONS_UNICODE if _UNICODE else _ICONS_ASCII
    return table.get(status, "?")


# ---------------------------------------------------------------------------
# ANSI-aware string width
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _visible_len(text: str) -> int:
    return len(_strip_ansi(text))


# ---------------------------------------------------------------------------
# Compact formatters
# ---------------------------------------------------------------------------


def fmt_duration(seconds: float | int | None) -> str:
    """Format seconds into compact human-readable duration. Max 2 units."""
    if not seconds:
        return "--"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m {sec}s" if sec else f"{m}m"
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h}h {m}m" if m else f"{h}h"


def fmt_tokens(n: int | None) -> str:
    """Format token count: 450, 12.4k, 1.2M."""
    if not n:
        return "--"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        v = n / 1000
        return f"{v:.1f}k" if v < 100 else f"{int(v)}k"
    v = n / 1_000_000
    return f"{v:.1f}M"


def fmt_cost(dollars: float | None) -> str:
    """Format cost: $0.04, <$0.01, $12.50."""
    if not dollars:
        return "--"
    if dollars < 0.01:
        return "<$0.01"
    if dollars < 10:
        return f"${dollars:.2f}"
    return f"${dollars:.0f}"


def fmt_relative_time(iso_str: str | None) -> str:
    """Format ISO timestamp as relative time: 'just now', '3m ago', '2h ago'."""
    if not iso_str:
        return "--"
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        delta = datetime.now(UTC) - dt
        secs = int(delta.total_seconds())
    except (ValueError, TypeError):
        return "--"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    days = secs // 86400
    return f"{days}d ago"


# ---------------------------------------------------------------------------
# fmt_status — icon + colored word
# ---------------------------------------------------------------------------

_STATUS_COLORS = {
    "running": "green",
    "completed": "green",
    "failed": "red",
    "blocked": "yellow",
    "queued": "dim",
    "stopped": "dim",
    "cancelled": "red",
}


def fmt_status(status: str) -> str:
    """Return colored icon + status word, e.g. '● running'."""
    color_name = _STATUS_COLORS.get(status, "dim")
    color_fn = getattr(style, color_name)
    icon = _icon(status)
    return color_fn(f"{icon} {status}")


# ---------------------------------------------------------------------------
# format_table — borderless table formatter
# ---------------------------------------------------------------------------


def format_table(
    headers: list[str],
    rows: list[list[str]],
    right_align: set[int] | None = None,
) -> str:
    """Render a borderless table with UPPERCASE headers.

    Args:
        headers: Column header strings (will be uppercased).
        rows: List of rows, each a list of cell strings.
        right_align: Set of column indices (0-based) to right-align.

    Returns:
        Formatted table string.
    """
    if not rows:
        return ""

    right_align = right_align or set()
    up_headers = [h.upper() for h in headers]

    # compute column widths using visible (non-ANSI) length
    num_cols = len(headers)
    widths = [_visible_len(h) for h in up_headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < num_cols:
                widths[i] = max(widths[i], _visible_len(cell))

    gutter = "  "
    lines: list[str] = []

    # header line
    hdr_parts: list[str] = []
    for i, h in enumerate(up_headers):
        if i in right_align:
            hdr_parts.append(h.rjust(widths[i]))
        else:
            hdr_parts.append(h.ljust(widths[i]))
    lines.append(style.bold(gutter.join(hdr_parts).rstrip()))

    # data rows
    for row in rows:
        parts: list[str] = []
        for i in range(num_cols):
            cell = row[i] if i < len(row) else ""
            vis_len = _visible_len(cell)
            pad = widths[i] - vis_len
            if i in right_align:
                parts.append(" " * pad + cell)
            else:
                parts.append(cell + " " * pad)
        lines.append(gutter.join(parts).rstrip())

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# High-level renderers
# ---------------------------------------------------------------------------


def render_task_list(tasks: list[dict], summaries: dict[int, dict]) -> str:
    """Render the task list view (table + footer).

    Args:
        tasks: List of task dicts from engine.list_tasks().
        summaries: Mapping of task_id -> summary dict from engine.summary().
    """
    if not tasks:
        return "No tasks. Use 'tao submit' to add one."

    term_width = shutil.get_terminal_size((80, 24)).columns
    # fixed column widths: ID(6) + gap(2) + STATUS(~16) + gap(2) + TIME(8) + gap(2) + COST(8)
    # = ~44 chars overhead. Title gets the rest.
    overhead = 6 + 2 + 16 + 2 + 8 + 2 + 8
    max_title = max(term_width - overhead, 15)

    headers = ["ID", "STATUS", "TITLE", "TIME", "COST"]
    rows: list[list[str]] = []
    for t in tasks:
        tid = t["task_id"]
        s = summaries.get(tid, {})
        title = t.get("title", "")
        if _visible_len(title) > max_title:
            title = title[: max_title - 1] + "\u2026"
        rows.append([
            f"#{tid}",
            fmt_status(t["status"]),
            title,
            fmt_duration(s.get("total_elapsed_s")),
            fmt_cost(s.get("total_cost_usd")),
        ])

    table = format_table(headers, rows, right_align={3, 4})

    # footer: count by status + total cost + total time
    counts: dict[str, int] = {}
    total_cost = 0.0
    total_time = 0.0
    for t in tasks:
        st = t["status"]
        counts[st] = counts.get(st, 0) + 1
        s = summaries.get(t["task_id"], {})
        total_cost += s.get("total_cost_usd", 0)
        total_time += s.get("total_elapsed_s", 0)

    n = len(tasks)
    task_word = "task" if n == 1 else "tasks"
    parts = [f"{n} {task_word}:"]
    for st in ("running", "blocked", "completed", "failed", "queued", "stopped"):
        c = counts.get(st, 0)
        if c:
            parts.append(f"{c} {st}")
    footer_left = " ".join(parts[:1]) + " " + ", ".join(parts[1:])
    footer_right_parts = []
    if total_cost:
        footer_right_parts.append(fmt_cost(total_cost))
    if total_time:
        footer_right_parts.append(fmt_duration(total_time))
    footer = footer_left
    if footer_right_parts:
        footer += "  |  " + "  |  ".join(footer_right_parts)

    return f"{table}\n\n{style.dim(footer)}"


def render_task_detail(
    task: dict,
    traces: list[dict],
    summary: dict,
) -> str:
    """Render the detail card for a single task."""
    tid = task["task_id"]
    title = task.get("title", "")
    header_text = f"Task #{tid} \u2014 {title}"
    sep = "\u2500" * min(_visible_len(header_text), shutil.get_terminal_size((80, 24)).columns)

    lines: list[str] = [
        style.bold(header_text),
        style.dim(sep),
        "",
    ]

    # key-value pairs
    kv: list[tuple[str, str]] = []
    kv.append(("Status", fmt_status(task["status"])))
    if task["status"] == "blocked" and task.get("blocked_reason"):
        kv.append(("Reason", task["blocked_reason"]))
    kv.append(("Duration", fmt_duration(summary.get("total_elapsed_s"))))
    tok_in = fmt_tokens(summary.get("total_tokens_in"))
    tok_out = fmt_tokens(summary.get("total_tokens_out"))
    if tok_in != "--" or tok_out != "--":
        kv.append(("Tokens", f"{tok_in} in / {tok_out} out"))
    kv.append(("Cost", fmt_cost(summary.get("total_cost_usd"))))
    created = task.get("created_at", "")
    if created:
        kv.append(("Created", f"{created} ({fmt_relative_time(created)})"))
    updated = task.get("updated_at", "")
    if updated:
        kv.append(("Updated", f"{updated} ({fmt_relative_time(updated)})"))

    label_width = max(len(k) for k, _ in kv)
    for k, v in kv:
        lines.append(f"  {k + ':':<{label_width + 1}}  {v}")

    # traces table
    if traces:
        lines.append("")
        lines.append(style.bold("Traces:"))
        t_headers = ["#", "ROLE", "MODEL", "TOKENS", "COST", "TIME", "OK"]
        t_rows: list[list[str]] = []
        for i, tr in enumerate(traces, 1):
            ok_status = "succeeded" if tr.get("success") else "failed"
            # for the last running trace, show running icon
            if i == len(traces) and task["status"] == "running" and tr.get("success") is None:
                ok_status = "running"
            tok = fmt_tokens((tr.get("tokens_in", 0) or 0) + (tr.get("tokens_out", 0) or 0))
            t_rows.append([
                str(i),
                tr.get("role", "--"),
                tr.get("model", "--"),
                tok,
                fmt_cost(tr.get("cost_usd")),
                fmt_duration(tr.get("elapsed_s")),
                fmt_status(ok_status).split(" ")[0],  # icon only
            ])
        trace_table = format_table(t_headers, t_rows, right_align={0, 3, 4, 5})
        # indent each line of the trace table
        for line in trace_table.split("\n"):
            lines.append(f"  {line}")

    return "\n".join(lines)


def render_traces(traces: list[dict]) -> str:
    """Render a standalone trace table."""
    if not traces:
        return "No traces."

    headers = ["#", "ROLE", "MODEL", "TOKENS IN", "TOKENS OUT", "COST", "TIME", "OK"]
    rows: list[list[str]] = []
    for i, tr in enumerate(traces, 1):
        ok = tr.get("success")
        if ok is True:
            ok_str = fmt_status("succeeded").split(" ")[0]
        elif ok is False:
            ok_str = fmt_status("failed").split(" ")[0]
        else:
            ok_str = fmt_status("running").split(" ")[0]
        rows.append([
            str(i),
            tr.get("role", "--"),
            tr.get("model", "--"),
            fmt_tokens(tr.get("tokens_in")),
            fmt_tokens(tr.get("tokens_out")),
            fmt_cost(tr.get("cost_usd")),
            fmt_duration(tr.get("elapsed_s")),
            ok_str,
        ])

    return format_table(headers, rows, right_align={0, 3, 4, 5, 6})


def render_summary(summary: dict) -> str:
    """Render a summary card for a task."""
    tid = summary.get("task_id", "?")
    header_text = f"Summary \u2014 Task #{tid}"
    sep = "\u2500" * _visible_len(header_text)

    lines: list[str] = [
        style.bold(header_text),
        style.dim(sep),
        "",
    ]

    kv: list[tuple[str, str]] = [
        ("Traces", str(summary.get("trace_count", 0))),
        ("Succeeded", str(summary.get("steps_succeeded", 0))),
        ("Failed", str(summary.get("steps_failed", 0))),
        ("Tokens in", fmt_tokens(summary.get("total_tokens_in"))),
        ("Tokens out", fmt_tokens(summary.get("total_tokens_out"))),
        ("Total cost", fmt_cost(summary.get("total_cost_usd"))),
        ("Total time", fmt_duration(summary.get("total_elapsed_s"))),
    ]

    label_width = max(len(k) for k, _ in kv)
    for k, v in kv:
        lines.append(f"  {k + ':':<{label_width + 1}}  {v}")

    return "\n".join(lines)
