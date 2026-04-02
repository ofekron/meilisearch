#!/usr/bin/env python3
"""
FastCI Trace Visualizer
Generates a terminal-based Gantt chart and timeline from JSONL trace data.

Usage:
    python3 visualize_trace.py <trace.jsonl>
    python3 visualize_trace.py <trace.jsonl> --html > timeline.html
"""

import json
import sys
import argparse
from collections import defaultdict
from datetime import datetime, timezone

# Reuse parsing from analyze_trace
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
from analyze_trace import load_spans, parse_time, duration_secs, build_tree, get_attr


COLORS = [
    "\033[94m", "\033[92m", "\033[93m", "\033[91m", "\033[95m",
    "\033[96m", "\033[97m", "\033[34m", "\033[32m", "\033[33m",
]
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def terminal_gantt(spans, width=80):
    """Print a terminal Gantt chart of the trace."""
    timed = []
    for span in spans:
        try:
            start = parse_time(span.get("start_time") or span.get("startTime") or span.get("start_time_unix_nano") or span.get("startTimeUnixNano"))
            end = parse_time(span.get("end_time") or span.get("endTime") or span.get("end_time_unix_nano") or span.get("endTimeUnixNano"))
            dur = (end - start).total_seconds()
            if dur > 0.1:  # Skip sub-100ms spans
                timed.append((span.get("name", "?"), start, end, dur, span))
        except (ValueError, KeyError, TypeError):
            continue

    if not timed:
        print("No timed spans found.")
        return

    timed.sort(key=lambda x: x[1])

    global_start = min(t[1] for t in timed)
    global_end = max(t[2] for t in timed)
    total_secs = (global_end - global_start).total_seconds()

    if total_secs == 0:
        print("All spans have zero duration.")
        return

    bar_width = width - 45  # Reserve space for labels and duration

    print(f"\n{BOLD}=== FastCI Trace Timeline ==={RESET}")
    print(f"Total wall time: {total_secs:.1f}s\n")

    # Group by trace_id or parent
    trace_ids = set(span.get("trace_id") or span.get("traceId") or "default" for _, _, _, _, span in timed)

    for i, (name, start, end, dur, span) in enumerate(timed):
        offset = (start - global_start).total_seconds()
        bar_start = int((offset / total_secs) * bar_width)
        bar_len = max(1, int((dur / total_secs) * bar_width))
        color = COLORS[i % len(COLORS)]

        # Truncate name to fit
        display_name = name[:30].ljust(30)
        dur_str = f"{dur:6.1f}s"

        bar = " " * bar_start + color + "█" * bar_len + RESET
        print(f"  {display_name} {dur_str} |{bar}")

    print(f"\n  {'─' * (bar_width + 38)}")
    # Time scale
    marks = 5
    scale = "  " + " " * 30 + "       |"
    for i in range(marks + 1):
        pos = int((i / marks) * bar_width)
        t = (i / marks) * total_secs
        scale += f"{t:>{bar_width // marks}.0f}s" if i > 0 else "0s"
    print(scale[:width + 20])
    print()


def generate_html_timeline(spans):
    """Generate an HTML timeline visualization."""
    timed = []
    for span in spans:
        try:
            start = parse_time(span.get("start_time") or span.get("startTime") or span.get("start_time_unix_nano") or span.get("startTimeUnixNano"))
            end = parse_time(span.get("end_time") or span.get("endTime") or span.get("end_time_unix_nano") or span.get("endTimeUnixNano"))
            dur = (end - start).total_seconds()
            if dur > 0.1:
                pid = span.get("parent_span_id") or span.get("parentSpanId") or ""
                timed.append({
                    "name": span.get("name", "?"),
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "duration": round(dur, 2),
                    "span_id": span.get("span_id") or span.get("spanId") or "",
                    "parent_id": pid,
                    "is_root": not pid or pid == "0" * len(pid),
                    "status": (span.get("status", {}) or {}).get("code", "unknown"),
                    "attributes": {k: v for k, v in (span.get("attributes", {}) if isinstance(span.get("attributes", {}), dict) else {}).items()},
                })
        except (ValueError, KeyError, TypeError):
            continue

    timed.sort(key=lambda x: x["start"])

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>FastCI Trace Timeline</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }}
h1 {{ color: #58a6ff; margin-bottom: 10px; }}
.summary {{ color: #8b949e; margin-bottom: 20px; }}
.timeline {{ position: relative; overflow-x: auto; }}
.row {{ display: flex; align-items: center; margin: 2px 0; height: 28px; }}
.label {{ width: 280px; min-width: 280px; font-size: 12px; text-align: right; padding-right: 10px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.track {{ flex: 1; position: relative; height: 22px; }}
.bar {{ position: absolute; height: 100%; border-radius: 3px; min-width: 2px; cursor: pointer; transition: opacity 0.2s; }}
.bar:hover {{ opacity: 0.8; filter: brightness(1.3); }}
.bar .tooltip {{ display: none; position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%); background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 8px; font-size: 11px; white-space: nowrap; z-index: 10; }}
.bar:hover .tooltip {{ display: block; }}
.dur {{ font-size: 11px; color: #8b949e; margin-left: 4px; min-width: 50px; }}
.root .bar {{ opacity: 1; }}
.child .bar {{ opacity: 0.7; }}
.colors {{ --c0: #58a6ff; --c1: #3fb950; --c2: #d29922; --c3: #f85149; --c4: #bc8cff; --c5: #39d2c0; --c6: #db61a2; --c7: #79c0ff; }}
</style>
</head>
<body>
<h1>FastCI Trace Timeline</h1>
<div class="summary">
    <span>Spans: {len(timed)}</span> |
    <span>Total: {timed[-1]['duration'] if timed else 0}s</span>
</div>
<div class="timeline colors">
"""

    if timed:
        global_start = min(t["start"] for t in timed)
        global_end = max(t["end"] for t in timed)
        gs = datetime.fromisoformat(global_start)
        ge = datetime.fromisoformat(global_end)
        total = (ge - gs).total_seconds() or 1

        colors = ["var(--c0)", "var(--c1)", "var(--c2)", "var(--c3)", "var(--c4)", "var(--c5)", "var(--c6)", "var(--c7)"]

        for i, t in enumerate(timed):
            s = datetime.fromisoformat(t["start"])
            left_pct = ((s - gs).total_seconds() / total) * 100
            width_pct = max(0.3, (t["duration"] / total) * 100)
            color = colors[i % len(colors)]
            cls = "root" if t["is_root"] else "child"

            html += f"""  <div class="row {cls}">
    <div class="label" title="{t['name']}">{t['name']}</div>
    <div class="track">
      <div class="bar" style="left:{left_pct:.2f}%;width:{width_pct:.2f}%;background:{color};">
        <div class="tooltip">{t['name']}<br>{t['duration']}s<br>Status: {t['status']}</div>
      </div>
    </div>
    <div class="dur">{t['duration']}s</div>
  </div>
"""

    html += """</div>
</body>
</html>"""
    return html


def main():
    parser = argparse.ArgumentParser(description="FastCI Trace Visualizer")
    parser.add_argument("trace_file", help="Path to JSONL trace file")
    parser.add_argument("--html", action="store_true", help="Output HTML timeline")
    parser.add_argument("--width", type=int, default=120, help="Terminal width for Gantt chart")
    args = parser.parse_args()

    spans = load_spans(args.trace_file)

    if not spans:
        print("No spans found.", file=sys.stderr)
        sys.exit(1)

    if args.html:
        print(generate_html_timeline(spans))
    else:
        terminal_gantt(spans, width=args.width)


if __name__ == "__main__":
    main()
