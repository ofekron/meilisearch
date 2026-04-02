#!/usr/bin/env python3
"""
FastCI Trace Analyzer
Parses JSONL trace data from FastCI and produces actionable CI optimization insights.

Usage:
    python3 analyze_trace.py <trace.jsonl> [--format json|text|markdown]
    python3 analyze_trace.py <trace.jsonl> --enrich > enriched_trace.jsonl
"""

import json
import sys
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def parse_time(ts):
    """Parse various timestamp formats into datetime."""
    if isinstance(ts, (int, float)):
        # Nanosecond epoch
        if ts > 1e15:
            return datetime.fromtimestamp(ts / 1e9, tz=timezone.utc)
        # Microsecond epoch
        elif ts > 1e12:
            return datetime.fromtimestamp(ts / 1e6, tz=timezone.utc)
        # Millisecond epoch
        elif ts > 1e9:
            return datetime.fromtimestamp(ts / 1e3, tz=timezone.utc)
        # Second epoch
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(ts, str):
        # Try ISO format
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            pass
        # Try integer string
        try:
            return parse_time(int(ts))
        except ValueError:
            pass
    raise ValueError(f"Cannot parse timestamp: {ts}")


def duration_secs(span):
    """Calculate span duration in seconds.
    Prefers step.execution_time_seconds attribute if available (FastCI native),
    falls back to start_time/end_time calculation.
    """
    # FastCI provides execution time as an attribute
    attrs = span.get("attributes", {})
    if isinstance(attrs, dict):
        exec_time = attrs.get("step.execution_time_seconds")
        if exec_time is not None:
            return float(exec_time)
    elif isinstance(attrs, list):
        for attr in attrs:
            if attr.get("key") == "step.execution_time_seconds":
                val = attr.get("value", {})
                if isinstance(val, dict):
                    return float(val.get("intValue") or val.get("stringValue") or 0)
                return float(val)

    start = parse_time(span.get("start_time") or span.get("startTime") or span.get("start_time_unix_nano") or span.get("startTimeUnixNano"))
    end = parse_time(span.get("end_time") or span.get("endTime") or span.get("end_time_unix_nano") or span.get("endTimeUnixNano"))
    return (end - start).total_seconds()


def load_spans(path):
    """Load spans from JSONL file."""
    spans = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                span = json.loads(line)
                spans.append(span)
            except json.JSONDecodeError:
                continue
    return spans


def build_tree(spans):
    """Build parent-child tree from spans."""
    by_id = {}
    children = defaultdict(list)
    roots = []

    for span in spans:
        sid = span.get("span_id") or span.get("spanId") or span.get("span_id")
        pid = span.get("parent_span_id") or span.get("parentSpanId")
        by_id[sid] = span
        if pid and pid != "" and pid != "0" * len(pid):
            children[pid].append(sid)
        else:
            roots.append(sid)

    return by_id, children, roots


def get_attr(span, key):
    """Get attribute from span, handling various attribute formats."""
    attrs = span.get("attributes", {})
    if isinstance(attrs, dict):
        return attrs.get(key)
    if isinstance(attrs, list):
        for attr in attrs:
            if attr.get("key") == key:
                val = attr.get("value", {})
                if isinstance(val, dict):
                    return val.get("stringValue") or val.get("intValue") or val.get("boolValue")
                return val
    return None


def analyze_bottlenecks(spans):
    """Find the longest-running spans (bottlenecks)."""
    timed = []
    for span in spans:
        try:
            dur = duration_secs(span)
            name = span.get("name", "unnamed")
            timed.append((name, dur, span))
        except (ValueError, KeyError, TypeError):
            continue

    timed.sort(key=lambda x: x[1], reverse=True)
    return timed


def detect_cache_opportunities(spans):
    """Detect steps that could benefit from caching."""
    cache_keywords = ["install", "download", "fetch", "setup", "restore", "build", "compile", "pip", "npm", "bun", "cargo", "docker"]
    opportunities = []

    for span in spans:
        name = (span.get("name") or "").lower()
        has_cache_hit = get_attr(span, "cache.hit") or get_attr(span, "cache-hit")

        if any(kw in name for kw in cache_keywords):
            try:
                dur = duration_secs(span)
            except (ValueError, KeyError, TypeError):
                dur = 0

            if dur > 5:  # Only flag if > 5 seconds
                opportunities.append({
                    "name": span.get("name"),
                    "duration_secs": round(dur, 2),
                    "cache_hit": has_cache_hit,
                    "suggestion": "Consider caching this step's output" if not has_cache_hit else "Cache hit detected - good!",
                })

    return opportunities


def detect_parallelization(spans):
    """Find sequential steps that could potentially run in parallel."""
    by_id, children, roots = build_tree(spans)
    sequential_groups = []

    for root_id in roots:
        child_spans = []
        for cid in children.get(root_id, []):
            child = by_id.get(cid)
            if child:
                try:
                    dur = duration_secs(child)
                    child_spans.append((child.get("name"), dur, child))
                except (ValueError, KeyError, TypeError):
                    continue

        # Look for sequences of independent steps
        if len(child_spans) > 1:
            sequential_groups.append({
                "parent": by_id.get(root_id, {}).get("name", root_id),
                "children": [(name, round(dur, 2)) for name, dur, _ in child_spans],
                "total_sequential_time": round(sum(d for _, d, _ in child_spans), 2),
                "max_parallel_time": round(max(d for _, d, _ in child_spans), 2) if child_spans else 0,
            })

    return sequential_groups


def detect_docker_optimizations(spans):
    """Find Docker-specific optimization opportunities."""
    docker_spans = []
    for span in spans:
        name = (span.get("name") or "").lower()
        if "docker" in name or "build" in name or "layer" in name or "buildx" in name:
            try:
                dur = duration_secs(span)
                docker_spans.append({
                    "name": span.get("name"),
                    "duration_secs": round(dur, 2),
                })
            except (ValueError, KeyError, TypeError):
                continue
    return docker_spans


def enrich_trace(spans):
    """Add computed attributes to each span for richer analysis."""
    bottlenecks = analyze_bottlenecks(spans)
    total_time = sum(d for _, d, _ in bottlenecks) if bottlenecks else 1

    # Get max duration for percentile ranking
    max_dur = bottlenecks[0][1] if bottlenecks else 1

    enriched = []
    for span in spans:
        enriched_span = dict(span)
        attrs = enriched_span.setdefault("attributes", {})
        if isinstance(attrs, list):
            attrs_dict = {a.get("key"): a.get("value") for a in attrs}
        else:
            attrs_dict = dict(attrs)

        try:
            dur = duration_secs(span)
            attrs_dict["fastci.duration_secs"] = round(dur, 3)
            attrs_dict["fastci.pct_of_total"] = round((dur / total_time) * 100, 2) if total_time else 0
            attrs_dict["fastci.is_bottleneck"] = dur > (max_dur * 0.3)
            attrs_dict["fastci.duration_bucket"] = (
                "fast" if dur < 5
                else "medium" if dur < 30
                else "slow" if dur < 120
                else "very_slow"
            )
        except (ValueError, KeyError, TypeError):
            pass

        # Cache detection
        name = (span.get("name") or "").lower()
        cache_keywords = ["install", "download", "fetch", "setup", "restore", "build", "compile"]
        attrs_dict["fastci.cacheable"] = any(kw in name for kw in cache_keywords)
        attrs_dict["fastci.cache_hit"] = bool(get_attr(span, "cache.hit") or get_attr(span, "cache-hit"))

        # Dependency detection
        if "install" in name or "setup" in name:
            attrs_dict["fastci.is_dependency_step"] = True
            attrs_dict["fastci.dependency_type"] = next(
                (pkg for pkg in ["npm", "bun", "pip", "cargo", "docker", "brew", "apt"]
                 if pkg in name), "unknown"
            )

        if isinstance(span.get("attributes"), list):
            enriched_span["attributes"] = [
                {"key": k, "value": {"stringValue": str(v)} if isinstance(v, str) else {"intValue": v} if isinstance(v, int) else {"boolValue": v} if isinstance(v, bool) else {"stringValue": str(v)}}
                for k, v in attrs_dict.items()
            ]
        else:
            enriched_span["attributes"] = attrs_dict

        enriched.append(enriched_span)

    return enriched


def generate_report(spans, format="markdown"):
    """Generate a full optimization report."""
    bottlenecks = analyze_bottlenecks(spans)
    cache_opps = detect_cache_opportunities(spans)
    parallel_opps = detect_parallelization(spans)
    docker_opps = detect_docker_optimizations(spans)

    total_duration = sum(d for _, d, _ in bottlenecks)

    report = {
        "summary": {
            "total_spans": len(spans),
            "total_duration_secs": round(total_duration, 2),
            "total_duration_human": f"{int(total_duration // 60)}m {int(total_duration % 60)}s",
        },
        "bottlenecks": [
            {"name": name, "duration_secs": round(dur, 2), "pct_of_total": round((dur / total_duration) * 100, 1) if total_duration else 0}
            for name, dur, _ in bottlenecks[:10]
        ],
        "cache_opportunities": cache_opps,
        "parallelization_opportunities": parallel_opps,
        "docker_optimizations": docker_opps,
        "recommendations": [],
    }

    # Generate recommendations
    recs = report["recommendations"]

    # Top bottleneck recommendations
    for name, dur, span in bottlenecks[:3]:
        if dur > 30:
            recs.append({
                "priority": "high",
                "target": name,
                "issue": f"Takes {dur:.0f}s ({(dur/total_duration)*100:.0f}% of total)",
                "suggestions": _suggest_for_step(name, dur, span),
            })

    # Cache recommendations
    for opp in cache_opps:
        if not opp["cache_hit"]:
            recs.append({
                "priority": "medium",
                "target": opp["name"],
                "issue": f"No caching detected, takes {opp['duration_secs']}s",
                "suggestions": ["Add actions/cache for this step's output", "Consider using setup-* actions with built-in caching"],
            })

    if format == "json":
        return json.dumps(report, indent=2)
    elif format == "markdown":
        return _report_to_markdown(report)
    else:
        return _report_to_text(report)


def _suggest_for_step(name, dur, span):
    """Generate specific suggestions based on step name."""
    name_lower = name.lower()
    suggestions = []

    if "docker" in name_lower or "build" in name_lower:
        suggestions.extend([
            "Enable Docker layer caching with cache-from/cache-to in buildx",
            "Use multi-stage builds to reduce final image size",
            "Order Dockerfile instructions from least to most frequently changed",
            "Consider using --mount=type=cache for package manager caches",
        ])
    elif "install" in name_lower or "setup" in name_lower:
        suggestions.extend([
            "Cache dependency directories (node_modules, .cache, target/)",
            "Use lockfile hashes as cache keys for precise invalidation",
            "Consider frozen installs (--frozen-lockfile, --ci) for reproducibility",
        ])
    elif "test" in name_lower:
        suggestions.extend([
            "Shard tests across multiple parallel jobs",
            "Use test impact analysis to only run affected tests",
            "Cache test fixtures and compiled test assets",
        ])
    elif "playwright" in name_lower or "e2e" in name_lower:
        suggestions.extend([
            "Cache Playwright browser binaries",
            "Shard e2e tests across parallel jobs",
            "Use headed mode only when debugging, headless for CI",
        ])
    elif "compile" in name_lower or "cargo" in name_lower or "rustc" in name_lower:
        suggestions.extend([
            "Use sccache or buildcache for compilation caching",
            "Enable incremental compilation",
            "Use cargo-nextest for faster test execution",
        ])

    if not suggestions:
        suggestions.append("Profile this step to understand what's taking time")

    return suggestions


def _report_to_markdown(report):
    """Convert report dict to markdown."""
    lines = []
    s = report["summary"]
    lines.append(f"# FastCI Trace Analysis Report\n")
    lines.append(f"**Total spans:** {s['total_spans']} | **Total duration:** {s['total_duration_human']} ({s['total_duration_secs']}s)\n")

    lines.append(f"\n## Top Bottlenecks\n")
    lines.append("| # | Step | Duration | % of Total |")
    lines.append("|---|------|----------|------------|")
    for i, b in enumerate(report["bottlenecks"], 1):
        lines.append(f"| {i} | {b['name']} | {b['duration_secs']}s | {b['pct_of_total']}% |")

    if report["cache_opportunities"]:
        lines.append(f"\n## Cache Opportunities\n")
        for opp in report["cache_opportunities"]:
            status = "HIT" if opp["cache_hit"] else "MISS/NONE"
            lines.append(f"- **{opp['name']}** ({opp['duration_secs']}s) - Cache: {status} - {opp['suggestion']}")

    if report["docker_optimizations"]:
        lines.append(f"\n## Docker Build Steps\n")
        for d in report["docker_optimizations"]:
            lines.append(f"- **{d['name']}** - {d['duration_secs']}s")

    if report["recommendations"]:
        lines.append(f"\n## Recommendations\n")
        for i, rec in enumerate(report["recommendations"], 1):
            lines.append(f"### {i}. [{rec['priority'].upper()}] {rec['target']}\n")
            lines.append(f"**Issue:** {rec['issue']}\n")
            lines.append("**Suggestions:**")
            for sug in rec["suggestions"]:
                lines.append(f"- {sug}")
            lines.append("")

    return "\n".join(lines)


def _report_to_text(report):
    """Convert report dict to plain text."""
    lines = []
    s = report["summary"]
    lines.append(f"=== FastCI Trace Analysis ===")
    lines.append(f"Spans: {s['total_spans']} | Duration: {s['total_duration_human']}")
    lines.append("")

    lines.append("--- Bottlenecks ---")
    for i, b in enumerate(report["bottlenecks"], 1):
        lines.append(f"  {i}. {b['name']}: {b['duration_secs']}s ({b['pct_of_total']}%)")

    if report["recommendations"]:
        lines.append("\n--- Recommendations ---")
        for rec in report["recommendations"]:
            lines.append(f"  [{rec['priority'].upper()}] {rec['target']}: {rec['issue']}")
            for sug in rec["suggestions"]:
                lines.append(f"    -> {sug}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="FastCI Trace Analyzer")
    parser.add_argument("trace_file", help="Path to JSONL trace file")
    parser.add_argument("--format", choices=["json", "text", "markdown"], default="markdown", help="Output format")
    parser.add_argument("--enrich", action="store_true", help="Output enriched JSONL with computed attributes")
    args = parser.parse_args()

    spans = load_spans(args.trace_file)

    if not spans:
        print("Error: No spans found in trace file", file=sys.stderr)
        sys.exit(1)

    if args.enrich:
        enriched = enrich_trace(spans)
        for span in enriched:
            print(json.dumps(span))
    else:
        print(generate_report(spans, format=args.format))


if __name__ == "__main__":
    main()
