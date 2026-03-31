"""Tests for tao.fmt — pure-function formatting, no DB required."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from src.fmt import (
    _strip_ansi,
    _Style,
    _visible_len,
    fmt_cost,
    fmt_duration,
    fmt_relative_time,
    fmt_status,
    fmt_tokens,
    format_table,
    render_summary,
    render_task_detail,
    render_task_list,
    render_traces,
)

# ---------------------------------------------------------------------------
# TestStyle
# ---------------------------------------------------------------------------


class TestStyle:
    def test_style_enabled_produces_ansi(self):
        s = _Style()
        s.enabled = True
        result = s.green("hello")
        assert "\033[32m" in result
        assert "\033[0m" in result
        assert "hello" in result

    def test_style_disabled_returns_plain(self):
        s = _Style()
        s.enabled = False
        result = s.green("hello")
        assert result == "hello"
        assert "\033[" not in result

    def test_style_unknown_attr_raises(self):
        s = _Style()
        with pytest.raises(AttributeError):
            s.nonexistent("x")

    def test_style_caches_method(self):
        s = _Style()
        s.enabled = True
        fn1 = s.bold
        fn2 = s.bold
        assert fn1 is fn2

    def test_no_color_env_disables(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "")
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        s = _Style()
        assert s.enabled is False

    def test_force_color_env_enables(self, monkeypatch):
        monkeypatch.setenv("FORCE_COLOR", "1")
        s = _Style()
        assert s.enabled is True

    def test_dumb_term_disables(self, monkeypatch):
        monkeypatch.setenv("TERM", "dumb")
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        monkeypatch.delenv("NO_COLOR", raising=False)
        s = _Style()
        assert s.enabled is False

    def test_non_tty_disables(self, monkeypatch):
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("TERM", raising=False)
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = False
            s = _Style()
            assert s.enabled is False

    def test_all_codes_exist(self):
        s = _Style()
        s.enabled = True
        for name in ("bold", "dim", "red", "green", "yellow", "cyan", "gray"):
            result = getattr(s, name)("test")
            assert "test" in result


# ---------------------------------------------------------------------------
# TestStripAnsi
# ---------------------------------------------------------------------------


class TestStripAnsi:
    def test_strip_ansi_removes_codes(self):
        assert _strip_ansi("\033[32mhello\033[0m") == "hello"

    def test_strip_ansi_preserves_plain(self):
        assert _strip_ansi("hello") == "hello"

    def test_visible_len_counts_correctly(self):
        assert _visible_len("\033[1m\033[32mhi\033[0m") == 2
        assert _visible_len("hello") == 5


# ---------------------------------------------------------------------------
# TestCompactFormatters
# ---------------------------------------------------------------------------


class TestFmtDuration:
    def test_zero_returns_dash(self):
        assert fmt_duration(0) == "--"

    def test_none_returns_dash(self):
        assert fmt_duration(None) == "--"

    def test_seconds_only(self):
        assert fmt_duration(45) == "45s"

    def test_minutes_and_seconds(self):
        assert fmt_duration(192) == "3m 12s"

    def test_exact_minutes(self):
        assert fmt_duration(120) == "2m"

    def test_hours_and_minutes(self):
        assert fmt_duration(8100) == "2h 15m"

    def test_exact_hours(self):
        assert fmt_duration(3600) == "1h"

    def test_float_input(self):
        assert fmt_duration(45.7) == "45s"


class TestFmtTokens:
    def test_zero_returns_dash(self):
        assert fmt_tokens(0) == "--"

    def test_none_returns_dash(self):
        assert fmt_tokens(None) == "--"

    def test_small_number(self):
        assert fmt_tokens(450) == "450"

    def test_thousands(self):
        assert fmt_tokens(12400) == "12.4k"

    def test_large_thousands(self):
        assert fmt_tokens(145000) == "145k"

    def test_millions(self):
        assert fmt_tokens(1200000) == "1.2M"

    def test_exact_thousand(self):
        assert fmt_tokens(1000) == "1.0k"


class TestFmtCost:
    def test_zero_returns_dash(self):
        assert fmt_cost(0) == "--"

    def test_none_returns_dash(self):
        assert fmt_cost(None) == "--"

    def test_tiny_cost(self):
        assert fmt_cost(0.001) == "<$0.01"

    def test_normal_cost(self):
        assert fmt_cost(0.04) == "$0.04"

    def test_dollar_plus(self):
        assert fmt_cost(1.23) == "$1.23"

    def test_large_cost(self):
        assert fmt_cost(12.50) == "$12"


class TestFmtRelativeTime:
    def test_none_returns_dash(self):
        assert fmt_relative_time(None) == "--"

    def test_empty_returns_dash(self):
        assert fmt_relative_time("") == "--"

    def test_invalid_returns_dash(self):
        assert fmt_relative_time("not-a-date") == "--"

    def test_just_now(self):
        now = datetime.now(UTC).isoformat()
        assert fmt_relative_time(now) == "just now"

    def test_minutes_ago(self):
        t = datetime.now(UTC) - timedelta(minutes=5)
        assert fmt_relative_time(t.isoformat()) == "5m ago"

    def test_hours_ago(self):
        t = datetime.now(UTC) - timedelta(hours=3)
        assert fmt_relative_time(t.isoformat()) == "3h ago"

    def test_days_ago(self):
        t = datetime.now(UTC) - timedelta(days=2)
        assert fmt_relative_time(t.isoformat()) == "2d ago"

    def test_naive_datetime_treated_as_utc(self):
        t = datetime.now(UTC) - timedelta(minutes=10)
        naive_str = t.strftime("%Y-%m-%d %H:%M:%S")
        result = fmt_relative_time(naive_str)
        assert "ago" in result


# ---------------------------------------------------------------------------
# TestFmtStatus
# ---------------------------------------------------------------------------


class TestFmtStatus:
    def test_running_contains_word(self):
        result = _strip_ansi(fmt_status("running"))
        assert "running" in result

    def test_completed_contains_word(self):
        result = _strip_ansi(fmt_status("completed"))
        assert "completed" in result

    def test_failed_contains_word(self):
        result = _strip_ansi(fmt_status("failed"))
        assert "failed" in result

    def test_blocked_contains_word(self):
        result = _strip_ansi(fmt_status("blocked"))
        assert "blocked" in result

    def test_queued_contains_word(self):
        result = _strip_ansi(fmt_status("queued"))
        assert "queued" in result

    def test_stopped_contains_word(self):
        result = _strip_ansi(fmt_status("stopped"))
        assert "stopped" in result

    def test_all_have_icon(self):
        for st in ("running", "completed", "failed", "blocked", "queued", "stopped"):
            result = _strip_ansi(fmt_status(st))
            # icon + space + word
            assert len(result.split()) == 2

    def test_with_color_enabled(self):
        import src.fmt as fmt_mod
        old_style = fmt_mod.style
        try:
            fmt_mod.style = _Style()
            fmt_mod.style.enabled = True
            result = fmt_status("running")
            assert "\033[" in result
        finally:
            fmt_mod.style = old_style

    def test_with_color_disabled(self):
        import src.fmt as fmt_mod
        old_style = fmt_mod.style
        try:
            fmt_mod.style = _Style()
            fmt_mod.style.enabled = False
            result = fmt_status("failed")
            assert "\033[" not in result
        finally:
            fmt_mod.style = old_style


# ---------------------------------------------------------------------------
# TestFormatTable
# ---------------------------------------------------------------------------


class TestFormatTable:
    def test_empty_rows_returns_empty(self):
        assert format_table(["A", "B"], []) == ""

    def test_headers_uppercased(self):
        result = format_table(["name", "age"], [["alice", "30"]])
        plain = _strip_ansi(result)
        lines = plain.split("\n")
        assert "NAME" in lines[0]
        assert "AGE" in lines[0]

    def test_columns_aligned(self):
        result = format_table(
            ["id", "name"],
            [["1", "short"], ["2", "a much longer name"]],
        )
        plain = _strip_ansi(result)
        lines = plain.split("\n")
        assert len(lines) == 3  # header + 2 rows

    def test_right_align(self):
        result = format_table(
            ["name", "count"],
            [["foo", "5"], ["bar", "123"]],
            right_align={1},
        )
        plain = _strip_ansi(result)
        lines = plain.split("\n")
        # "5" should be right-padded to align with "123"
        # The count column values should end at the same position
        for line in lines[1:]:
            parts = line.rstrip().split("  ")
            # last non-empty part is the count
            count_part = [p for p in parts if p.strip()][-1]
            assert count_part.strip() in ("5", "123")

    def test_gutter_spacing(self):
        result = format_table(["a", "b"], [["x", "y"]])
        plain = _strip_ansi(result)
        # columns separated by 2 spaces (gutter)
        assert "  " in plain

    def test_ansi_width_calc(self):
        """ANSI codes should not affect column width calculation."""
        import src.fmt as fmt_mod
        old_style = fmt_mod.style
        try:
            fmt_mod.style = _Style()
            fmt_mod.style.enabled = True
            colored_cell = fmt_mod.style.green("hi")
            result = format_table(
                ["val", "x"], [[colored_cell, "a"], ["hello", "b"]],
            )
            plain = _strip_ansi(result)
            lines = plain.split("\n")
            # Both data rows should have consistent column positioning
            # "hi" row should be padded to same width as "hello"
            assert len(lines) == 3
            # Column 1 starts at same position in both data rows
            assert lines[1].index("a") == lines[2].index("b")
        finally:
            fmt_mod.style = old_style


# ---------------------------------------------------------------------------
# TestRenderers
# ---------------------------------------------------------------------------


class TestRenderTaskList:
    def _make_task(self, tid: int, title: str, status: str = "queued") -> dict:
        return {
            "task_id": tid,
            "title": title,
            "status": status,
            "created_at": "2026-03-19 14:00:00",
            "updated_at": "2026-03-19 14:00:00",
        }

    def _make_summary(self, cost: float = 0, elapsed: float = 0) -> dict:
        return {
            "total_cost_usd": cost,
            "total_elapsed_s": elapsed,
            "total_tokens_in": 0,
            "total_tokens_out": 0,
        }

    def test_empty_shows_hint(self):
        result = render_task_list([], {})
        assert "No tasks" in result
        assert "tao submit" in result

    def test_single_task(self):
        tasks = [self._make_task(1, "Fix auth", "running")]
        sums = {1: self._make_summary(0.04, 192)}
        result = _strip_ansi(render_task_list(tasks, sums))
        assert "#1" in result
        assert "running" in result
        assert "Fix auth" in result
        assert "1 task:" in result

    def test_multiple_tasks(self):
        tasks = [
            self._make_task(1, "Fix auth", "running"),
            self._make_task(2, "Add cache", "blocked"),
            self._make_task(3, "Update errors", "completed"),
        ]
        sums = {
            1: self._make_summary(0.04, 192),
            2: self._make_summary(0, 0),
            3: self._make_summary(0.48, 765),
        }
        result = _strip_ansi(render_task_list(tasks, sums))
        assert "#1" in result
        assert "#2" in result
        assert "#3" in result
        assert "3 tasks:" in result
        assert "1 running" in result
        assert "1 blocked" in result
        assert "1 completed" in result

    def test_footer_total_cost(self):
        tasks = [
            self._make_task(1, "A", "completed"),
            self._make_task(2, "B", "completed"),
        ]
        sums = {
            1: self._make_summary(0.25, 60),
            2: self._make_summary(0.75, 120),
        }
        result = _strip_ansi(render_task_list(tasks, sums))
        assert "$1.00" in result

    def test_long_title_truncated(self):
        long_title = "A" * 200
        tasks = [self._make_task(1, long_title, "queued")]
        sums = {1: self._make_summary()}
        result = _strip_ansi(render_task_list(tasks, sums))
        # title should not be 200 chars
        lines = result.split("\n")
        for line in lines:
            assert len(line) < 200


class TestRenderTaskDetail:
    def _make_task(self, **overrides: object) -> dict:
        base = {
            "task_id": 1,
            "title": "Fix auth module",
            "status": "running",
            "created_at": "2026-03-19 14:29:00",
            "updated_at": "2026-03-19 14:32:00",
        }
        base.update(overrides)
        return base

    def _make_summary(self, **overrides: object) -> dict:
        base = {
            "task_id": 1,
            "total_cost_usd": 0.04,
            "total_elapsed_s": 192,
            "total_tokens_in": 12400,
            "total_tokens_out": 3200,
            "steps_succeeded": 2,
            "steps_failed": 0,
            "trace_count": 3,
        }
        base.update(overrides)
        return base

    def test_header_shows_task_id_and_title(self):
        result = _strip_ansi(render_task_detail(self._make_task(), [], self._make_summary()))
        assert "Task #1" in result
        assert "Fix auth module" in result

    def test_shows_status(self):
        result = _strip_ansi(render_task_detail(self._make_task(), [], self._make_summary()))
        assert "running" in result

    def test_shows_duration_and_cost(self):
        result = _strip_ansi(render_task_detail(self._make_task(), [], self._make_summary()))
        assert "3m 12s" in result
        assert "$0.04" in result

    def test_shows_tokens(self):
        result = _strip_ansi(render_task_detail(self._make_task(), [], self._make_summary()))
        assert "12.4k in" in result
        assert "3.2k out" in result

    def test_blocked_shows_reason(self):
        task = self._make_task(status="blocked", blocked_reason="Waiting for input")
        result = _strip_ansi(render_task_detail(task, [], self._make_summary()))
        assert "blocked" in result
        assert "Waiting for input" in result

    def test_no_traces_omits_section(self):
        result = _strip_ansi(render_task_detail(self._make_task(), [], self._make_summary()))
        assert "Traces:" not in result

    def test_with_traces(self):
        traces = [
            {"role": "scope", "model": "sonnet", "tokens_in": 1200, "tokens_out": 500,
             "cost_usd": 0.01, "elapsed_s": 14, "success": True},
            {"role": "plan", "model": "opus", "tokens_in": 4500, "tokens_out": 1200,
             "cost_usd": 0.05, "elapsed_s": 45, "success": True},
        ]
        result = _strip_ansi(render_task_detail(self._make_task(), traces, self._make_summary()))
        assert "Traces:" in result
        assert "scope" in result
        assert "plan" in result
        assert "sonnet" in result
        assert "opus" in result


class TestRenderTraces:
    def test_empty_traces(self):
        result = render_traces([])
        assert "No traces" in result

    def test_renders_table(self):
        traces = [
            {"role": "execute", "model": "opus", "tokens_in": 100, "tokens_out": 200,
             "cost_usd": 0.01, "elapsed_s": 5.0, "success": True},
        ]
        result = _strip_ansi(render_traces(traces))
        assert "ROLE" in result
        assert "MODEL" in result
        assert "execute" in result
        assert "opus" in result


class TestRenderSummary:
    def test_renders_card(self):
        summary = {
            "task_id": 1,
            "total_cost_usd": 0.07,
            "total_elapsed_s": 13,
            "total_tokens_in": 700,
            "total_tokens_out": 1400,
            "steps_succeeded": 2,
            "steps_failed": 0,
            "trace_count": 2,
        }
        result = _strip_ansi(render_summary(summary))
        assert "Summary" in result
        assert "Task #1" in result
        assert "Traces:" in result or "2" in result
        assert "$0.07" in result
        assert "13s" in result
        assert "700" in result
        assert "1.4k" in result

    def test_empty_summary(self):
        summary = {
            "task_id": 42,
            "total_cost_usd": 0,
            "total_elapsed_s": 0,
            "total_tokens_in": 0,
            "total_tokens_out": 0,
            "steps_succeeded": 0,
            "steps_failed": 0,
            "trace_count": 0,
        }
        result = _strip_ansi(render_summary(summary))
        assert "Task #42" in result
        assert "--" in result
