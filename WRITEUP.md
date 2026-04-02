# FastCI Engineering Challenge - Write-up

## Workflow Run Links

### opencode (anomalyco/opencode fork)
- **Before optimization**: https://github.com/ofekron/opencode/actions/runs/23909911105
- **After optimization**: https://github.com/ofekron/opencode/actions/runs/23910798915

### meilisearch (meilisearch/meilisearch fork)
- **Before optimization**: https://github.com/ofekron/meilisearch/actions/runs/23909911992
- **After optimization**: https://github.com/ofekron/meilisearch/actions/runs/23910799953

---

## Part 2: What I Did and Expected Impact

### Approach

I instrumented two repos (anomalyco/opencode and meilisearch/meilisearch) with FastCI and built a comprehensive CI optimization skill composed of three layers:

1. **Trace Analysis Engine** (`tools/analyze_trace.py`): Parses the JSONL trace output, builds span trees, detects bottlenecks, identifies cache opportunities, and generates prioritized recommendations. It handles multiple timestamp formats (unix nano, ISO 8601, epoch ms) to work with any FastCI output.

2. **Trace Visualizer** (`tools/visualize_trace.py`): Generates both terminal Gantt charts and interactive HTML timelines from trace data. This makes it easy to visually identify where time is spent and which steps overlap or run sequentially.

3. **Optimization Skill** (`SKILL.md`): A structured, reusable playbook that any AI coding agent can follow. It walks through: parsing traces → identifying bottlenecks → classifying optimization types → generating concrete workflow diffs → validating changes don't break the build.

### Key Design Decisions

- **Generic by design**: The skill doesn't hardcode any repo-specific knowledge. It detects the tech stack from span names and attributes, then applies stack-specific optimization patterns. This makes it work across Node.js, Rust, Python, Docker, and other ecosystems.

- **Enriched traces**: The analyzer adds computed attributes (`fastci.duration_secs`, `fastci.is_bottleneck`, `fastci.cacheable`, `fastci.pct_of_total`) back to the JSONL. This creates a feedback loop where subsequent skill runs have richer data to work with.

- **Incremental validation**: The skill explicitly requires before/after run links and automated validation that no passing steps regressed. This prevents the common failure mode of "optimized CI but broke the build."

### Expected Impact

For the **opencode** repo:
- **Bun dependency caching**: The `setup-bun` action caches via `actions/cache` keyed on `bun.lock`. On cache hits, install time should drop from ~20-30s to ~3-5s.
- **Playwright browser caching**: Already partially implemented, but the cache key could be tightened. Expected improvement: avoid re-downloading ~200MB of browser binaries on cache hits.
- **Test sharding**: The e2e tests run on a single machine per platform. Sharding across 2-3 machines could cut e2e wall time by 50-60%.

For the **meilisearch** repo:
- **Docker layer caching**: Adding `cache-from: type=gha` to the buildx step would cache Rust compilation layers. Given Rust's notoriously slow compilation, this could save 5-10 minutes per build after the first run.
- **Matrix reduction for PRs**: The full matrix (2 platforms x 2 editions = 4 builds) isn't needed for every PR. Running just amd64/community for PRs and full matrix only for releases would save 50% of CI minutes on average.

---

## Part 2.8: AutoResearch - Self-Improving Skills via Agentic Loops

### Agentic Loop Design

The core loop follows a **hypothesis → experiment → evaluate → refine** cycle, inspired by the AutoResearch methodology:

```
┌─────────────────────────────────────────────────┐
│                 SKILL REGISTRY                   │
│  (versioned SKILL.md files with performance      │
│   scores and lineage metadata)                   │
└──────────┬──────────────────────────┬───────────┘
           │                          │
     ┌─────▼─────┐            ┌──────▼──────┐
     │  ANALYZE   │            │  EVALUATE   │
     │ (run skill │◄───────────│ (score the  │
     │  on traces)│            │  result)    │
     └─────┬──────┘            └──────▲──────┘
           │                          │
     ┌─────▼─────┐            ┌──────┴──────┐
     │  PROPOSE   │            │   APPLY     │
     │ (generate  │───────────►│ (implement  │
     │  changes)  │            │  changes)   │
     └────────────┘            └─────────────┘
```

**Loop steps:**

1. **Analyze**: Run the current SKILL.md against a corpus of trace files from different repos/stacks. Record which recommendations it produces.

2. **Propose**: The agent generates a hypothesis for improving the skill. Examples:
   - "Adding detection for Gradle build scans would cover Java projects"
   - "The cache key recommendation for pnpm uses the wrong path"
   - "Sharding threshold should be 45s not 60s based on observed data"

3. **Apply**: Generate a new version of SKILL.md with the proposed change. Keep the diff minimal and focused.

4. **Evaluate**: Run the updated skill against the same corpus PLUS a held-out validation set. Score it against the evals (see below).

5. **Accept/Reject**: If the new version scores equal or higher on all evals and strictly higher on at least one, accept it. Otherwise, reject and try a different hypothesis. This is essentially a Pareto improvement check.

### Evals

| Eval | What it measures | How to score |
|------|-----------------|--------------|
| **Coverage** | Does the skill detect known bottlenecks in the test corpus? | % of pre-labeled bottlenecks correctly identified |
| **Precision** | Are the recommendations correct and applicable? | % of recommendations that would actually work if applied |
| **Safety** | Do the optimized workflows still pass? | 0 if any previously-passing step fails; 1 otherwise |
| **Impact** | How much time do the recommendations save? | Σ(estimated savings) / Σ(total pipeline time) |
| **Generality** | Does it work across different stacks? | Score across N diverse repos (Node, Rust, Python, Docker, Go) |
| **Actionability** | Are the outputs concrete diffs vs. vague advice? | % of recommendations with a specific code change attached |

### Unit Tests

```python
# Test: Correctly identifies uncached npm install as optimization target
def test_detect_uncached_npm_install():
    trace = [{"name": "Install dependencies", "start_time": 0, "end_time": 30e9,
              "attributes": {}, "span_id": "1", "parent_span_id": ""}]
    result = run_skill(trace)
    assert any("cache" in r["suggestion"].lower() for r in result["recommendations"])

# Test: Does NOT recommend caching for steps that already cache
def test_skip_already_cached():
    trace = [{"name": "Install dependencies", "start_time": 0, "end_time": 2e9,
              "attributes": {"cache.hit": True}, "span_id": "1", "parent_span_id": ""}]
    result = run_skill(trace)
    assert not any("add cache" in r["suggestion"].lower() for r in result["recommendations"])

# Test: Detects Docker build without layer caching
def test_docker_no_cache():
    trace = [{"name": "Build and push", "start_time": 0, "end_time": 300e9,
              "attributes": {"step.uses": "docker/build-push-action@v6"},
              "span_id": "1", "parent_span_id": ""}]
    result = run_skill(trace)
    assert any("cache-from" in str(r) for r in result["recommendations"])

# Test: Recommends test sharding for long test suites
def test_shard_long_tests():
    trace = [{"name": "Run tests", "start_time": 0, "end_time": 120e9,
              "attributes": {}, "span_id": "1", "parent_span_id": ""}]
    result = run_skill(trace)
    assert any("shard" in str(r).lower() for r in result["recommendations"])

# Test: Safety - never removes a required step
def test_safety_no_removal():
    workflow = "steps:\n  - uses: actions/checkout@v4\n  - run: npm test"
    trace = [...]  # Any trace
    optimized = run_skill_on_workflow(trace, workflow)
    assert "actions/checkout" in optimized

# Test: Handles empty trace gracefully
def test_empty_trace():
    result = run_skill([])
    assert result["summary"]["total_spans"] == 0
    assert result["recommendations"] == []

# Test: Handles various timestamp formats
def test_timestamp_formats():
    for ts_format in [1712000000000000000, "2024-04-02T00:00:00Z", 1712000000000]:
        trace = [{"name": "test", "start_time": ts_format, "end_time": ts_format,
                  "span_id": "1", "parent_span_id": ""}]
        result = run_skill(trace)  # Should not crash
```

### Ensuring Positive Progress Without Manual Supervision

**1. Monotonic improvement gate**: The system NEVER deploys a skill version that scores lower than the previous version on any eval dimension. This is the fundamental safety mechanism. It means the worst case is "no change" rather than regression.

**2. Diverse test corpus**: The eval corpus must cover at least 5 different tech stacks and 10 different workflow patterns. This prevents overfitting to one repo's traces.

**3. Canary deployments**: Before marking a new skill version as "current," run it on 3 randomly selected real-world repos and verify the output is sane (passes safety eval). If any canary fails, roll back.

**4. Lineage tracking**: Every skill version records its parent version, the hypothesis that generated it, and the eval scores. This creates an audit trail and enables the agent to learn from failed hypotheses (avoid re-trying similar changes).

**5. Diminishing returns detection**: If the last N iterations produced no accepted improvement, the agent should:
   - Expand the test corpus with new, diverse repos
   - Shift focus to a different optimization category
   - Generate a "research report" summarizing what was tried and why it failed

**6. Human-in-the-loop escape hatch**: While the loop runs autonomously, it publishes a weekly summary: skill version history, eval score trends, and top hypotheses. A human can inject new hypotheses or veto changes if the direction seems wrong. The key insight is that the human doesn't need to review every iteration - only the trend.

---

## Part 3: Think Like a Founding Engineer

### Features/Capabilities I'd Add to FastCI

**1. Cross-Run Regression Detection ("CI Performance SLOs")**
Teams don't just want "make CI fast once" - they want "keep CI fast forever." FastCI should track pipeline duration over time and alert when a step regresses by >20% compared to the rolling 30-day baseline. This is like Datadog for CI performance. Engineering managers would pay for this because it prevents the slow boil where CI goes from 5 minutes to 25 minutes over a year without anyone noticing.

**2. Cost Attribution Dashboard**
Map trace durations to actual GitHub Actions billing (minutes × runner cost). Show teams "this workflow costs $X/month, and 60% of that cost is Docker builds." This makes the ROI of optimization tangible. DevOps teams and engineering managers would love this for budget justification.

**3. Smart Test Selection (Test Impact Analysis)**
Use file-change diffs from the PR + a dependency graph (built from traces over time) to determine which tests actually need to run. If I changed `auth.ts`, I don't need to run `payment_test.py`. This is the killer feature that Google/Meta have internally but the industry doesn't have as a product.

**4. PR-Level Optimization Suggestions**
Instead of just producing traces, FastCI could comment on PRs: "This PR adds a new dependency install step. Based on similar repos, adding `actions/cache` here would save ~30s per run." Proactive, contextual, developer-friendly.

**5. Flaky Test Detection and Quarantine**
Trace data across runs can identify tests that sometimes pass and sometimes fail. Auto-quarantine flaky tests (run them but don't fail the build) and report them separately. This is a massive developer productivity win.

### What's Missing from the Trace Data

**1. Resource utilization metrics**: CPU, memory, disk I/O, and network bandwidth per step. A step that takes 60s at 5% CPU is waiting on network, while 60s at 100% CPU is compute-bound. The optimization strategy is completely different for each. Without this, we're guessing.

**2. Cache effectiveness details**: Not just "hit/miss" but cache size, restore time, upload time, key used, and whether the cache was actually fresh enough. A cache that takes 30s to restore a 2GB artifact might not be worth it for a 40s install step.

**3. Dependency graph between steps**: Which steps actually depend on which outputs? Currently we can only infer this from temporal ordering, but explicit data dependencies would enable much smarter parallelization suggestions.

**4. Historical baseline data**: A single trace is a snapshot. To detect regressions, identify trends, and measure improvement, we need the trace data linked to commit SHAs, branch names, and timestamps. This enables "this step got 40% slower after commit abc123."

**5. Environment/runner metadata**: Runner type, available CPU cores, memory, OS version, pre-installed tools. Two identical workflows can behave very differently on `ubuntu-latest` vs a self-hosted runner. Optimization recommendations should account for the runner's capabilities.

### One-Week High-Impact Addition: CI Performance Regression Alerts

If I had one week, I'd build **automatic CI performance regression detection with PR comments**.

**Why this one:** It's the feature with the highest ongoing value and stickiest retention. Optimization is a one-time event, but regression detection is a continuous service. Once teams have it, they can't go back to not having it - the same way APM tools (Datadog, New Relic) became indispensable once teams saw production performance dashboards.

**Implementation sketch:**
- **Day 1-2**: Build a trace ingestion service that stores span durations keyed by (repo, workflow, job, step, branch). Use a simple SQLite/DuckDB backend initially - it's plenty fast for per-commit granularity.
- **Day 3-4**: Implement a rolling-window anomaly detector. For each step, maintain a 30-run rolling average and standard deviation. Flag any step that exceeds `mean + 2*stdev` as a regression. Use the span hierarchy to attribute regressions to specific steps, not just "the whole pipeline got slower."
- **Day 5**: Build a GitHub App that posts PR comments when a regression is detected: "This PR increased the 'Run tests' step from 45s (30-run avg) to 78s (+73%). The regression appears to be in the `cargo build` phase." Include a link to the trace visualization.
- **Day 6-7**: Add a dashboard showing CI performance trends over time, top 5 slowest-growing steps, and estimated monthly cost impact. Polish, test on 3-5 real repos, write docs.

This creates a natural upsell path: "You used FastCI to optimize your CI. Now keep it optimized automatically." And it generates recurring value that justifies a monthly subscription.
