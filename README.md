# Long-Context Coding Evaluation Pipeline

**GSoC 2026 — Issue #23316 — Gemini CLI**

A production-grade pipeline for extracting, validating, and evaluating long-context coding tasks from real-world open-source repositories.

## What This Does

```
mine_repos.py → extract_tasks.py → build_dataset.py → run_eval.py
  (score repos)    (mine git history)   (validate & filter)  (clone → test → patch → retest)
```

## Quick Start

### 1. Mine Repositories

```bash
# Recommended: use GitHub API for accurate data
GITHUB_TOKEN=ghp_xxx python mine_repos.py

# Fallback: curated metadata (no API needed)
python mine_repos.py
```

**Output:** `ranked_repos.json` — 20 scored repositories

### 2. Extract Tasks from a Repository

```bash
python extract_tasks.py https://github.com/facebook/react --limit 100 --top 5
```

**Output:** `sample_tasks.json` — extracted tasks with:
- Import-based dependency graph (2-level transitive expansion)
- LCVS scoring (0-100)
- Shortcut resistance validation (context ratio ≥ 2.0, tokens ≥ 30K, modules ≥ 2)
- Language detection and post-training-cutoff filtering

### 3. Build Validated Dataset

```bash
python build_dataset.py --input sample_tasks.json --min-lcvs 60
```

**Output:** `dataset.json` — validates schema, enforces quality thresholds, auto-detects languages and test commands. Tasks failing LCVS, context ratio, or cutoff checks are rejected with logged reasons.

### 4. Run Evaluation

```bash
python run_eval.py                          # Run all tasks
python run_eval.py --task react-5e427913    # Run specific task
python run_eval.py --validate-only          # Only check patches apply cleanly
```

**Pipeline per task:**
1. Clone repo → checkout `base_commit`
2. Install language-specific dependencies (npm/pip/go mod/cargo)
3. Auto-detect test command
4. Validate patch (`git apply --check`)
5. Run tests BEFORE fix (expect FAIL)
6. Apply gold patch
7. Run tests AFTER fix (expect PASS)
8. Compute partial credit score (file overlap + test improvement + patch precision)
9. Classify failure mode (6 categories)

**Output:** `eval_results.json`

## Pipeline Components

| File | Lines | Description |
|---|---|---|
| `mine_repos.py` | 262 | Repository scoring with GitHub API + offline fallback |
| `extract_tasks.py` | 750+ | Task extraction with dependency graph analysis |
| `build_dataset.py` | 260+ | Schema validation, quality filtering, language detection |
| `run_eval.py` | 490+ | End-to-end evaluation with scoring and classification |
| `long-context-helper.ts` | 360+ | TypeScript adapter for Gemini CLI integration |

## Quality Guarantees

Every accepted task must satisfy:

| Criterion | Threshold | Why |
|---|---|---|
| LCVS score | ≥ 60 | Composite difficulty score |
| Context ratio | ≥ 2.0 | Agent reads 2× more files than it edits |
| Token volume | ≥ 30,000 | Minimum context window utilization |
| Module count | ≥ 2 | Cross-module reasoning required |
| Commit date | ≥ 2024-06-01 | Post-training cutoff (contamination defense) |

## Failure Mode Taxonomy

| Mode | Detection |
|---|---|
| Context Selection | Agent didn't read required files |
| Cross-Module Reasoning | Fixed one module, missed cascade |
| Cascading Breakage | Fix breaks unrelated tests |
| Scope Creep | Too many unnecessary changes |
| Hallucinated Code | Invented non-existent functions |
| Shallow Fix | Tests pass but wrong architecture |

## Requirements

- Python 3.10+
- `git` in PATH
- Language-specific tools as needed: `npm`, `pip`, `go`, `cargo`
- Optional: `GITHUB_TOKEN` for API enrichment
