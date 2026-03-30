#!/usr/bin/env python3
"""
End-to-End Evaluation Pipeline for Long-Context Dataset
GSoC 2026 — Issue #23316

Pipeline that:
1. Reads dataset.json
2. Clones repo at base_commit
3. Runs validation tests (should FAIL before fix)
4. Applies gold patch
5. Runs tests again (should PASS after fix)
6. Reports results

Usage:
    python run_eval.py                          # Run all tasks
    python run_eval.py --task django-abc12345   # Run specific task
"""

import json
import os
import sys
import subprocess
import tempfile
import argparse
from datetime import datetime


CACHE_DIR = os.path.join(tempfile.gettempdir(), "long-context-eval")


def clone_repo(repo_url: str, cache_name: str) -> str:
    """Clone or use cached repo."""
    repo_dir = os.path.join(CACHE_DIR, cache_name)

    if os.path.exists(repo_dir):
        print(f"  💾 Using cached clone: {repo_dir}")
        return repo_dir

    os.makedirs(CACHE_DIR, exist_ok=True)
    print(f"  📥 Cloning {repo_url}...")
    result = subprocess.run(
        ["git", "clone", "--depth=500", repo_url, repo_dir],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  ❌ Clone failed: {result.stderr[:200]}")
        return ""
    return repo_dir


def checkout_commit(repo_dir: str, commit: str) -> bool:
    """Checkout specific commit, resolving references like abc^ first."""
    # Clean workspace
    subprocess.run(["git", "clean", "-fdx"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "checkout", "."], cwd=repo_dir, capture_output=True)

    # Resolve commit reference (handles ^ syntax)
    resolve = subprocess.run(
        ["git", "rev-parse", commit],
        cwd=repo_dir, capture_output=True, text=True
    )
    resolved_hash = resolve.stdout.strip() if resolve.returncode == 0 else commit

    result = subprocess.run(
        ["git", "checkout", resolved_hash, "--force"],
        cwd=repo_dir, capture_output=True, text=True
    )
    if result.returncode != 0:
        # Try fetching more history
        print(f"  ⚠️  Commit not found, deepening history...")
        subprocess.run(
            ["git", "fetch", "--deepen=2000"],
            cwd=repo_dir, capture_output=True
        )
        # Re-resolve after fetch
        resolve = subprocess.run(
            ["git", "rev-parse", commit],
            cwd=repo_dir, capture_output=True, text=True
        )
        resolved_hash = resolve.stdout.strip() if resolve.returncode == 0 else commit
        result = subprocess.run(
            ["git", "checkout", resolved_hash, "--force"],
            cwd=repo_dir, capture_output=True, text=True
        )
    return result.returncode == 0


def run_validation(repo_dir: str, command: str, timeout: int = 120) -> dict:
    """Run validation command and capture results."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        return {
            "exit_code": result.returncode,
            "passed": result.returncode == 0,
            "stdout": result.stdout[-2000:] if result.stdout else "",
            "stderr": result.stderr[-2000:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {
            "exit_code": -1,
            "passed": False,
            "stdout": "",
            "stderr": f"TIMEOUT after {timeout}s",
        }
    except Exception as e:
        return {
            "exit_code": -1,
            "passed": False,
            "stdout": "",
            "stderr": str(e),
        }


def apply_patch(repo_dir: str, patch_text: str) -> bool:
    """Apply a unified diff patch."""
    patch_file = os.path.join(repo_dir, ".eval_patch.diff")
    with open(patch_file, "w", encoding="utf-8") as f:
        f.write(patch_text)

    result = subprocess.run(
        ["git", "apply", "--verbose", patch_file],
        cwd=repo_dir, capture_output=True, text=True
    )
    os.remove(patch_file)

    if result.returncode != 0:
        print(f"  ⚠️  Patch apply warning: {result.stderr[:200]}")
        # Try with --3way
        with open(patch_file, "w", encoding="utf-8") as f:
            f.write(patch_text)
        result = subprocess.run(
            ["git", "apply", "--3way", patch_file],
            cwd=repo_dir, capture_output=True, text=True
        )
        if os.path.exists(patch_file):
            os.remove(patch_file)

    return result.returncode == 0


def list_modified_files(repo_dir: str) -> list[str]:
    """List files modified in the working directory."""
    result = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=repo_dir, capture_output=True, text=True
    )
    if result.returncode == 0:
        return [f for f in result.stdout.strip().split("\n") if f]
    return []


def evaluate_task(task: dict) -> dict:
    """Run full evaluation pipeline on a single task."""
    task_id = task["task_id"]
    repo_url = task["repo"]
    base_commit = task["base_commit"]
    validation_cmd = task["validation"]
    gold_patch = task.get("gold_patch", "")

    print(f"\n{'='*60}")
    print(f"🧪 Evaluating: {task_id}")
    print(f"   Repo: {repo_url}")
    print(f"   Commit: {base_commit[:12]}")
    print(f"   Validation: {validation_cmd[:60]}")
    print(f"{'='*60}")

    result = {
        "task_id": task_id,
        "timestamp": datetime.now().isoformat(),
        "repo": repo_url,
        "base_commit": base_commit,
        "steps": {},
        "overall": "NOT_RUN",
    }

    # Step 1: Clone
    print("\n📦 Step 1: Clone repository")
    cache_name = repo_url.split("/")[-1]
    repo_dir = clone_repo(repo_url, cache_name)
    if not repo_dir:
        result["overall"] = "CLONE_FAILED"
        return result
    result["steps"]["clone"] = {"status": "ok", "path": repo_dir}

    # Step 2: Checkout base commit (state BEFORE the fix)
    print(f"\n📌 Step 2: Checkout {base_commit[:12]}")
    if not checkout_commit(repo_dir, base_commit):
        result["overall"] = "CHECKOUT_FAILED"
        print(f"  ❌ Could not checkout {base_commit}")
        return result
    result["steps"]["checkout"] = {"status": "ok"}
    print(f"  ✅ Checked out {base_commit[:12]}")

    # Step 3: Run tests BEFORE fix (expect failure or specific behavior)
    print(f"\n🧪 Step 3: Run validation BEFORE fix")
    before_result = run_validation(repo_dir, validation_cmd)
    result["steps"]["test_before_fix"] = before_result
    status = "✅ PASS" if before_result["passed"] else "❌ FAIL"
    print(f"  {status} (exit code: {before_result['exit_code']})")
    if before_result["stderr"]:
        lines = before_result["stderr"].strip().split("\n")
        for line in lines[-5:]:
            print(f"  │ {line[:100]}")

    # Step 4: Apply gold patch (the actual fix)
    if gold_patch:
        print(f"\n🔧 Step 4: Apply gold patch")
        patch_ok = apply_patch(repo_dir, gold_patch)
        result["steps"]["apply_patch"] = {"status": "ok" if patch_ok else "failed"}
        modified = list_modified_files(repo_dir)
        result["steps"]["apply_patch"]["files_touched"] = modified
        print(f"  {'✅' if patch_ok else '❌'} Patch applied ({len(modified)} files modified)")
        for f in modified[:10]:
            print(f"  │ {f}")

        # Step 5: Run tests AFTER fix (expect pass)
        print(f"\n🧪 Step 5: Run validation AFTER fix")
        after_result = run_validation(repo_dir, validation_cmd)
        result["steps"]["test_after_fix"] = after_result
        status = "✅ PASS" if after_result["passed"] else "❌ FAIL"
        print(f"  {status} (exit code: {after_result['exit_code']})")

        # Determine overall result
        if not before_result["passed"] and after_result["passed"]:
            result["overall"] = "PATCH_FIXES_TEST"
        elif before_result["passed"] and after_result["passed"]:
            result["overall"] = "TEST_ALREADY_PASSES"
        elif not after_result["passed"]:
            result["overall"] = "PATCH_INSUFFICIENT"
        else:
            result["overall"] = "UNEXPECTED"
    else:
        result["overall"] = "NO_PATCH_PROVIDED"
        result["steps"]["test_before_fix"]["note"] = "No gold patch — would send to AI agent"

    # Summary
    print(f"\n{'─'*60}")
    print(f"📊 Result: {result['overall']}")

    # Log context info
    result["context"] = {
        "files_in_task": task.get("files", []),
        "task_description": task.get("task", ""),
        "category": task.get("category", ""),
    }

    return result


def main():
    parser = argparse.ArgumentParser(description="Run evaluation pipeline")
    parser.add_argument("--dataset", default="dataset.json",
                       help="Path to dataset.json (default: dataset.json)")
    parser.add_argument("--task", default=None,
                       help="Run specific task by ID")
    parser.add_argument("--no-patch", action="store_true",
                       help="Skip patch application (test discovery only)")
    args = parser.parse_args()

    dataset_path = args.dataset
    if not os.path.isabs(dataset_path):
        dataset_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), dataset_path)

    if not os.path.exists(dataset_path):
        print(f"❌ Dataset not found: {dataset_path}")
        sys.exit(1)

    with open(dataset_path, "r", encoding="utf-8") as f:
        dataset = json.load(f)

    tasks = dataset.get("tasks", [])
    if not tasks:
        print("❌ No tasks in dataset")
        sys.exit(1)

    # Filter by task ID if specified
    if args.task:
        tasks = [t for t in tasks if t["task_id"] == args.task]
        if not tasks:
            print(f"❌ Task '{args.task}' not found")
            sys.exit(1)

    print(f"{'='*60}")
    print(f"🔍 Long-Context Evaluation Pipeline")
    print(f"   Dataset: {dataset_path}")
    print(f"   Tasks: {len(tasks)}")
    print(f"{'='*60}")

    results = []
    for task in tasks:
        if args.no_patch:
            task["gold_patch"] = ""
        result = evaluate_task(task)
        results.append(result)

    # Write results
    output = {
        "metadata": {
            "pipeline": "long-context-eval",
            "version": "1.0.0",
            "timestamp": datetime.now().isoformat(),
            "tasks_evaluated": len(results),
        },
        "results": results,
    }

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # Final summary
    print(f"\n{'='*60}")
    print(f"📊 EVALUATION SUMMARY")
    print(f"{'='*60}")
    for r in results:
        icon = {"PATCH_FIXES_TEST": "✅", "TEST_ALREADY_PASSES": "🟡",
                "PATCH_INSUFFICIENT": "❌", "NO_PATCH_PROVIDED": "📝"}.get(r["overall"], "❓")
        print(f"  {icon} {r['task_id']}: {r['overall']}")
    print(f"\n📄 Results saved: {out_path}")


if __name__ == "__main__":
    main()
