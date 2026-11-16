#!/usr/bin/env python3
from pathlib import Path
from typing import Tuple
import subprocess

class GitAirgapSandbox:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()

    def _run_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=self.repo_root, capture_output=True, text=True, check=False)

    def ensure_clean_baseline(self) -> None:
        if self._run_git("status", "--porcelain").returncode != 0:
            raise RuntimeError(f"Not a valid git repository: {self.repo_root}")

    def apply_slice_mutation(self, rel_path: str, start_line: int, end_line: int, replacement_code: str) -> None:
        target = (self.repo_root / rel_path).resolve()
        if not str(target).startswith(str(self.repo_root)):
            raise ValueError(f"Path traversal blocked: {rel_path}")
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        new_lines = [ln if ln.endswith("\n") else f"{ln}\n" for ln in replacement_code.splitlines()]
        target.write_text("".join(lines[: start_line - 1] + new_lines + lines[end_line:]), encoding="utf-8")

    def run_verification(self, test_cmd: list[str], timeout_sec: int = 45) -> Tuple[bool, str]:
        try:
            proc = subprocess.run(test_cmd, cwd=self.repo_root, capture_output=True, text=True, timeout=timeout_sec, check=False)
            combined = (proc.stdout + "\n" + proc.stderr).strip()
            return (proc.returncode == 0), (combined[-380:] if len(combined) > 380 else combined)
        except subprocess.TimeoutExpired:
            return False, f"Verification timed out after {timeout_sec}s."

    def commit_hop(self, node_id: int, band: int, rel_path: str, scar: str) -> str:
        self._run_git("add", rel_path)
        res = self._run_git("commit", "-m", f"flow(node-{node_id}/3200, band-{band}): {scar[:72]}")
        if res.returncode != 0:
            return "NO_DIFF_COMMITTED"
        return self._run_git("rev-parse", "--short", "HEAD").stdout.strip()

    def rollback_path(self, rel_path: str) -> None:
        self._run_git("checkout", "--", rel_path)
