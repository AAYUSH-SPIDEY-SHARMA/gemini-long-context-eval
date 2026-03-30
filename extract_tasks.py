#!/usr/bin/env python3
"""
Task Extraction Pipeline for Long-Context Evaluation Dataset
GSoC 2026 — Issue #23316

Mines git history of a repository to extract complex, multi-file
engineering tasks suitable for long-context evaluation.

Features:
  - Git history mining with multi-file filtering
  - Import-based dependency graph construction
  - LCVS scoring with context expansion
  - Anti-shortcut context ratio validation

Usage:
    python extract_tasks.py <repo_url> [--since 2024-01-01] [--limit 50]

Example:
    python extract_tasks.py https://github.com/vercel/next.js --limit 30
"""

import json
import os
import sys
import subprocess
import tempfile
import argparse
import re
from dataclasses import dataclass, field
from typing import Optional


# --- Filtering Criteria ---
MIN_FILES_CHANGED = 3
MIN_LINES_CHANGED = 30
MIN_DISTINCT_DIRS = 2  # Files must span at least N top-level directories
MAX_FILES_CHANGED = 50  # Reject massive refactors/generated code

# Skip patterns (trivial changes)
SKIP_PATTERNS = [
    r"^\.github/",
    r"^\.gitignore",
    r"^\.eslint",
    r"^\.prettier",
    r"node_modules/",
    r"vendor/",
    r"package-lock\.json",
    r"yarn\.lock",
    r"pnpm-lock\.yaml",
    r"go\.sum",
    r"Cargo\.lock",
    r"\.md$",
    r"\.txt$",
    r"CHANGELOG",
    r"LICENSE",
    r"__pycache__",
    r"\.pyc$",
]

# Commit message patterns indicating non-trivial work
INTERESTING_KEYWORDS = [
    "fix", "bug", "error", "crash", "issue", "resolve",
    "refactor", "redesign", "rewrite",
    "feature", "implement", "add support",
    "breaking", "migrate", "update api",
    "race condition", "deadlock", "memory leak",
    "performance", "optimize",
]

# Trivial commit patterns to skip
TRIVIAL_KEYWORDS = [
    "bump", "version", "release", "changelog",
    "merge branch", "merge pull",
    "rename", "typo", "formatting", "lint",
    "chore", "docs:", "ci:", "build:",
    "dependabot", "renovate",
    "auto-generated", "generated",
]

# Import patterns for dependency graph construction (multi-language)
IMPORT_PATTERNS = [
    # JavaScript/TypeScript: import X from 'Y', import 'Y', require('Y')
    r"""import\s+.*?\s+from\s+['"]([^'"]+)['"]""",
    r"""import\s+['"]([^'"]+)['"]""",
    r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""",
    # Python: import X, from X import Y
    r"""^from\s+(\S+)\s+import""",
    r"""^import\s+(\S+)""",
    # Go: import "X"
    r"""^\s*"([^"]+)"$""",
    # Java/Scala: import X.Y.Z
    r"""^import\s+([\w.]+)""",
    # Rust: use X::Y
    r"""^use\s+([\w:]+)""",
]


@dataclass
class FileChange:
    """A single file changed in a commit."""
    path: str
    additions: int = 0
    deletions: int = 0
    is_test: bool = False
    top_dir: str = ""
    module: str = ""

    def __post_init__(self):
        parts = self.path.split("/")
        self.top_dir = parts[0] if parts else ""
        self.module = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
        self.is_test = any(t in self.path.lower() for t in
                          ["test", "spec", "__tests__", "testing", "fixtures"])


@dataclass
class CommitTask:
    """A potential evaluation task derived from a commit."""
    commit_hash: str = ""
    message: str = ""
    author: str = ""
    date: str = ""
    files: list = field(default_factory=list)
    source_files: int = 0
    test_files: int = 0
    total_additions: int = 0
    total_deletions: int = 0
    distinct_dirs: int = 0
    distinct_modules: int = 0
    is_interesting: bool = False
    lcvs_score: float = 0.0
    rejection_reason: str = ""


def should_skip_file(path: str) -> bool:
    """Check if a file path matches skip patterns."""
    for pattern in SKIP_PATTERNS:
        if re.search(pattern, path, re.IGNORECASE):
            return True
    return False


def is_interesting_message(message: str) -> bool:
    """Check if commit message indicates non-trivial work."""
    lower = message.lower()
    # Skip trivial commits
    for kw in TRIVIAL_KEYWORDS:
        if kw in lower:
            return False
    # Check for interesting patterns
    for kw in INTERESTING_KEYWORDS:
        if kw in lower:
            return True
    # Default: accept if message is substantial
    return len(message) > 30


def extract_imports_from_content(content: str) -> list[str]:
    """Extract import paths from file content using multi-language regex."""
    imports = []
    for line in content.split("\n"):
        line = line.strip()
        for pattern in IMPORT_PATTERNS:
            match = re.search(pattern, line)
            if match:
                imp = match.group(1)
                # Filter out noise
                if imp and len(imp) > 1 and not imp.startswith("http"):
                    imports.append(imp)
                break  # One match per line
    return imports


def extract_imports_from_file(repo_dir: str, file_path: str, commit_hash: str) -> list[str]:
    """Extract imports from a file at a specific commit."""
    cmd = ["git", "show", f"{commit_hash}^:{file_path}"]
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=repo_dir,
        encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        return []
    return extract_imports_from_content(result.stdout)


def resolve_import_to_file(import_path: str, source_file: str, all_files: list[str]) -> Optional[str]:
    """
    Resolve an import string to a file in the repo.

    Only matches source code files (not .yml, .md, .css, etc.) to avoid
    false-positive dependency edges.
    """
    # Source code extensions that represent real import targets
    SOURCE_EXTENSIONS = {
        ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
        ".py", ".go", ".rs", ".java", ".scala", ".kt",
        ".c", ".cpp", ".h", ".hpp",
    }

    def is_source_file(path: str) -> bool:
        _, ext = os.path.splitext(path)
        return ext.lower() in SOURCE_EXTENSIONS

    # Normalize: remove leading ./ or ../
    clean = import_path.replace("./", "").replace("../", "")

    # Skip node_modules, external packages (no slash = npm package)
    if not clean or clean.startswith("@") and "/" not in clean[1:]:
        return None
    # Skip single-word npm packages (e.g., "util", "path", "fs")
    if "/" not in clean and "." not in clean:
        return None

    # Try exact path match first (highest confidence)
    for f in all_files:
        if not is_source_file(f):
            continue
        if f == clean or f.endswith("/" + clean):
            return f

    # Try with common extensions appended
    for ext in [".js", ".ts", ".tsx", ".jsx", ".py"]:
        target = clean + ext
        for f in all_files:
            if f == target or f.endswith("/" + target):
                return f

    # Try index file resolution (e.g., "components/X" -> "components/X/index.js")
    for ext in ["/index.js", "/index.ts", "/index.tsx"]:
        target = clean + ext
        for f in all_files:
            if f == target or f.endswith("/" + target):
                return f

    return None


def build_dependency_graph(
    repo_dir: str, commit_hash: str, changed_files: list[str]
) -> tuple[list[str], list[str]]:
    """
    Build real import-based dependency graph from changed files.

    Returns:
        (dependency_edges, context_files) — edges as "A -> B (import)" strings,
        and expanded list of files that form the required context.
    """
    edges = []
    context_files = set(changed_files)

    # Get list of all files in repo at this commit
    cmd = ["git", "ls-tree", "-r", "--name-only", f"{commit_hash}^"]
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=repo_dir,
        encoding="utf-8", errors="replace"
    )
    all_repo_files = result.stdout.strip().split("\n") if result.returncode == 0 else []

    # For each changed file, extract its imports
    for src_file in changed_files:
        imports = extract_imports_from_file(repo_dir, src_file, commit_hash)

        for imp in imports:
            resolved = resolve_import_to_file(imp, src_file, all_repo_files)
            if resolved and resolved != src_file:
                edge = f"{src_file} -> {resolved} (import)"
                if edge not in edges:
                    edges.append(edge)
                context_files.add(resolved)

                # Second-level: also get imports OF the imported file (depth=2)
                second_imports = extract_imports_from_file(repo_dir, resolved, commit_hash)
                for imp2 in second_imports[:10]:  # Limit to avoid explosion
                    resolved2 = resolve_import_to_file(imp2, resolved, all_repo_files)
                    if resolved2 and resolved2 != resolved and resolved2 != src_file:
                        edge2 = f"{resolved} -> {resolved2} (import)"
                        if edge2 not in edges:
                            edges.append(edge2)
                        context_files.add(resolved2)

    return edges, sorted(context_files)


def estimate_tokens_from_files(
    repo_dir: str, file_paths: list[str], commit_hash: str
) -> int:
    """Estimate total tokens by reading actual file sizes at the commit."""
    total_bytes = 0
    for fpath in file_paths:
        cmd = ["git", "show", f"{commit_hash}^:{fpath}"]
        result = subprocess.run(
            cmd, capture_output=True, cwd=repo_dir,
            encoding="utf-8", errors="replace"
        )
        if result.returncode == 0 and result.stdout:
            total_bytes += len(result.stdout)

    # Rough estimate: 1 token ≈ 4 characters
    return total_bytes // 4


def compute_lcvs(task: CommitTask) -> float:
    """
    Compute Long-Context Validation Score (0-100).

    Factors:
      - File count (more files = harder)
      - Directory spread (more dirs = more cross-module)
      - LOC changed (more changes = more complex)
      - Has tests (indicates verifiable task)
    """
    file_score = min(task.source_files / 15, 1.0) * 30
    dir_score = min(task.distinct_dirs / 5, 1.0) * 25
    loc = task.total_additions + task.total_deletions
    loc_score = min(loc / 500, 1.0) * 25
    test_score = 20 if task.test_files > 0 else 5

    return round(file_score + dir_score + loc_score + test_score, 2)


def compute_context_ratio(context_files: int, modified_files: int) -> float:
    """Compute context ratio: required_context_files / modified_files."""
    if modified_files == 0:
        return 0.0
    return round(context_files / modified_files, 2)


def clone_repo(url: str) -> str:
    """Clone repository to temp dir (shallow)."""
    cache_dir = os.path.join(tempfile.gettempdir(), "long-context-mining")
    repo_name = url.rstrip("/").split("/")[-1]
    repo_dir = os.path.join(cache_dir, repo_name)

    if os.path.exists(repo_dir):
        print(f"   💾 Using cached clone: {repo_dir}")
        return repo_dir

    os.makedirs(cache_dir, exist_ok=True)
    print(f"📥 Cloning {url} (shallow)...")
    subprocess.run(
        ["git", "clone", "--depth=500", url, repo_dir],
        capture_output=True, text=True
    )
    return repo_dir


def parse_git_log(repo_dir: str, since: str, limit: int) -> list:
    """Parse git log into commit task structures."""
    cmd = [
        "git", "log", f"--since={since}", f"-{limit}",
        "--pretty=format:%H|%s|%an|%aI",
        "--numstat"
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=repo_dir,
        encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        print(f"  ❌ git log failed: {result.stderr[:200]}")
        return []

    tasks = []
    current = None

    for line in result.stdout.split("\n"):
        line = line.strip()
        if not line:
            continue

        if "|" in line and len(line.split("|")) >= 4:
            # New commit header
            if current:
                tasks.append(current)
            parts = line.split("|")
            current = CommitTask(
                commit_hash=parts[0],
                message=parts[1],
                author=parts[2],
                date=parts[3] if len(parts) > 3 else ""
            )
        elif current and "\t" in line:
            # File stat line: additions deletions filename
            parts = line.split("\t")
            if len(parts) >= 3:
                path = parts[2]
                if should_skip_file(path):
                    continue
                try:
                    adds = int(parts[0]) if parts[0] != "-" else 0
                    dels = int(parts[1]) if parts[1] != "-" else 0
                except ValueError:
                    adds, dels = 0, 0
                current.files.append(FileChange(
                    path=path, additions=adds, deletions=dels
                ))

    if current:
        tasks.append(current)

    return tasks


def analyze_and_filter(tasks: list[CommitTask]) -> list[CommitTask]:
    """Compute metrics and filter tasks."""
    accepted = []

    for task in tasks:
        if not task.files:
            task.rejection_reason = "no source files"
            continue

        # Separate source and test files
        source_files = [f for f in task.files if not f.is_test]
        test_files = [f for f in task.files if f.is_test]

        task.source_files = len(source_files)
        task.test_files = len(test_files)
        task.total_additions = sum(f.additions for f in source_files)
        task.total_deletions = sum(f.deletions for f in source_files)

        # Distinct directories and modules
        dirs = set(f.top_dir for f in source_files)
        modules = set(f.module for f in source_files)
        task.distinct_dirs = len(dirs)
        task.distinct_modules = len(modules)

        # Check message
        task.is_interesting = is_interesting_message(task.message)

        # Apply filters
        if task.source_files < MIN_FILES_CHANGED:
            task.rejection_reason = f"too few source files ({task.source_files}<{MIN_FILES_CHANGED})"
            continue

        total_loc = task.total_additions + task.total_deletions
        if total_loc < MIN_LINES_CHANGED:
            task.rejection_reason = f"too few lines changed ({total_loc}<{MIN_LINES_CHANGED})"
            continue

        if task.distinct_dirs < MIN_DISTINCT_DIRS:
            task.rejection_reason = f"not cross-module ({task.distinct_dirs}<{MIN_DISTINCT_DIRS} dirs)"
            continue

        if task.source_files > MAX_FILES_CHANGED:
            task.rejection_reason = f"too many files ({task.source_files}>{MAX_FILES_CHANGED})"
            continue

        if not task.is_interesting:
            task.rejection_reason = "commit message not interesting"
            continue

        # Compute scores
        task.lcvs_score = compute_lcvs(task)

        if task.lcvs_score < 40:  # Relaxed threshold for mining
            task.rejection_reason = f"LCVS too low ({task.lcvs_score}<40)"
            continue

        accepted.append(task)

    return accepted


def get_commit_diff(repo_dir: str, commit_hash: str) -> str:
    """Get the actual diff for a commit."""
    cmd = ["git", "diff", f"{commit_hash}^..{commit_hash}", "--unified=3"]
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=repo_dir, encoding="utf-8",
        errors="replace"
    )
    if result.returncode == 0:
        return result.stdout[:50000]  # Cap at 50K chars
    return ""


def format_task_schema(
    task: CommitTask, repo_url: str, diff: str,
    repo_dir: str
) -> dict:
    """Format task into the dataset schema with real dependency analysis."""
    source_files = [f for f in task.files if not f.is_test]
    test_files = [f for f in task.files if f.is_test]
    changed_paths = [f.path for f in source_files]

    # Build REAL dependency graph from imports
    print(f"   🔗 Building dependency graph for {task.commit_hash[:8]}...")
    dep_edges, context_files = build_dependency_graph(
        repo_dir, task.commit_hash, changed_paths
    )

    # Estimate tokens from actual file sizes
    print(f"   📏 Estimating context size ({len(context_files)} files)...")
    token_estimate = estimate_tokens_from_files(
        repo_dir, context_files[:50], task.commit_hash  # Cap at 50 files
    )

    # Compute context ratio (required_context / modified_files)
    context_ratio = compute_context_ratio(len(context_files), task.source_files)

    # Group files by module
    modules = {}
    for f_path in context_files:
        parts = f_path.split("/")
        mod = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
        if mod not in modules:
            modules[mod] = []
        modules[mod].append(f_path)

    # Compute dependency depth (max traversal level reached)
    # Level 1 = direct imports, Level 2 = imports of imports
    direct_imports = set()
    for src_file in changed_paths:
        imports = extract_imports_from_file(repo_dir, src_file, task.commit_hash)
        for imp in imports:
            resolved = resolve_import_to_file(imp, src_file, context_files)
            if resolved and resolved != src_file:
                direct_imports.add(resolved)
    # If we have files beyond direct imports → depth is 2
    indirect_files = set(context_files) - set(changed_paths) - direct_imports
    dep_depth = 2 if indirect_files else (1 if direct_imports else 0)

    # Shortcut resistance: explicit, defensible thresholds
    is_shortcut_resistant = (
        context_ratio >= 2.0 and
        token_estimate >= 30000 and
        len(modules) >= 2
    )

    # Auto-detect long-context justification signals
    changed_dirs = set(f.top_dir for f in source_files)
    changed_modules_set = set(f.module for f in source_files)

    justification = {
        "cross_module": len(changed_modules_set) >= 2,
        "requires_build_system": any(
            "build" in p or "rollup" in p or "webpack" in p or "vite" in p
            for p in changed_paths
        ),
        "requires_config_propagation": any(
            "config" in p or "flag" in p or "feature" in p or "fork" in p
            for p in changed_paths
        ),
        "multi_layer_change": len(changed_dirs) >= 2,
        "deep_dependency_chain": dep_depth >= 2,
        "requires_test_awareness": task.test_files > 0,
    }

    return {
        "task_id": f"{repo_url.split('/')[-1]}-{task.commit_hash[:8]}",
        "repository_metadata": {
            "repo_url": repo_url,
            "base_commit": f"{task.commit_hash}^",
            "language": "auto-detected",
            "commit_date": task.date,
        },
        "task_description": {
            "issue_text": task.message,
            "task_category": categorize_task(task.message),
        },
        "context_requirements": {
            "modified_files": changed_paths,
            "required_context_files": context_files,
            "test_files": [f.path for f in test_files],
            "dependency_graph_edges": dep_edges,
            "distinct_modules": list(modules.keys()),
            "context_size_estimate_tokens": token_estimate,
        },
        "evaluation_parameters": {
            "gold_patch": diff,
            "difficulty_score": task.lcvs_score,
            "loc_changed": task.total_additions + task.total_deletions,
        },
        "long_context_metrics": {
            "lcvs_score": task.lcvs_score,
            "source_files_modified": task.source_files,
            "total_context_files": len(context_files),
            "test_files_count": task.test_files,
            "distinct_dirs": task.distinct_dirs,
            "distinct_modules": len(modules),
            "context_ratio": context_ratio,
            "dependency_depth_estimate": dep_depth,
            "dependency_edges_count": len(dep_edges),
            "total_additions": task.total_additions,
            "total_deletions": task.total_deletions,
        },
        "shortcut_resistance": {
            "is_resistant": is_shortcut_resistant,
            "criteria": {
                "context_ratio_threshold": "≥ 2.0",
                "context_ratio_actual": context_ratio,
                "token_threshold": "≥ 30,000",
                "token_actual": token_estimate,
                "module_threshold": "≥ 2",
                "module_actual": len(modules),
            },
        },
        "long_context_justification": justification,
        "failure_modes_classification": {
            "cascading_breakage_dirs": list(set(f.top_dir for f in source_files)),
        },
    }


def categorize_task(message: str) -> str:
    """Categorize task based on commit message."""
    lower = message.lower()
    if any(w in lower for w in ["fix", "bug", "error", "crash", "issue"]):
        return "bug_investigation"
    if any(w in lower for w in ["refactor", "redesign", "rewrite", "clean"]):
        return "cross_file_refactoring"
    if any(w in lower for w in ["feature", "implement", "add", "support"]):
        return "feature_implementation"
    if any(w in lower for w in ["test", "spec", "coverage"]):
        return "integration_testing"
    return "architectural_understanding"


def main():
    parser = argparse.ArgumentParser(
        description="Extract long-context evaluation tasks from git history"
    )
    parser.add_argument("repo_url", help="GitHub repository URL")
    parser.add_argument("--since", default="2024-01-01",
                       help="Only consider commits after this date")
    parser.add_argument("--limit", type=int, default=100,
                       help="Max commits to scan")
    parser.add_argument("--top", type=int, default=5,
                       help="Number of top tasks to extract")
    parser.add_argument("--output", default="sample_tasks.json",
                       help="Output file name")
    args = parser.parse_args()

    print("=" * 60)
    print("🔍 Long-Context Task Extraction Pipeline v2.0")
    print("=" * 60)
    print(f"   Repo: {args.repo_url}")
    print(f"   Since: {args.since}")
    print(f"   Scan limit: {args.limit}")
    print()

    # Clone
    repo_dir = clone_repo(args.repo_url)
    if not os.path.exists(repo_dir):
        print("❌ Failed to clone repository")
        sys.exit(1)

    # Parse git log
    print(f"\n📊 Parsing git log...")
    tasks = parse_git_log(repo_dir, args.since, args.limit)
    print(f"   Found {len(tasks)} commits")

    # Filter
    print(f"\n🔍 Analyzing and filtering...")
    accepted = analyze_and_filter(tasks)
    print(f"   Accepted: {len(accepted)} tasks")

    if not accepted:
        print("❌ No suitable tasks found. Try increasing --limit or adjusting --since")
        sys.exit(1)

    # Sort by LCVS score and take top N
    accepted.sort(key=lambda t: t.lcvs_score, reverse=True)
    top_tasks = accepted[:args.top]

    # Extract detailed data for top tasks
    print(f"\n🏗️ Extracting detailed data for top {len(top_tasks)} tasks...")
    schema_tasks = []
    for task in top_tasks:
        print(f"\n   📋 {task.commit_hash[:8]}: {task.message[:60]}...")
        diff = get_commit_diff(repo_dir, task.commit_hash)
        schema = format_task_schema(task, args.repo_url, diff, repo_dir)
        schema_tasks.append(schema)

    # Build output
    output = {
        "metadata": {
            "pipeline": "long-context-eval-extraction",
            "version": "2.0.0",
            "source_repo": args.repo_url,
            "extraction_params": {
                "since": args.since,
                "commits_scanned": len(tasks),
                "candidates_found": len(accepted),
                "tasks_extracted": len(schema_tasks),
            },
            "features": [
                "import-based dependency graph (multi-language)",
                "2-level context expansion (transitive imports)",
                "real token estimation (git show)",
                "shortcut resistance validation",
                "LCVS scoring (0-100)",
                "long-context justification signals",
            ],
        },
        "tasks": schema_tasks,
    }

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.output)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # Summary
    print(f"\n{'=' * 60}")
    print(f"✅ Extracted {len(schema_tasks)} tasks")
    print(f"📄 Output: {out_path}")
    print(f"{'=' * 60}")
    for s in schema_tasks:
        m = s["long_context_metrics"]
        sr = s["shortcut_resistance"]
        print(f"  📋 {s['task_id']}")
        print(f"     LCVS={m['lcvs_score']} | Files={m['source_files_modified']} | Context={m['total_context_files']} | Ratio={m['context_ratio']}")
        print(f"     Tokens≈{s['context_requirements']['context_size_estimate_tokens']:,} | Edges={m['dependency_edges_count']} | Shortcut-resistant={sr['is_resistant']}")


if __name__ == "__main__":
    main()
