#!/usr/bin/env python3
"""
End-to-End Evaluation Pipeline for Long-Context Dataset
GSoC 2026 — Issue #23316

Pipeline:
  1. Reads dataset.json
  2. Clones repo (reproducible: exact commit checkout)
  3. Installs language-specific dependencies
  4. Runs validation tests BEFORE fix (expects FAIL)
  5. Validates gold patch (git apply --check)
  6. Applies gold patch
  7. Runs tests AFTER fix (expects PASS)
  8. Computes partial credit score
  9. Classifies failure mode
  10. Reports results

Usage:
    python run_eval.py                          # Run all tasks
    python run_eval.py --task react-5e427913    # Run specific task
    python run_eval.py --validate-only          # Only validate patches apply cleanly
"""

import json
import os
import sys
import subprocess
import tempfile
import argparse
from datetime import datetime

CACHE_DIR = os.path.join(tempfile.gettempdir(), "long-context-eval")

# ─── Language Detection ─────────────────────────────────────────────
EXTENSION_TO_LANGUAGE = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".jsx": "javascript",
    ".go": "go", ".java": "java", ".rs": "rust",
    ".scala": "scala", ".rb": "ruby",
    ".c": "c", ".cpp": "cpp", ".h": "c",
}


def detect_language_from_files(files: list[str]) -> str:
    """Detect primary language from file extensions."""
    counts: dict[str, int] = {}
    for f in files:
        _, ext = os.path.splitext(f)
        lang = EXTENSION_TO_LANGUAGE.get(ext.lower(), "")
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return max(counts, key=counts.get) if counts else "unknown"


def detect_test_command(repo_dir: str, language: str, task_files: list[str]) -> str:
    """Auto-detect test command based on repo structure and language."""
    # Check for specific test framework files
    if os.path.exists(os.path.join(repo_dir, "package.json")):
        # Read package.json to check for test script
        try:
            with open(os.path.join(repo_dir, "package.json"), "r") as f:
                pkg = json.load(f)
            scripts = pkg.get("scripts", {})
            if "test" in scripts:
                return "npm test"
        except (json.JSONDecodeError, IOError):
            pass
        return "npx jest --passWithNoTests"

    if os.path.exists(os.path.join(repo_dir, "pytest.ini")) or \
       os.path.exists(os.path.join(repo_dir, "setup.py")) or \
       os.path.exists(os.path.join(repo_dir, "pyproject.toml")):
        return "python -m pytest --tb=short -q"

    if os.path.exists(os.path.join(repo_dir, "go.mod")):
        # Find Go test packages from task files
        packages = set()
        for f in task_files:
            if f.endswith("_test.go"):
                pkg = os.path.dirname(f)
                packages.add(f"./{pkg}/...")
        if packages:
            return f"go test {' '.join(sorted(packages))}"
        return "go test ./..."

    if os.path.exists(os.path.join(repo_dir, "Cargo.toml")):
        return "cargo test"

    if os.path.exists(os.path.join(repo_dir, "pom.xml")):
        return "mvn test -q"

    if os.path.exists(os.path.join(repo_dir, "build.gradle")) or \
       os.path.exists(os.path.join(repo_dir, "build.gradle.kts")):
        return "gradle test"

    # Fallback by language
    fallback = {
        "python": "python -m pytest --tb=short -q",
        "javascript": "npm test",
        "typescript": "npm test",
        "go": "go test ./...",
        "rust": "cargo test",
        "java": "mvn test -q",
    }
    return fallback.get(language, "echo 'No test command detected'")


# ─── Dependency Installation ────────────────────────────────────────

def install_dependencies(repo_dir: str, language: str, timeout: int = 300) -> dict:
    """Install language-specific dependencies."""
    install_cmds = []

    if language in ("javascript", "typescript"):
        if os.path.exists(os.path.join(repo_dir, "yarn.lock")):
            install_cmds.append(["yarn", "install", "--frozen-lockfile", "--ignore-scripts"])
        elif os.path.exists(os.path.join(repo_dir, "pnpm-lock.yaml")):
            install_cmds.append(["pnpm", "install", "--frozen-lockfile"])
        elif os.path.exists(os.path.join(repo_dir, "package.json")):
            install_cmds.append(["npm", "install", "--ignore-scripts"])

    elif language == "python":
        if os.path.exists(os.path.join(repo_dir, "requirements.txt")):
            install_cmds.append(["pip", "install", "-r", "requirements.txt", "-q"])
        elif os.path.exists(os.path.join(repo_dir, "setup.py")):
            install_cmds.append(["pip", "install", "-e", ".", "-q"])
        elif os.path.exists(os.path.join(repo_dir, "pyproject.toml")):
            install_cmds.append(["pip", "install", "-e", ".", "-q"])

    elif language == "go":
        if os.path.exists(os.path.join(repo_dir, "go.mod")):
            install_cmds.append(["go", "mod", "download"])

    elif language == "rust":
        if os.path.exists(os.path.join(repo_dir, "Cargo.toml")):
            install_cmds.append(["cargo", "fetch"])

    elif language == "java":
        if os.path.exists(os.path.join(repo_dir, "pom.xml")):
            install_cmds.append(["mvn", "dependency:resolve", "-q"])

    if not install_cmds:
        return {"status": "skipped", "reason": "no dependency file found"}

    for cmd in install_cmds:
        try:
            result = subprocess.run(
                cmd, cwd=repo_dir,
                capture_output=True, text=True,
                timeout=timeout,
                encoding="utf-8", errors="replace",
            )
            if result.returncode != 0:
                return {
                    "status": "failed",
                    "command": " ".join(cmd),
                    "error": result.stderr[-500:],
                }
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "command": " ".join(cmd)}
        except FileNotFoundError:
            return {"status": "tool_missing", "command": cmd[0]}

    return {"status": "ok", "commands": [" ".join(c) for c in install_cmds]}


# ─── Git Operations ─────────────────────────────────────────────────

def clone_repo(repo_url: str, cache_name: str) -> str:
    """Clone or use cached repo with reproducible checkout support."""
    repo_dir = os.path.join(CACHE_DIR, cache_name)

    if os.path.exists(repo_dir):
        print(f"  💾 Using cached clone: {repo_dir}")
        return repo_dir

    os.makedirs(CACHE_DIR, exist_ok=True)
    print(f"  📥 Cloning {repo_url}...")

    # Use filter clone for faster, more reproducible checkout
    result = subprocess.run(
        ["git", "clone", "--filter=blob:limit=5m", "--no-checkout", repo_url, repo_dir],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        # Fallback to shallow clone
        result = subprocess.run(
            ["git", "clone", "--depth=500", repo_url, repo_dir],
            capture_output=True, text=True
        )
    if result.returncode != 0:
        print(f"  ❌ Clone failed: {result.stderr[:200]}")
        return ""
    return repo_dir


def checkout_commit(repo_dir: str, commit: str) -> bool:
    """Checkout specific commit with reliable resolution."""
    # Clean workspace
    subprocess.run(["git", "clean", "-fdx"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "checkout", "."], cwd=repo_dir, capture_output=True)

    # Resolve commit reference (handles ^ ~N syntax)
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
        # Fetch more history if needed
        print(f"  ⚠️  Commit not found, fetching more history...")
        subprocess.run(
            ["git", "fetch", "--deepen=2000"],
            cwd=repo_dir, capture_output=True
        )
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


# ─── Test Execution ─────────────────────────────────────────────────

def run_validation(repo_dir: str, command: str, timeout: int = 180) -> dict:
    """Run validation command and capture results."""
    try:
        # Use shell=False when possible by splitting command
        # Fall back to shell=True only for complex commands with pipes/redirects
        use_shell = any(c in command for c in ["|", "&&", "||", ">", "<", ";"])

        if use_shell:
            cmd = command
        else:
            cmd = command.split()

        result = subprocess.run(
            cmd,
            shell=use_shell,
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
            "stdout_tail": result.stdout[-2000:] if result.stdout else "",
            "stderr_tail": result.stderr[-2000:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {
            "exit_code": -1,
            "passed": False,
            "stdout_tail": "",
            "stderr_tail": f"TIMEOUT after {timeout}s",
        }
    except Exception as e:
        return {
            "exit_code": -1,
            "passed": False,
            "stdout_tail": "",
            "stderr_tail": str(e),
        }


# ─── Patch Operations ──────────────────────────────────────────────

def validate_patch(repo_dir: str, patch_text: str) -> tuple[bool, str]:
    """Dry-run patch to check if it applies cleanly."""
    patch_file = os.path.join(repo_dir, ".eval_patch_check.diff")
    with open(patch_file, "w", encoding="utf-8") as f:
        f.write(patch_text)

    result = subprocess.run(
        ["git", "apply", "--check", patch_file],
        cwd=repo_dir, capture_output=True, text=True
    )
    os.remove(patch_file)

    if result.returncode == 0:
        return True, "clean"
    return False, result.stderr[:500]


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
        # Fallback: try with --3way for fuzzy matching
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


# ─── Scoring & Classification ──────────────────────────────────────

def compute_partial_score(
    before_result: dict, after_result: dict,
    expected_files: list[str], actual_files: list[str],
) -> dict:
    """Compute weighted partial credit score."""
    # File overlap (0-1)
    expected_set = set(expected_files)
    actual_set = set(actual_files)
    overlap = len(expected_set & actual_set) / max(len(expected_set), 1)

    # Test improvement (0-1)
    before_pass = 1.0 if before_result.get("passed") else 0.0
    after_pass = 1.0 if after_result.get("passed") else 0.0
    test_improvement = max(0, after_pass - before_pass)

    # Patch precision: what fraction of agent changes are in the gold set
    precision = len(expected_set & actual_set) / max(len(actual_set), 1)

    # Weighted score
    score = round(0.4 * overlap + 0.4 * test_improvement + 0.2 * precision, 3)

    return {
        "total_score": score,
        "file_overlap": round(overlap, 3),
        "test_improvement": round(test_improvement, 3),
        "patch_precision": round(precision, 3),
    }


def classify_failure_mode(
    before_result: dict, after_result: dict,
    expected_files: list[str], actual_files: list[str],
    required_context: list[str],
) -> str:
    """Classify failure into taxonomy categories."""
    expected_set = set(expected_files)
    actual_set = set(actual_files)
    overlap = len(expected_set & actual_set) / max(len(expected_set), 1)

    # 1. Context Selection: agent barely touched the right files
    if overlap < 0.2:
        return "context_selection"

    # 2. Hallucinated Code: agent modified files not in expected or context
    context_set = set(required_context)
    hallucinated = actual_set - expected_set - context_set
    if len(hallucinated) > len(actual_set) * 0.5:
        return "hallucinated_code"

    # 3. Cross-Module: agent only touched one module
    actual_modules = set(f.split("/")[0] for f in actual_files if "/" in f)
    expected_modules = set(f.split("/")[0] for f in expected_files if "/" in f)
    if len(actual_modules) == 1 and len(expected_modules) > 1:
        return "cross_module_reasoning"

    # 4. Scope Creep: too many extra files
    if len(actual_set - expected_set) > len(expected_set):
        return "scope_creep"

    # 5. Cascading Breakage: tests were passing before, failing after
    if before_result.get("passed") and not after_result.get("passed"):
        return "cascading_breakage"

    # 6. Shallow Fix: partial overlap but tests still fail
    if not after_result.get("passed") and overlap > 0.3:
        return "shallow_fix"

    return "partial_fix"


# ─── Main Evaluation ──────────────────────────────────────────────

def evaluate_task(task: dict, validate_only: bool = False) -> dict:
    """Run full evaluation pipeline on a single task."""
    task_id = task.get("task_id", "unknown")
    repo_meta = task.get("repository_metadata", {})
    repo_url = repo_meta.get("repo_url", "")
    base_commit = repo_meta.get("base_commit", "")

    # Get files and context info
    ctx = task.get("context_requirements", {})
    modified_files = ctx.get("modified_files", [])
    required_context = ctx.get("required_context_files", [])

    eval_params = task.get("evaluation_parameters", {})
    gold_patch = eval_params.get("gold_patch", "")
    validation_cmd = eval_params.get("validation_command", "")

    print(f"\n{'='*60}")
    print(f"🧪 Evaluating: {task_id}")
    print(f"   Repo: {repo_url}")
    print(f"   Commit: {base_commit[:12] if base_commit else 'N/A'}")
    print(f"{'='*60}")

    result = {
        "task_id": task_id,
        "timestamp": datetime.now().isoformat(),
        "repo": repo_url,
        "base_commit": base_commit,
        "language": repo_meta.get("language", "unknown"),
        "steps": {},
        "overall": "NOT_RUN",
        "score": None,
        "failure_mode": None,
    }

    # Step 1: Clone
    print("\n📦 Step 1: Clone repository")
    cache_name = repo_url.rstrip("/").split("/")[-1]
    repo_dir = clone_repo(repo_url, cache_name)
    if not repo_dir:
        result["overall"] = "CLONE_FAILED"
        return result
    result["steps"]["clone"] = {"status": "ok", "path": repo_dir}

    # Step 2: Checkout base commit
    print(f"\n📌 Step 2: Checkout {base_commit[:12] if base_commit else 'N/A'}")
    if not checkout_commit(repo_dir, base_commit):
        result["overall"] = "CHECKOUT_FAILED"
        print(f"  ❌ Could not checkout {base_commit}")
        return result
    result["steps"]["checkout"] = {"status": "ok"}
    print(f"  ✅ Checked out {base_commit[:12]}")

    # Step 3: Detect language and install dependencies
    language = detect_language_from_files(modified_files)
    result["language"] = language
    print(f"\n📦 Step 3: Install dependencies ({language})")
    install_result = install_dependencies(repo_dir, language)
    result["steps"]["install_deps"] = install_result
    print(f"  {'✅' if install_result['status'] == 'ok' else '⚠️'} {install_result['status']}")

    # Step 4: Auto-detect test command if needed
    if not validation_cmd or validation_cmd.startswith("node -e") or validation_cmd.startswith("echo"):
        validation_cmd = detect_test_command(repo_dir, language, modified_files)
        print(f"  🔍 Auto-detected test command: {validation_cmd}")
    result["validation_command"] = validation_cmd

    # Step 5: Validate patch (dry-run)
    if gold_patch:
        print(f"\n🔍 Step 5: Validate patch (dry-run)")
        patch_ok, patch_msg = validate_patch(repo_dir, gold_patch)
        result["steps"]["patch_validation"] = {"clean": patch_ok, "message": patch_msg}
        print(f"  {'✅' if patch_ok else '❌'} Patch validation: {patch_msg}")

        if validate_only:
            result["overall"] = "PATCH_VALID" if patch_ok else "PATCH_INVALID"
            return result

    # Step 6: Run tests BEFORE fix
    print(f"\n🧪 Step 6: Run validation BEFORE fix")
    before_result = run_validation(repo_dir, validation_cmd)
    result["steps"]["test_before_fix"] = before_result
    status = "✅ PASS" if before_result["passed"] else "❌ FAIL"
    print(f"  {status} (exit code: {before_result['exit_code']})")

    # Step 7: Apply gold patch and test AFTER fix
    if gold_patch:
        print(f"\n🔧 Step 7: Apply gold patch")
        patch_ok = apply_patch(repo_dir, gold_patch)
        actual_modified = list_modified_files(repo_dir)
        result["steps"]["apply_patch"] = {
            "status": "ok" if patch_ok else "failed",
            "files_touched": actual_modified,
        }
        print(f"  {'✅' if patch_ok else '❌'} Patch applied ({len(actual_modified)} files)")

        # Step 8: Run tests AFTER fix
        print(f"\n🧪 Step 8: Run validation AFTER fix")
        after_result = run_validation(repo_dir, validation_cmd)
        result["steps"]["test_after_fix"] = after_result
        status = "✅ PASS" if after_result["passed"] else "❌ FAIL"
        print(f"  {status} (exit code: {after_result['exit_code']})")

        # Step 9: Compute score and classify
        score = compute_partial_score(
            before_result, after_result,
            modified_files, actual_modified,
        )
        result["score"] = score

        # Determine overall result
        if before_result["passed"] and after_result["passed"]:
            result["overall"] = "TASK_INVALID_TESTS_ALREADY_PASS"
            result["failure_mode"] = "invalid_task"
        elif not before_result["passed"] and after_result["passed"]:
            result["overall"] = "PATCH_FIXES_TEST"
        elif not after_result["passed"]:
            result["overall"] = "PATCH_INSUFFICIENT"
            result["failure_mode"] = classify_failure_mode(
                before_result, after_result,
                modified_files, actual_modified, required_context,
            )
        else:
            result["overall"] = "UNEXPECTED"
    else:
        result["overall"] = "NO_PATCH_PROVIDED"
        result["steps"]["note"] = "No gold patch — would send to AI agent"

    # Summary
    print(f"\n{'─'*60}")
    print(f"📊 Result: {result['overall']}")
    if result["score"]:
        print(f"   Score: {result['score']['total_score']} (overlap={result['score']['file_overlap']}, test={result['score']['test_improvement']}, precision={result['score']['patch_precision']})")
    if result["failure_mode"]:
        print(f"   Failure mode: {result['failure_mode']}")

    return result


def main():
    parser = argparse.ArgumentParser(description="Run long-context evaluation pipeline")
    parser.add_argument("--dataset", default="dataset.json",
                       help="Path to dataset.json")
    parser.add_argument("--task", default=None,
                       help="Run specific task by ID")
    parser.add_argument("--validate-only", action="store_true",
                       help="Only validate patches apply cleanly (no test execution)")
    parser.add_argument("--timeout", type=int, default=180,
                       help="Test timeout in seconds (default: 180)")
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

    if args.task:
        tasks = [t for t in tasks if t.get("task_id") == args.task]
        if not tasks:
            print(f"❌ Task '{args.task}' not found")
            sys.exit(1)

    print(f"{'='*60}")
    print(f"🔍 Long-Context Evaluation Pipeline v2.0")
    print(f"   Dataset: {dataset_path}")
    print(f"   Tasks: {len(tasks)}")
    print(f"   Mode: {'validate-only' if args.validate_only else 'full evaluation'}")
    print(f"{'='*60}")

    results = []
    for task in tasks:
        result = evaluate_task(task, validate_only=args.validate_only)
        results.append(result)

    # Write results
    output = {
        "metadata": {
            "pipeline": "long-context-eval",
            "version": "2.0.0",
            "timestamp": datetime.now().isoformat(),
            "tasks_evaluated": len(results),
            "mode": "validate-only" if args.validate_only else "full",
        },
        "summary": {
            "total": len(results),
            "patch_fixes_test": sum(1 for r in results if r["overall"] == "PATCH_FIXES_TEST"),
            "patch_insufficient": sum(1 for r in results if r["overall"] == "PATCH_INSUFFICIENT"),
            "invalid_tasks": sum(1 for r in results if r["overall"] == "TASK_INVALID_TESTS_ALREADY_PASS"),
            "errors": sum(1 for r in results if r["overall"] in ("CLONE_FAILED", "CHECKOUT_FAILED")),
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
    icons = {
        "PATCH_FIXES_TEST": "✅", "TASK_INVALID_TESTS_ALREADY_PASS": "🟡",
        "PATCH_INSUFFICIENT": "❌", "NO_PATCH_PROVIDED": "📝",
        "PATCH_VALID": "🔍", "PATCH_INVALID": "💔",
        "CLONE_FAILED": "💥", "CHECKOUT_FAILED": "💥",
    }
    for r in results:
        icon = icons.get(r["overall"], "❓")
        score_str = f" score={r['score']['total_score']}" if r.get("score") else ""
        mode_str = f" [{r['failure_mode']}]" if r.get("failure_mode") else ""
        print(f"  {icon} {r['task_id']}: {r['overall']}{score_str}{mode_str}")

    s = output["summary"]
    print(f"\n  ✅ Fixes test: {s['patch_fixes_test']} | ❌ Insufficient: {s['patch_insufficient']} | 🟡 Invalid: {s['invalid_tasks']} | 💥 Errors: {s['errors']}")
    print(f"\n📄 Results saved: {out_path}")


if __name__ == "__main__":
    main()
