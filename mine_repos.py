#!/usr/bin/env python3
"""
Repository Mining Pipeline for Long-Context Evaluation Dataset
GSoC 2026 — Issue #23316

Scores and ranks curated repositories for long-context coding evaluation.
Uses GitHub API for enrichment, falls back to metadata for offline scoring.

Usage:
    python mine_repos.py                          # Offline mode (no API needed)
    GITHUB_TOKEN=ghp_xxx python mine_repos.py     # Enriched mode (API data)
"""

import json
import math
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

GITHUB_API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")

# ─── Curated Repository List ───────────────────────────────────────────
# Selected for: high LOC, multi-language, active development, good test suites
CURATED_REPOS = [
    # Repo                     Primary Lang   Est. LOC   Est. Forks  Description
    ("vercel/next.js",         "TypeScript",  800000,    28000,      "React framework with SSR, routing, complex build pipeline"),
    ("microsoft/TypeScript",   "TypeScript",  1100000,   12500,      "Type system compiler with deep inference and type-checking"),
    ("facebook/react",         "JavaScript",  350000,    45000,      "UI library with reconciler, fiber architecture, hooks"),
    ("kubernetes/kubernetes",  "Go",          3200000,   41000,      "Container orchestration with complex scheduler and API server"),
    ("django/django",          "Python",      500000,    32000,      "Web framework with ORM, middleware, authentication"),
    ("pytorch/pytorch",        "Python",      2500000,   24000,      "ML framework with autograd, distributed training, CUDA"),
    ("nodejs/node",            "JavaScript",  2000000,   26000,      "JS runtime with streams, crypto, async I/O"),
    ("golang/go",              "Go",          1800000,   18000,      "Language compiler and standard library"),
    ("rust-lang/rust",         "Rust",        2200000,   12000,      "Systems language compiler with borrow checker"),
    ("apache/kafka",           "Java",        1000000,   14500,      "Distributed message queue with replication and partitioning"),
    ("grafana/grafana",        "Go",          1200000,   11000,      "Observability platform with plugins and dashboards"),
    ("elastic/elasticsearch",  "Java",        2000000,   14000,      "Search engine with indexing, sharding, aggregation"),
    ("angular/angular",        "TypeScript",  600000,    25000,      "Frontend framework with dependency injection, change detection"),
    ("sveltejs/svelte",        "JavaScript",  120000,    4000,       "Compiler-based UI framework"),
    ("vitejs/vite",            "TypeScript",  200000,    8000,       "Build tool with HMR and plugin system"),
    ("nestjs/nest",            "TypeScript",  150000,    8500,       "Node.js framework with decorators and dependency injection"),
    ("prisma/prisma",          "TypeScript",  300000,    5500,       "ORM with schema-driven development and migrations"),
    ("apache/spark",           "Scala",       1500000,   19000,      "Distributed computing with SQL, streaming, ML"),
    ("flutter/flutter",        "Dart",        1000000,   27000,      "Cross-platform UI toolkit"),
    ("tensorflow/tensorflow",  "Python",      3000000,   85000,      "ML framework with computation graphs and serving"),
]


def api_get(url: str) -> dict:
    """Make GitHub API request with optional authentication."""
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "gsoc-long-context-eval"
    }
    if TOKEN:
        headers["Authorization"] = f"token {TOKEN}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print(f"  ⏳ Rate limited, using fallback data")
            return {}
        return {}
    except Exception:
        return {}


def enrich_repo(owner: str, name: str) -> dict:
    """Fetch real-time metadata from GitHub API."""
    url = f"{GITHUB_API}/repos/{owner}/{name}"
    data = api_get(url)
    if not data:
        return {}

    # Get language breakdown
    lang_url = f"{GITHUB_API}/repos/{owner}/{name}/languages"
    languages = api_get(lang_url)
    time.sleep(1)

    # Check CI
    ci_url = f"{GITHUB_API}/repos/{owner}/{name}/contents/.github/workflows"
    ci_data = api_get(ci_url)
    has_ci = isinstance(ci_data, list) and len(ci_data) > 0
    time.sleep(1)

    return {
        "stars": data.get("stargazers_count", 0),
        "forks": data.get("forks_count", 0),
        "size_kb": data.get("size", 0),
        "open_issues": data.get("open_issues_count", 0),
        "updated_at": data.get("updated_at", ""),
        "license": (data.get("license") or {}).get("spdx_id", "Unknown"),
        "languages": languages if isinstance(languages, dict) else {},
        "has_ci": has_ci,
        "default_branch": data.get("default_branch", "main"),
    }


def compute_score(
    stars: int, forks: int, est_loc: int,
    lang_count: int, has_ci: bool
) -> float:
    """
    Compute suitability score for long-context evaluation.

    Formula:
        0.25 × norm(community_size) +
        0.20 × norm(language_diversity) +
        0.20 × norm(test_infrastructure) +
        0.20 × norm(activity_signal) +
        0.15 × norm(codebase_size)
    """
    community = min(forks / 20000, 1.0)
    lang_diversity = min(lang_count / 5, 1.0)
    test_infra = 1.0 if has_ci else 0.0
    activity = min(math.log10(max(stars, 1)) / 5.0, 1.0)
    size = min(math.log10(max(est_loc, 1)) / 7.0, 1.0)

    return round(
        0.25 * community +
        0.20 * lang_diversity +
        0.20 * test_infra +
        0.20 * activity +
        0.15 * size, 4
    )


def main():
    print("=" * 60)
    print("🔍 Long-Context Eval — Repository Mining Pipeline")
    print("=" * 60)

    use_api = bool(TOKEN)
    if use_api:
        print("✅ GitHub token detected — enriching with live API data")
    else:
        print("📦 Offline mode — using curated metadata")
        print("   (Set GITHUB_TOKEN for live enrichment)")
    print()

    results = []

    for repo_name, primary_lang, est_loc, est_forks, desc in CURATED_REPOS:
        owner, name = repo_name.split("/")
        print(f"📦 {repo_name} ({primary_lang})")

        # Start with curated defaults
        stars = est_forks * 3  # rough estimate
        forks = est_forks
        size_kb = est_loc * 10  # rough
        languages = {primary_lang: est_loc * 100}
        has_ci = True  # All curated repos have CI
        license_id = "Unknown"
        updated_at = datetime.now().isoformat()

        # Optionally enrich with live data
        if use_api:
            live = enrich_repo(owner, name)
            if live:
                stars = live.get("stars", stars)
                forks = live.get("forks", forks)
                size_kb = live.get("size_kb", size_kb)
                languages = live.get("languages", languages)
                has_ci = live.get("has_ci", has_ci)
                license_id = live.get("license", license_id)
                updated_at = live.get("updated_at", updated_at)
                print(f"   ✅ Enriched: ⭐{stars} 🍴{forks} | {len(languages)} langs")
                time.sleep(1)
            else:
                print(f"   ⚠️  API failed, using curated data")
        else:
            print(f"   📊 Est. LOC: {est_loc:,} | Forks: {est_forks:,}")

        lang_count = len(languages) if isinstance(languages, dict) else 1
        # In offline mode, estimate diversity based on repo characteristics
        if not use_api and lang_count == 1:
            # Most large repos have multiple languages (config, tests, docs, infra)
            lang_count = max(2, min(5, est_loc // 200000))
        score = compute_score(stars, forks, est_loc, lang_count, has_ci)

        results.append({
            "rank": 0,  # Will be set after sorting
            "name": repo_name,
            "url": f"https://github.com/{repo_name}",
            "primary_language": primary_lang,
            "stars": stars,
            "forks": forks,
            "estimated_loc": est_loc,
            "size_kb": size_kb,
            "language_count": lang_count,
            "languages": languages if use_api else {primary_lang: est_loc},
            "has_ci": has_ci,
            "license": license_id,
            "description": desc,
            "score": score,
            "suitability": (
                "excellent" if score >= 0.75 else
                "good" if score >= 0.60 else
                "moderate" if score >= 0.45 else
                "low"
            ),
        })

    # Sort by score
    results.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1

    # Build output
    output = {
        "metadata": {
            "pipeline": "long-context-eval-repo-mining",
            "version": "1.0.0",
            "generated_at": datetime.now().isoformat(),
            "mode": "api-enriched" if use_api else "curated-offline",
            "criteria": {
                "min_loc": 100000,
                "diversity": "Multi-language, multi-domain",
                "requirements": [
                    "Active development (recent commits)",
                    "Comprehensive test suite with CI",
                    "Permissive or copyleft license",
                    "Rich cross-module dependency structure"
                ],
            },
            "scoring_formula": "0.25*community + 0.20*lang_diversity + 0.20*test_infra + 0.20*activity + 0.15*size",
            "total_candidates": len(results),
        },
        "ranked_repos": results,
    }

    # Write output
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ranked_repos.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # Print summary
    print()
    print("=" * 60)
    print(f"✅ Scored {len(results)} repositories")
    print(f"📄 Output: {out_path}")
    print("=" * 60)
    print()
    print("🏆 Rankings:")
    print("-" * 60)
    for r in results:
        tier = {"excellent": "🟢", "good": "🔵", "moderate": "🟡", "low": "🔴"}
        icon = tier.get(r["suitability"], "⚪")
        print(f"  #{r['rank']:2d} {icon} {r['name']:<35s} Score={r['score']:.4f}  LOC≈{r['estimated_loc']:>10,}")
    print()


if __name__ == "__main__":
    main()
