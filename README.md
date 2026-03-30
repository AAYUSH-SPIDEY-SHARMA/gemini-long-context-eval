# Long-Context & Complex Reasoning Coding Evaluation Dataset

**GSoC 2026 — Gemini CLI Issue [#23316](https://github.com/google-gemini/gemini-cli/issues/23316)**

A prototype pipeline for building evaluation datasets that test AI coding assistants
on **real-world, multi-file, cross-module engineering tasks** extracted from open-source
repositories.

## Why This Matters

Current coding benchmarks (SWE-bench, HumanEval) test isolated functions or single-file fixes.
Real software engineering requires reading 20-100+ files across modules, understanding build
systems, dependency chains, and architectural constraints. This pipeline extracts and validates
tasks that **require genuine long-context reasoning** — not shortcut-solvable puzzles.

## Pipeline

```
mine_repos.py          →  ranked_repos.json       (20 scored repositories)
       │
extract_tasks.py       →  sample_tasks.json       (tasks with import-based dependency analysis)
       │
build_dataset.py       →  dataset.json            (clean eval-ready format)
       │
run_eval.py            →  eval_results.json       (pass/fail with logs)
```

## Key Features

| Feature | Description |
|---|---|
| **Import-based dependency graph** | Regex patterns for JS/TS/Python/Go/Java/Rust; 2-level traversal |
| **LCVS scoring** | Long-Context Validation Score (0-100) measuring file count, module diversity, depth |
| **Real token estimation** | Actual file content sizes via `git show`, not averages |
| **Shortcut resistance** | `context_ratio ≥ 2.0 AND tokens ≥ 30K AND modules ≥ 2` — defensible thresholds |
| **Context expansion** | Modified files → transitive imports → full required context |
| **Failure mode classification** | 6 categories: context_selection, cross_module_reasoning, partial_fix, etc. |
| **Gold patch validation** | End-to-end: clone → checkout → test → apply patch → retest |

## Quick Start

```bash
# 1. Score repositories (offline mode, no API key needed)
python mine_repos.py

# 2. Extract tasks from a repository
python extract_tasks.py https://github.com/facebook/react --limit 300

# 3. Generate clean dataset
python build_dataset.py

# 4. Run evaluation pipeline
python run_eval.py
```

## Example Output (facebook/react)

| Task | Files Modified | Context Files | Tokens | Context Ratio | Dep Edges |
|---|---|---|---|---|---|
| react-3cb2c420 | 11 | 38 | ~46K | 3.45 | 52 |
| react-4cf90638 | 4 | 77 | ~190K | 19.25 | 250 |

Both tasks are **shortcut-resistant** (ratio ≥ 2.0) — an AI agent must read significantly
more files than it modifies to solve correctly.

## Files

| File | Description | Lines |
|---|---|---|
| `mine_repos.py` | Repository discovery & scoring pipeline | ~260 |
| `extract_tasks.py` | Git history mining, import-based deps, LCVS scoring | ~720 |
| `build_dataset.py` | Generates clean `dataset.json` from extracted tasks | ~80 |
| `run_eval.py` | End-to-end evaluation: clone → test → patch → retest | ~335 |
| `long-context-helper.ts` | TypeScript eval adapter for Gemini CLI integration | ~270 |

## Integration with Gemini CLI

The `long-context-helper.ts` extends the existing `evalTest()` pattern from `evals/test-helper.ts`
to support external repository checkouts. It includes:
- `longContextEvalTest()` function for repo-level tasks
- Git clone/cache management
- 6-category failure mode classification
- Detailed metrics collection (context utilization, test pass rates)

## Phase 2 (Planned)

- **AST-based filtering** via tree-sitter for language-aware import resolution
- **CNS (Contextual Necessity Score)** formula for quantifying reasoning depth
- **Multi-repo extraction** across top-10 ranked repositories
- **Full Gemini CLI TestRig integration** for automated agent evaluation
