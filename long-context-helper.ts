/**
 * Long-Context Evaluation Helper for Gemini CLI
 * GSoC 2026 — Issue #23316
 *
 * Extends the eval test framework to support external repository
 * checkout and long-context task evaluation.
 *
 * Designed to integrate with `evals/test-helper.ts` evalTest() pattern.
 */

import { execSync, type ExecSyncOptions } from "child_process";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";

// ─── Types ─────────────────────────────────────────────────────────

interface LongContextTask {
  task_id: string;
  repository_metadata: {
    repo_url: string;
    base_commit: string;
    language: string;
    commit_date: string;
  };
  task_description: {
    issue_text: string;
    task_category: string;
  };
  context_requirements: {
    modified_files: string[];
    required_context_files: string[];
    test_files: string[];
    dependency_graph_edges: string[];
    distinct_modules: string[];
    context_size_estimate_tokens: number;
  };
  evaluation_parameters: {
    gold_patch: string;
    difficulty_score: number;
    loc_changed: number;
  };
  long_context_metrics: {
    lcvs_score: number;
    source_files_modified: number;
    total_context_files: number;
    test_files_count: number;
    distinct_dirs: number;
    distinct_modules: number;
    context_ratio: number;
    dependency_depth_estimate: number;
    dependency_edges_count: number;
  };
  shortcut_resistance: {
    is_resistant: boolean;
    criteria: Record<string, unknown>;
  };
  long_context_justification: Record<string, boolean>;
}

/**
 * Failure mode classification for long-context tasks.
 *
 * Categories:
 *   - context_selection: Agent failed to identify relevant files
 *   - cross_module_reasoning: Agent fixed one module but missed cascading effects
 *   - partial_fix: Correct approach but incomplete implementation
 *   - cascading_breakage: Fix introduced new failures in other modules
 *   - test_awareness: Agent missed test files that need updating
 *   - architectural_misunderstanding: Fundamentally wrong approach
 */
type FailureMode =
  | "context_selection"
  | "cross_module_reasoning"
  | "partial_fix"
  | "cascading_breakage"
  | "test_awareness"
  | "architectural_misunderstanding";

interface EvalResult {
  task_id: string;
  passed: boolean;
  failure_mode?: FailureMode;
  files_modified: string[];
  files_expected: string[];
  overlap_ratio: number;
  context_utilization: number;
  test_pass_rate: number;
  execution_time_ms: number;
  error?: string;
}

// ─── Git Operations ────────────────────────────────────────────────

const CACHE_DIR = path.join(os.tmpdir(), "gemini-long-context-eval");

function ensureCacheDir(): void {
  if (!fs.existsSync(CACHE_DIR)) {
    fs.mkdirSync(CACHE_DIR, { recursive: true });
  }
}

function cloneOrCacheRepo(repoUrl: string): string {
  ensureCacheDir();
  const repoName = repoUrl.split("/").pop() || "repo";
  const repoDir = path.join(CACHE_DIR, repoName);

  if (fs.existsSync(repoDir)) {
    console.log(`  💾 Using cached: ${repoDir}`);
    return repoDir;
  }

  console.log(`  📥 Cloning ${repoUrl}...`);
  const opts: ExecSyncOptions = { stdio: "pipe", timeout: 300000 };
  execSync(`git clone --depth=500 ${repoUrl} ${repoDir}`, opts);
  return repoDir;
}

function checkoutCommit(repoDir: string, commitRef: string): boolean {
  const opts: ExecSyncOptions = { cwd: repoDir, stdio: "pipe" };
  try {
    execSync("git clean -fdx", opts);
    execSync("git checkout .", opts);

    // Resolve commit reference (handles ^ syntax)
    let resolved: string;
    try {
      resolved = execSync(`git rev-parse ${commitRef}`, opts)
        .toString()
        .trim();
    } catch {
      resolved = commitRef;
    }

    execSync(`git checkout ${resolved} --force`, opts);
    return true;
  } catch {
    // Try deepening
    try {
      execSync("git fetch --deepen=2000", opts);
      const resolved = execSync(`git rev-parse ${commitRef}`, opts)
        .toString()
        .trim();
      execSync(`git checkout ${resolved} --force`, opts);
      return true;
    } catch {
      return false;
    }
  }
}

// ─── Evaluation Logic ──────────────────────────────────────────────

function classifyFailure(
  expectedFiles: string[],
  actualFiles: string[],
  testsPassed: boolean,
): FailureMode {
  const expectedSet = new Set(expectedFiles);

  // Check if agent touched completely wrong files
  const overlap = actualFiles.filter((f) => expectedSet.has(f));
  const overlapRatio = expectedFiles.length > 0
    ? overlap.length / expectedFiles.length
    : 0;

  if (overlapRatio < 0.3) {
    return "context_selection";
  }

  // Check for cascading breakage
  const extraFiles = actualFiles.filter((f) => !expectedSet.has(f));
  if (extraFiles.length > expectedFiles.length) {
    return "cascading_breakage";
  }

  // Check cross-module issues
  const expectedDirs = new Set(expectedFiles.map((f) => f.split("/")[0]));
  const actualDirs = new Set(actualFiles.map((f) => f.split("/")[0]));
  const missingDirs = [...expectedDirs].filter((d) => !actualDirs.has(d));
  if (missingDirs.length > 0) {
    return "cross_module_reasoning";
  }

  // Partial fix
  if (overlapRatio < 1.0) {
    return "partial_fix";
  }

  // Test awareness
  if (!testsPassed) {
    return "test_awareness";
  }

  return "architectural_misunderstanding";
}

function computeContextUtilization(
  task: LongContextTask,
  agentFilesRead: string[],
): number {
  const required = new Set(task.context_requirements.required_context_files);
  const read = new Set(agentFilesRead);

  // How many of the required files did the agent actually read?
  const relevant = [...read].filter((f) => required.has(f));
  return required.size > 0 ? relevant.length / required.size : 0;
}

// ─── Main Eval Function ────────────────────────────────────────────

/**
 * Run a long-context evaluation task.
 *
 * This is designed to be called from the Gemini CLI eval framework:
 *
 * ```ts
 * import { longContextEvalTest } from './long-context-helper';
 *
 * longContextEvalTest('react-feature-flags', taskData, {
 *   timeout: 300000,
 *   model: 'gemini-2.5-pro',
 * });
 * ```
 */
export async function longContextEvalTest(
  taskId: string,
  task: LongContextTask,
  options: {
    timeout?: number;
    validateTests?: boolean;
  } = {},
): Promise<EvalResult> {
  const startTime = Date.now();
  const { timeout = 300000, validateTests = true } = options;

  console.log(`\n${"=".repeat(60)}`);
  console.log(`🧪 Long-Context Eval: ${taskId}`);
  console.log(`   Repo: ${task.repository_metadata.repo_url}`);
  console.log(`   Commit: ${task.repository_metadata.base_commit}`);
  console.log(`   Category: ${task.task_description.task_category}`);
  console.log(`   LCVS: ${task.long_context_metrics.lcvs_score}`);
  console.log(`   Context files: ${task.long_context_metrics.total_context_files}`);
  console.log(`   Shortcut resistant: ${task.shortcut_resistance.is_resistant}`);
  console.log(`${"=".repeat(60)}\n`);

  try {
    // Step 1: Clone and checkout
    const repoDir = cloneOrCacheRepo(task.repository_metadata.repo_url);
    if (!checkoutCommit(repoDir, task.repository_metadata.base_commit)) {
      return {
        task_id: taskId,
        passed: false,
        files_modified: [],
        files_expected: task.context_requirements.modified_files,
        overlap_ratio: 0,
        context_utilization: 0,
        test_pass_rate: 0,
        execution_time_ms: Date.now() - startTime,
        error: "Failed to checkout base commit",
      };
    }

    // Step 2: Apply gold patch as reference implementation
    // In production, this would invoke the Gemini CLI agent instead
    console.log("  📋 Task description:");
    console.log(`     "${task.task_description.issue_text}"`);

    const expectedFiles = task.context_requirements.modified_files;
    let agentFiles: string[] = [];

    // Step 3: Apply gold patch and capture modified files
    if (task.evaluation_parameters.gold_patch) {
      console.log("  🔧 Applying gold patch...");
      const patchFile = path.join(repoDir, ".eval_patch.diff");
      fs.writeFileSync(patchFile, task.evaluation_parameters.gold_patch);

      try {
        // Validate patch first (dry-run)
        execSync(`git apply --check ${patchFile}`, {
          cwd: repoDir,
          stdio: "pipe",
        });

        // Apply patch
        execSync(`git apply --verbose ${patchFile}`, {
          cwd: repoDir,
          stdio: "pipe",
        });

        // Capture actually modified files
        const diffOutput = execSync("git diff --name-only", {
          cwd: repoDir,
          encoding: "utf-8",
        });
        agentFiles = diffOutput
          .trim()
          .split("\n")
          .filter((f) => f.length > 0);
        console.log(`  ✅ Patch applied: ${agentFiles.length} files modified`);
      } catch (patchError) {
        console.log(`  ❌ Patch failed: ${String(patchError).slice(0, 200)}`);
      } finally {
        if (fs.existsSync(patchFile)) fs.unlinkSync(patchFile);
      }
    }

    const overlap = agentFiles.filter((f) => expectedFiles.includes(f));
    const overlapRatio =
      expectedFiles.length > 0 ? overlap.length / expectedFiles.length : 0;

    // Step 4: Run tests if available
    let testPassRate = 0;
    if (validateTests && task.context_requirements.test_files.length > 0) {
      console.log("  🧪 Running test validation...");
      try {
        // Auto-detect test command based on repo structure
        let testCmd = "npm test";
        if (fs.existsSync(path.join(repoDir, "go.mod"))) testCmd = "go test ./...";
        else if (fs.existsSync(path.join(repoDir, "Cargo.toml"))) testCmd = "cargo test";
        else if (fs.existsSync(path.join(repoDir, "pytest.ini"))) testCmd = "python -m pytest -q";

        execSync(testCmd, {
          cwd: repoDir,
          stdio: "pipe",
          timeout: timeout,
        });
        testPassRate = 1.0;
        console.log("  ✅ Tests pass");
      } catch {
        testPassRate = 0;
        console.log("  ❌ Tests fail");
      }
    }

    const passed = overlapRatio >= 0.8 && testPassRate >= 0.8;

    const result: EvalResult = {
      task_id: taskId,
      passed,
      files_modified: agentFiles,
      files_expected: expectedFiles,
      overlap_ratio: overlapRatio,
      context_utilization: computeContextUtilization(task, agentFiles),
      test_pass_rate: testPassRate,
      execution_time_ms: Date.now() - startTime,
    };

    if (!passed) {
      result.failure_mode = classifyFailure(
        expectedFiles,
        agentFiles,
        testPassRate >= 0.8,
      );
    }

    console.log(`\n📊 Result: ${passed ? "✅ PASS" : "❌ FAIL"}`);
    if (result.failure_mode) {
      console.log(`   Failure mode: ${result.failure_mode}`);
    }
    console.log(`   Overlap: ${(overlapRatio * 100).toFixed(1)}%`);
    console.log(`   Time: ${result.execution_time_ms}ms`);

    return result;
  } catch (error) {
    return {
      task_id: taskId,
      passed: false,
      files_modified: [],
      files_expected: task.context_requirements.modified_files,
      overlap_ratio: 0,
      context_utilization: 0,
      test_pass_rate: 0,
      execution_time_ms: Date.now() - startTime,
      error: String(error),
    };
  }
}
