#!/usr/bin/env python3
"""
Dataset Builder — Auto-generates dataset.json from extracted tasks.
GSoC 2026 — Issue #23316

Reads sample_tasks.json → validates → filters → produces dataset.json

Pipeline:
  1. Load sample_tasks.json (output of extract_tasks.py)
  2. Validate schema (required fields, types)
  3. Enforce quality thresholds (LCVS ≥ 60, context_ratio ≥ 2.0, modules ≥ 2)
  4. Detect language + auto-detect test commands
  5. Output clean dataset.json with unified schema

Usage:
    python build_dataset.py                                # Default: sample_tasks.json → dataset.json
    python build_dataset.py --input tasks.json --min-lcvs 55
"""

import json
import os
import sys
import argparse
from datetime import datetime

# ─── Quality Thresholds ─────────────────────────────────────────────
DEFAULT_MIN_LCVS = 60
DEFAULT_MIN_CONTEXT_RATIO = 2.0
DEFAULT_MIN_MODULES = 2
DEFAULT_MIN_TOKENS = 30000
POST_TRAINING_CUTOFF = "2024-06-01"

# ─── Language Detection ─────────────────────────────────────────────
EXTENSION_TO_LANGUAGE = {
    ".py": "Python", ".pyx": "Python",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".jsx": "JavaScript",
    ".go": "Go",
    ".java": "Java", ".kt": "Kotlin", ".scala": "Scala",
    ".rs": "Rust",
    ".c": "C", ".h": "C", ".cpp": "C++", ".hpp": "C++", ".cc": "C++",
    ".rb": "Ruby",
    ".dart": "Dart",
}

# ─── Test Command Detection ─────────────────────────────────────────
LANGUAGE_TEST_COMMANDS = {
    "Python": "pytest",
    "JavaScript": "npm test",
    "TypeScript": "npm test",
    "Go": "go test ./...",
    "Java": "mvn test",
    "Rust": "cargo test",
    "Scala": "sbt test",
    "Ruby": "bundle exec rspec",
    "C": "make test",
    "C++": "make test",
}


def detect_language(files: list[str]) -> str:
    """Detect primary language from file extensions."""
    lang_counts: dict[str, int] = {}
    for f in files:
        _, ext = os.path.splitext(f)
        ext = ext.lower()
        lang = EXTENSION_TO_LANGUAGE.get(ext)
        if lang:
            lang_counts[lang] = lang_counts.get(lang, 0) + 1

    if not lang_counts:
        return "Unknown"
    return max(lang_counts, key=lang_counts.get)


def detect_test_command(language: str, modified_files: list[str]) -> str:
    """Auto-detect appropriate test command based on language and files."""
    # Check for specific test framework indicators in file paths
    for f in modified_files:
        if "pytest" in f or "conftest" in f:
            return "pytest"
        if "jest" in f or ".test." in f or ".spec." in f:
            return "npx jest"
        if "_test.go" in f:
            # Extract Go package path
            parts = f.rsplit("/", 1)
            pkg = parts[0] if len(parts) > 1 else "."
            return f"go test ./{pkg}/..."

    return LANGUAGE_TEST_COMMANDS.get(language, "echo 'No test command detected'")


REQUIRED_FIELDS = {
    "task_id": str,
    "repository_metadata": dict,
    "task_description": dict,
    "context_requirements": dict,
    "evaluation_parameters": dict,
    "long_context_metrics": dict,
    "shortcut_resistance": dict,
}

REQUIRED_METRICS = [
    "lcvs_score", "source_files_modified", "total_context_files",
    "context_ratio", "dependency_depth_estimate", "dependency_edges_count",
]


def validate_task_schema(task: dict) -> tuple[bool, str]:
    """Validate that a task has all required fields and correct types."""
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in task:
            return False, f"missing field: {field}"
        if not isinstance(task[field], expected_type):
            return False, f"invalid type for {field}: expected {expected_type.__name__}"

    metrics = task.get("long_context_metrics", {})
    for metric in REQUIRED_METRICS:
        if metric not in metrics:
            return False, f"missing metric: {metric}"

    if "gold_patch" not in task.get("evaluation_parameters", {}):
        return False, "missing gold_patch in evaluation_parameters"

    if not task.get("evaluation_parameters", {}).get("gold_patch"):
        return False, "gold_patch is empty"

    return True, "valid"


def check_quality_thresholds(
    task: dict,
    min_lcvs: float,
    min_context_ratio: float,
    min_modules: int,
    min_tokens: int,
) -> tuple[bool, str]:
    """Check if task meets quality thresholds."""
    metrics = task["long_context_metrics"]

    lcvs = metrics.get("lcvs_score", 0)
    if lcvs < min_lcvs:
        return False, f"LCVS too low ({lcvs:.1f} < {min_lcvs})"

    ratio = metrics.get("context_ratio", 0)
    if ratio < min_context_ratio:
        return False, f"context ratio too low ({ratio:.2f} < {min_context_ratio})"

    modules = metrics.get("distinct_modules", 0)
    if isinstance(modules, int) and modules < min_modules:
        return False, f"too few modules ({modules} < {min_modules})"

    tokens = task.get("context_requirements", {}).get("context_size_estimate_tokens", 0)
    if tokens < min_tokens:
        return False, f"context tokens too low ({tokens:,} < {min_tokens:,})"

    return True, "passes all thresholds"


def check_post_cutoff(task: dict, cutoff: str) -> tuple[bool, str]:
    """Ensure commit is after post-training cutoff date."""
    commit_date = task.get("repository_metadata", {}).get("commit_date", "")
    if not commit_date:
        return True, "no date available (accepted)"

    # Parse ISO date — handle timezone suffixes
    date_str = commit_date[:10]  # YYYY-MM-DD
    if date_str < cutoff:
        return False, f"commit before cutoff ({date_str} < {cutoff})"
    return True, "post-cutoff"


def enrich_task(task: dict) -> dict:
    """Enrich task with detected language and test command."""
    all_files = (
        task.get("context_requirements", {}).get("modified_files", []) +
        task.get("context_requirements", {}).get("required_context_files", [])
    )

    # Detect language
    language = detect_language(all_files)
    task["repository_metadata"]["language"] = language

    # Auto-detect test command if validation is weak
    current_validation = task.get("evaluation_parameters", {}).get("validation_command", "")
    if not current_validation or current_validation.startswith("node -e"):
        modified = task.get("context_requirements", {}).get("modified_files", [])
        task["evaluation_parameters"]["validation_command"] = detect_test_command(language, modified)

    return task


def build_dataset(
    input_path: str,
    output_path: str,
    min_lcvs: float,
    min_context_ratio: float,
    min_modules: int,
    min_tokens: int,
    cutoff: str,
) -> None:
    """Build validated dataset from extracted tasks."""
    # Load input
    if not os.path.exists(input_path):
        print(f"❌ Input not found: {input_path}")
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    tasks = raw.get("tasks", [])
    if not tasks:
        print("❌ No tasks found in input file")
        sys.exit(1)

    print(f"📥 Loaded {len(tasks)} tasks from {input_path}")
    print(f"   Thresholds: LCVS≥{min_lcvs} | Ratio≥{min_context_ratio} | Modules≥{min_modules} | Tokens≥{min_tokens:,}")
    print(f"   Cutoff: {cutoff}")
    print()

    accepted = []
    rejected = []

    for task in tasks:
        task_id = task.get("task_id", "unknown")

        # Step 1: Schema validation
        valid, reason = validate_task_schema(task)
        if not valid:
            rejected.append({"task_id": task_id, "reason": f"schema: {reason}"})
            print(f"  ❌ {task_id}: schema validation failed — {reason}")
            continue

        # Step 2: Post-training cutoff filter
        passes_cutoff, cutoff_reason = check_post_cutoff(task, cutoff)
        if not passes_cutoff:
            rejected.append({"task_id": task_id, "reason": f"cutoff: {cutoff_reason}"})
            print(f"  ❌ {task_id}: {cutoff_reason}")
            continue

        # Step 3: Quality thresholds
        passes_quality, quality_reason = check_quality_thresholds(
            task, min_lcvs, min_context_ratio, min_modules, min_tokens
        )
        if not passes_quality:
            rejected.append({"task_id": task_id, "reason": f"quality: {quality_reason}"})
            print(f"  ⚠️  {task_id}: {quality_reason}")
            continue

        # Step 4: Enrich with language + test command
        task = enrich_task(task)

        accepted.append(task)
        metrics = task["long_context_metrics"]
        print(f"  ✅ {task_id}: LCVS={metrics['lcvs_score']:.1f} | Ratio={metrics['context_ratio']:.2f} | Lang={task['repository_metadata']['language']}")

    print()
    print(f"{'='*60}")
    print(f"📊 Results: {len(accepted)} accepted / {len(rejected)} rejected / {len(tasks)} total")

    # Build output
    output = {
        "metadata": {
            "pipeline": "long-context-eval-dataset",
            "version": "2.0.0",
            "generated_at": datetime.now().isoformat(),
            "source": input_path,
            "description": "Long-context coding evaluation dataset — auto-generated from extracted tasks",
            "quality_thresholds": {
                "min_lcvs": min_lcvs,
                "min_context_ratio": min_context_ratio,
                "min_modules": min_modules,
                "min_context_tokens": min_tokens,
                "post_training_cutoff": cutoff,
            },
            "stats": {
                "tasks_input": len(tasks),
                "tasks_accepted": len(accepted),
                "tasks_rejected": len(rejected),
            },
        },
        "tasks": accepted,
        "rejected_tasks": [
            {"task_id": r["task_id"], "rejection_reason": r["reason"]}
            for r in rejected
        ],
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"📄 Output: {output_path}")

    if rejected:
        print(f"\n🔍 Rejection log:")
        for r in rejected:
            print(f"   • {r['task_id']}: {r['reason']}")


def main():
    parser = argparse.ArgumentParser(description="Build validated dataset from extracted tasks")
    parser.add_argument("--input", default="sample_tasks.json",
                       help="Input file (output of extract_tasks.py)")
    parser.add_argument("--output", default="dataset.json",
                       help="Output dataset file")
    parser.add_argument("--min-lcvs", type=float, default=DEFAULT_MIN_LCVS,
                       help=f"Minimum LCVS score (default: {DEFAULT_MIN_LCVS})")
    parser.add_argument("--min-ratio", type=float, default=DEFAULT_MIN_CONTEXT_RATIO,
                       help=f"Minimum context ratio (default: {DEFAULT_MIN_CONTEXT_RATIO})")
    parser.add_argument("--min-modules", type=int, default=DEFAULT_MIN_MODULES,
                       help=f"Minimum distinct modules (default: {DEFAULT_MIN_MODULES})")
    parser.add_argument("--min-tokens", type=int, default=DEFAULT_MIN_TOKENS,
                       help=f"Minimum context tokens (default: {DEFAULT_MIN_TOKENS})")
    parser.add_argument("--cutoff", default=POST_TRAINING_CUTOFF,
                       help=f"Post-training cutoff date (default: {POST_TRAINING_CUTOFF})")
    args = parser.parse_args()

    # Resolve paths
    base_dir = os.path.dirname(os.path.abspath(__file__))
    input_path = args.input if os.path.isabs(args.input) else os.path.join(base_dir, args.input)
    output_path = args.output if os.path.isabs(args.output) else os.path.join(base_dir, args.output)

    print("=" * 60)
    print("🏗️  Long-Context Dataset Builder v2.0")
    print("=" * 60)

    build_dataset(
        input_path=input_path,
        output_path=output_path,
        min_lcvs=args.min_lcvs,
        min_context_ratio=args.min_ratio,
        min_modules=args.min_modules,
        min_tokens=args.min_tokens,
        cutoff=args.cutoff,
    )


if __name__ == "__main__":
    main()
