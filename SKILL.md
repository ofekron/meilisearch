# FastCI Trace Optimizer

## Description
Analyzes FastCI OpenTelemetry trace data from GitHub Actions workflows and produces concrete, actionable CI optimizations. Works across any repo and tech stack by reading the trace JSONL and the workflow YAML, identifying bottlenecks, and generating optimization PRs.

## Trigger
Run this skill when:
- A FastCI trace artifact is available from a completed GitHub Actions run
- The user wants to optimize CI pipeline performance
- The user wants to optimize local build/dev performance based on CI insights

## Inputs
- **Trace file**: A JSONL file produced by FastCI (one OpenTelemetry span per line)
- **Workflow file(s)**: The `.github/workflows/*.yml` file(s) that produced the trace
- **Repository context**: The repo's language, build system, and dependency manager

## Step 1: Load and Parse Trace Data

Read the JSONL trace file. Each line is a JSON span with these key fields:
- `trace_id`, `span_id`, `parent_span_id`: Span hierarchy
- `name`: Step or operation name
- `start_time`, `end_time`: Timestamps (may be unix nano, ISO 8601, or epoch ms)
- `attributes`: Key-value metadata (cache hits, step type, etc.)
- `status`: Execution status (OK, ERROR, etc.)

Parse all spans and build a parent-child tree using `span_id` / `parent_span_id`.

```
For each span:
  - Calculate duration = end_time - start_time
  - Identify root spans (no parent_span_id or parent_span_id is all zeros)
  - Map children to parents
```

## Step 2: Identify Bottlenecks

Sort all spans by duration descending. The top spans are your optimization targets.

**Classification rules:**
| Duration | Category | Priority |
|----------|----------|----------|
| > 120s   | Very slow | Critical |
| 30-120s  | Slow     | High     |
| 5-30s    | Medium   | Medium   |
| < 5s     | Fast     | Low - skip |

Focus optimization effort on spans that consume >10% of total pipeline wall time.

## Step 3: Analyze Optimization Opportunities

For each bottleneck span, apply these detection rules:

### 3a. Caching Opportunities
Check if the span name contains: `install`, `download`, `fetch`, `setup`, `restore`, `build`, `compile`, `pip`, `npm`, `bun`, `yarn`, `pnpm`, `cargo`, `docker`, `apt`, `brew`

If yes, check for `cache.hit` or `cache-hit` in attributes:
- **No cache attribute**: This step has no caching - add it
- **cache.hit = false**: Cache exists but missed - check cache key strategy
- **cache.hit = true**: Already cached - move on

**Optimization actions by package manager:**

| Manager | Cache path | Cache key |
|---------|-----------|-----------|
| npm/yarn/pnpm | `~/.npm`, `~/.yarn/cache`, `~/.pnpm-store` | `${{ runner.os }}-node-${{ hashFiles('**/package-lock.json') }}` |
| bun | Output of `bun pm cache` | `${{ runner.os }}-bun-${{ hashFiles('**/bun.lock') }}` |
| pip | `~/.cache/pip` | `${{ runner.os }}-pip-${{ hashFiles('**/requirements*.txt') }}` |
| cargo | `~/.cargo/registry`, `~/.cargo/git`, `target/` | `${{ runner.os }}-cargo-${{ hashFiles('**/Cargo.lock') }}` |
| docker | Use `cache-from`/`cache-to` with buildx | `type=gha` or `type=registry` |

### 3b. Parallelization Opportunities
Look for sequential steps within the same job that have no data dependency:
- Setup steps (checkout, install) must stay sequential
- Test suites can often be sharded: split by file, module, or test count
- Lint, typecheck, and test can run as separate parallel jobs
- Docker builds for multiple platforms can use matrix strategy

**Detection**: If a job has >2 test/lint/build steps running sequentially and total >60s, recommend splitting into parallel jobs.

### 3c. Docker Build Optimizations
For spans matching `docker`, `buildx`, `build-push`:
- **No layer caching**: Add `cache-from: type=gha` and `cache-to: type=gha,mode=max`
- **Large build time**: Suggest multi-stage builds, .dockerignore, ordering COPY after RUN for dependencies
- **Multiple platform builds**: Ensure matrix strategy is used, not sequential builds

### 3d. Test Optimization
For spans matching `test`, `e2e`, `playwright`, `cypress`, `jest`, `pytest`:
- **Long test suites (>60s)**: Recommend test sharding
  - Jest: `--shard=${{ matrix.shard }}`
  - Playwright: `--shard=${{ strategy.job-index + 1 }}/${{ strategy.job-total }}`
  - pytest: `pytest-split` or `pytest-xdist`
- **E2E browser tests**: Cache browser binaries, use `--project=chromium` only in CI
- **Flaky test detection**: Check for retry attributes or multiple runs of same test

### 3e. Dependency Installation
For spans matching `install`, `setup`, `restore`:
- Use frozen/locked installs: `npm ci`, `bun install --frozen-lockfile`, `pip install --require-hashes`
- Cache the global package cache, not `node_modules` directly
- Consider using setup actions with built-in caching (`setup-node` with `cache: npm`)

## Step 4: Generate Workflow Optimizations

Read the actual workflow YAML file. For each identified opportunity, generate a concrete diff:

```yaml
# Example: Adding cache to a Docker build step
- name: Build and push
  uses: docker/build-push-action@v6
  with:
    context: .
    push: true
+   cache-from: type=gha
+   cache-to: type=gha,mode=max
```

```yaml
# Example: Test sharding
jobs:
  test:
+   strategy:
+     matrix:
+       shard: [1, 2, 3, 4]
    steps:
      - name: Run tests
-       run: npm test
+       run: npm test -- --shard=${{ matrix.shard }}/4
```

```yaml
# Example: Parallel lint + test + typecheck
jobs:
- build-and-test:
+ lint:
    steps:
-     - run: npm run lint
-     - run: npm run typecheck
-     - run: npm test
+     - run: npm run lint
+ typecheck:
+   steps:
+     - run: npm run typecheck
+ test:
+   steps:
+     - run: npm test
```

## Step 5: Local Development Optimizations

Based on CI trace insights, also suggest local dev improvements:

1. **If CI installs dependencies every run**: Suggest local pre-commit hooks that verify lockfile freshness
2. **If CI runs full test suite**: Suggest `--watch` mode or affected-only testing locally
3. **If Docker builds are slow in CI**: Suggest `docker compose` with volume mounts for local dev instead of rebuilding
4. **If compilation is a bottleneck**: Suggest incremental compilation settings, sccache, or turbopack/swc for JS

Output a `.fastci/local-optimizations.md` with specific commands for the detected stack.

## Step 6: Validate Optimizations

Before applying any optimization:

1. **Verify the workflow runs successfully before changes** (baseline run link)
2. **Apply optimizations incrementally** - one PR per optimization category
3. **Run the workflow after changes** and verify:
   - All previously passing steps still pass
   - No new failures introduced
   - Duration improved (compare trace data before/after)
4. **Include both run links** in the PR description

**Automated validation approach:**
```bash
# Compare before/after traces
python3 tools/analyze_trace.py before.jsonl --format json > before_report.json
python3 tools/analyze_trace.py after.jsonl --format json > after_report.json

# Check: no regression in passing steps
# Check: total duration decreased or stayed the same
# Check: no new error statuses in spans
```

## Step 7: Output

Produce:
1. **Analysis report** (markdown) with bottlenecks, opportunities, and recommendations
2. **Enriched trace** (JSONL) with computed attributes (`fastci.duration_secs`, `fastci.is_bottleneck`, `fastci.cacheable`, etc.)
3. **Optimized workflow file(s)** with concrete changes
4. **PR description** explaining what changed and expected impact
5. **Local dev recommendations** based on CI insights

## Composability

This skill is designed to compose with other skills:
- **Trace collection skill**: Runs FastCI and produces the JSONL input for this skill
- **PR creation skill**: Takes this skill's output and creates a GitHub PR
- **Regression detection skill**: Compares before/after traces to verify improvements
- **Stack detection skill**: Identifies language/framework to tune recommendations

To discover this skill, an agent should look for `SKILL.md` files in the repo root or `.fastci/` directory.

## Examples

### Example 1: Node.js project with slow install
```
Input trace shows: "Install dependencies" span = 45s, no cache attributes
→ Output: Add actions/cache for node_modules with package-lock.json hash key
→ Expected impact: 45s → 5s on cache hit (89% reduction)
```

### Example 2: Rust project with slow compilation
```
Input trace shows: "cargo build" span = 180s, "cargo test" span = 120s
→ Output: Add sccache, cache cargo registry + target dir, use cargo-nextest
→ Expected impact: 300s → 90s (70% reduction)
```

### Example 3: Docker multi-arch build
```
Input trace shows: "Build amd64" = 300s, "Build arm64" = 400s, sequential
→ Output: Add buildx GHA cache, matrix strategy for parallel builds
→ Expected impact: 700s → 420s (40% reduction from caching + parallel)
```
