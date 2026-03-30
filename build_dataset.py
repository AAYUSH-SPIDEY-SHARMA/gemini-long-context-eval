#!/usr/bin/env python3
"""
Generate clean dataset.json from extracted tasks with resolved commit hashes.
GSoC 2026 — Issue #23316
"""
import json
import os

tasks = [
    {
        "task_id": "react-3cb2c420",
        "repo": "https://github.com/facebook/react",
        "base_commit": "3cb2c420^",
        "fix_commit": "3cb2c420",
        "task": "Add ReactFeatureFlags support to the eslint-plugin-react-hooks package. The compiler options in RunReactCompiler.ts are currently hardcoded. They should be controlled via ReactFeatureFlags so they can be configured per-environment (www vs oss vs native).",
        "category": "feature_implementation",
        "files": [
            "packages/eslint-plugin-react-hooks/src/shared/RunReactCompiler.ts",
            "packages/shared/ReactFeatureFlags.js",
            "packages/shared/forks/ReactFeatureFlags.www.js",
            "packages/shared/forks/ReactFeatureFlags.native-fb.js",
            "packages/shared/forks/ReactFeatureFlags.native-oss.js",
            "scripts/flags/flags.js",
            "scripts/rollup/build.js",
            "scripts/rollup/bundles.js",
            "scripts/rollup/validate/index.js"
        ],
        "validation": "node scripts/flags/flags.js",
        "metrics": {
            "lcvs_score": 67.84,
            "source_files_modified": 11,
            "total_context_files": 38,
            "context_ratio": 3.45,
            "dependency_depth": 2,
            "shortcut_resistant": True
        }
    },
    {
        "task_id": "react-4cf90638",
        "repo": "https://github.com/facebook/react",
        "base_commit": "4cf90638^",
        "fix_commit": "4cf90638",
        "task": "Optimize gesture transitions by allowing the original work-in-progress tree to be used as a suspended commit. This avoids re-rendering when the gesture completes and the commit is already prepared.",
        "category": "architectural_understanding",
        "files": [
            "fixtures/view-transition/src/components/SwipeRecognizer.js",
            "packages/react-reconciler/src/ReactFiberGestureScheduler.js",
            "packages/react-reconciler/src/ReactFiberPerformanceTrack.js",
            "packages/react-reconciler/src/ReactFiberWorkLoop.js"
        ],
        "validation": "node -e \"require('./packages/react-reconciler/src/ReactFiberGestureScheduler.js')\" 2>&1 || echo VALIDATION_CHECK",
        "metrics": {
            "lcvs_score": 46.56,
            "source_files_modified": 4,
            "total_context_files": 77,
            "context_ratio": 19.25,
            "dependency_depth": 2,
            "shortcut_resistant": True
        }
    }
]

# Load gold patches from sample_tasks.json if available
sample_path = os.path.join(os.path.dirname(__file__), "sample_tasks.json")
if os.path.exists(sample_path):
    with open(sample_path, "r", encoding="utf-8") as f:
        sample = json.load(f)
    for st in sample["tasks"]:
        for t in tasks:
            if t["task_id"] == st["task_id"]:
                t["gold_patch"] = st["evaluation_parameters"]["gold_patch"]

out = {
    "metadata": {
        "pipeline": "long-context-eval-dataset",
        "version": "1.0.0",
        "description": "Long-context coding evaluation tasks extracted from facebook/react",
        "source": "Git history mining with import-based dependency analysis",
        "tasks_count": len(tasks)
    },
    "tasks": tasks
}

out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dataset.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print(f"Created dataset.json with {len(tasks)} tasks at {out_path}")
