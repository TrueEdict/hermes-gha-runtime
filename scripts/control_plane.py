#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "control-state"
OUTBOX = ROOT / "control-outbox"
RECEIPTS = ROOT / "control-receipts"

STATES = [
    "CREATED",
    "PACKAGED",
    "SUBMITTED",
    "PROVIDER_CONFIRMED",
    "RUNNING",
    "RESULT_RETURNED",
    "VERIFIED",
    "PROMOTED",
    "FAILED",
]

TERMINAL = {"PROMOTED", "FAILED"}
BACKENDS = {"github_actions", "colab", "kaggle", "lightning"}


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: pathlib.Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_json(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")


def validate_task(task: dict[str, Any]) -> None:
    required = ["task_id", "source_repo", "source_sha", "goal"]
    missing = [key for key in required if not task.get(key)]
    if missing:
        raise ValueError(f"missing required task fields: {missing}")
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", str(task["source_sha"])):
        raise ValueError("source_sha must be a git SHA")
    requested = task.get("requested_backend", "auto")
    if requested != "auto" and requested not in BACKENDS:
        raise ValueError(f"invalid requested_backend: {requested}")
    task.setdefault("required_capabilities", [])
    task.setdefault("verification", {})
    task["verification"].setdefault("exact_sha", True)
    task["verification"].setdefault("receipt_required", True)
    task.setdefault("retry", {"max_attempts": 2, "allow_reroute": True})


def route(task: dict[str, Any], calyx: dict[str, Any] | None = None) -> dict[str, Any]:
    requested = task.get("requested_backend", "auto")
    if requested != "auto":
        return {"backend": requested, "reason": "explicit_request", "confidence": 1.0}

    caps = {str(x).lower() for x in task.get("required_capabilities", [])}
    goal = str(task.get("goal", "")).lower()
    hint = (calyx or {}).get("recommended_backend")
    if hint in BACKENDS:
        return {"backend": hint, "reason": "calyx_recommendation", "confidence": float((calyx or {}).get("confidence", 0.8))}

    if {"persistent", "ssh", "long_lived", "special_environment"} & caps:
        return {"backend": "lightning", "reason": "persistent_or_special_environment", "confidence": 0.92}
    if {"gpu", "training", "large_gpu"} & caps or any(x in goal for x in ["train", "kaggle", "gpu"]):
        return {"backend": "kaggle", "reason": "gpu_or_training", "confidence": 0.90}
    if {"notebook", "interactive_python", "drive"} & caps or "colab" in goal:
        return {"backend": "colab", "reason": "notebook_or_interactive_python", "confidence": 0.88}
    return {"backend": "github_actions", "reason": "default_git_native_task", "confidence": 0.80}


def initial_state(task: dict[str, Any], route_decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "state": "CREATED",
        "backend": route_decision["backend"],
        "route": route_decision,
        "source_repo": task["source_repo"],
        "source_sha": task["source_sha"],
        "attempt": 0,
        "history": [{"state": "CREATED", "at": utc(), "evidence": {"task_hash": sha256_json(task)}}],
        "updated_at": utc(),
    }


def evidence_ok(current: str, target: str, evidence: dict[str, Any], task: dict[str, Any]) -> tuple[bool, str]:
    allowed = {
        "CREATED": {"PACKAGED", "FAILED"},
        "PACKAGED": {"SUBMITTED", "FAILED"},
        "SUBMITTED": {"PROVIDER_CONFIRMED", "FAILED"},
        "PROVIDER_CONFIRMED": {"RUNNING", "RESULT_RETURNED", "FAILED"},
        "RUNNING": {"RESULT_RETURNED", "FAILED"},
        "RESULT_RETURNED": {"VERIFIED", "FAILED"},
        "VERIFIED": {"PROMOTED", "FAILED"},
    }
    if target not in allowed.get(current, set()):
        return False, f"transition {current}->{target} not allowed"

    requirements = {
        "PACKAGED": ["package_sha256", "packaged_source_sha"],
        "SUBMITTED": ["submission_receipt", "nonce"],
        "PROVIDER_CONFIRMED": ["provider_job_id", "provider_status_raw"],
        "RUNNING": ["provider_job_id", "provider_status_raw"],
        "RESULT_RETURNED": ["result_uri", "result_sha256"],
        "VERIFIED": ["verified_source_sha", "validator_pass"],
        "PROMOTED": ["promotion_ref"],
        "FAILED": ["failure_reason"],
    }
    missing = [key for key in requirements[target] if not evidence.get(key)]
    if missing:
        return False, f"missing evidence for {target}: {missing}"

    if target == "PACKAGED" and task.get("verification", {}).get("exact_sha", True):
        if evidence.get("packaged_source_sha") != task["source_sha"]:
            return False, "packaged source SHA does not match task source_sha"
    if target == "VERIFIED" and task.get("verification", {}).get("exact_sha", True):
        if evidence.get("verified_source_sha") != task["source_sha"]:
            return False, "verified source SHA does not match task source_sha"
    if target in {"PROVIDER_CONFIRMED", "RUNNING"}:
        raw = str(evidence.get("provider_status_raw", "")).lower()
        if target == "RUNNING" and not any(x in raw for x in ["running", "active", "executing", "kernelworkerstatu.running"]):
            return False, "RUNNING requires provider evidence that actually says running/active/executing"
    return True, "ok"


def transition(task: dict[str, Any], state: dict[str, Any], target: str, evidence: dict[str, Any]) -> dict[str, Any]:
    ok, reason = evidence_ok(state["state"], target, evidence, task)
    if not ok:
        raise ValueError(reason)
    state = dict(state)
    state["state"] = target
    state["updated_at"] = utc()
    state.setdefault("history", []).append({"state": target, "at": utc(), "evidence": evidence})
    if target == "SUBMITTED":
        state["attempt"] = int(state.get("attempt", 0)) + 1
    return state


def compile_backend_request(task: dict[str, Any], state: dict[str, Any]) -> tuple[pathlib.Path, dict[str, Any]]:
    backend = state["backend"]
    nonce = safe_id(f"{task['task_id']}-a{int(state.get('attempt', 0)) + 1}")
    common = {
        "task_id": task["task_id"],
        "nonce": nonce,
        "source_repo": task["source_repo"],
        "source_sha": task["source_sha"],
        "goal": task["goal"],
        "issue": task.get("issue"),
        "worktree_ref": task.get("worktree_ref"),
        "created_at": utc(),
    }
    if backend == "kaggle":
        request = {**common, "backend": "kaggle", "slug": task.get("backend_config", {}).get("kaggle_slug"), "request_type": "verified_kaggle_transaction"}
        path = OUTBOX / "kaggle" / f"{nonce}.json"
    elif backend == "colab":
        request = {**common, "backend": "colab", "notebook": task.get("backend_config", {}).get("notebook"), "request_type": "colab_execution"}
        path = OUTBOX / "colab" / f"{nonce}.json"
    elif backend == "lightning":
        request = {**common, "backend": "lightning", "studio": task.get("backend_config", {}).get("studio"), "request_type": "persistent_execution"}
        path = OUTBOX / "lightning" / f"{nonce}.json"
    else:
        request = {**common, "backend": "github_actions", "request_type": "git_native_worker"}
        path = OUTBOX / "github_actions" / f"{nonce}.json"
    dump(path, request)
    return path, request


def package_worktree(task: dict[str, Any], checkout_root: pathlib.Path) -> dict[str, Any]:
    repo_dir = checkout_root / safe_id(task["task_id"])
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    subprocess.run(["git", "clone", "--no-checkout", f"https://github.com/{task['source_repo']}.git", str(repo_dir)], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "checkout", "--detach", task["source_sha"]], check=True)
    actual = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], text=True).strip()
    if actual != task["source_sha"]:
        raise RuntimeError(f"worktree SHA mismatch: {actual} != {task['source_sha']}")
    manifest = {
        "task_id": task["task_id"],
        "source_repo": task["source_repo"],
        "source_sha": task["source_sha"],
        "actual_sha": actual,
        "worktree_path": str(repo_dir),
        "created_at": utc(),
    }
    manifest["package_sha256"] = sha256_json(manifest)
    return manifest


def reconcile(task: dict[str, Any], state: dict[str, Any], evidence_dir: pathlib.Path) -> dict[str, Any]:
    evidence_files = sorted(evidence_dir.glob("*.json")) if evidence_dir.exists() else []
    evidence = [load(p) for p in evidence_files]
    by_type = {str(e.get("type")): e for e in evidence if e.get("type")}
    current = state["state"]

    candidates: list[tuple[str, dict[str, Any]]] = []
    if current == "PACKAGED" and "submission" in by_type:
        candidates.append(("SUBMITTED", by_type["submission"]))
    elif current == "SUBMITTED" and "provider_status" in by_type:
        raw = str(by_type["provider_status"].get("provider_status_raw", "")).lower()
        if any(x in raw for x in ["running", "active", "executing"]):
            candidates.append(("PROVIDER_CONFIRMED", by_type["provider_status"]))
        elif any(x in raw for x in ["complete", "success", "finished"]):
            candidates.append(("PROVIDER_CONFIRMED", by_type["provider_status"]))
    elif current == "PROVIDER_CONFIRMED" and "provider_status" in by_type:
        raw = str(by_type["provider_status"].get("provider_status_raw", "")).lower()
        if any(x in raw for x in ["running", "active", "executing"]):
            candidates.append(("RUNNING", by_type["provider_status"]))
        elif "result" in by_type:
            candidates.append(("RESULT_RETURNED", by_type["result"]))
    elif current == "RUNNING" and "result" in by_type:
        candidates.append(("RESULT_RETURNED", by_type["result"]))
    elif current == "RESULT_RETURNED" and "verification" in by_type:
        candidates.append(("VERIFIED", by_type["verification"]))
    elif current == "VERIFIED" and "promotion" in by_type:
        candidates.append(("PROMOTED", by_type["promotion"]))

    for target, ev in candidates:
        state = transition(task, state, target, ev)
        current = state["state"]
    return state


def cmd_init(args: argparse.Namespace) -> int:
    task = load(pathlib.Path(args.task))
    validate_task(task)
    calyx = load(pathlib.Path(args.calyx)) if args.calyx else None
    decision = route(task, calyx)
    state = initial_state(task, decision)
    task_path = STATE_DIR / safe_id(task["task_id"]) / "task.json"
    state_path = STATE_DIR / safe_id(task["task_id"]) / "state.json"
    dump(task_path, task)
    dump(state_path, state)
    print(json.dumps({"task": str(task_path), "state": str(state_path), "route": decision}, indent=2))
    return 0


def cmd_package(args: argparse.Namespace) -> int:
    base = STATE_DIR / safe_id(args.task_id)
    task, state = load(base / "task.json"), load(base / "state.json")
    manifest = package_worktree(task, pathlib.Path(args.checkout_root))
    dump(base / "worktree.json", manifest)
    state = transition(task, state, "PACKAGED", {"package_sha256": manifest["package_sha256"], "packaged_source_sha": manifest["actual_sha"], "worktree_path": manifest["worktree_path"]})
    dump(base / "state.json", state)
    print(json.dumps(manifest, indent=2))
    return 0


def cmd_dispatch(args: argparse.Namespace) -> int:
    base = STATE_DIR / safe_id(args.task_id)
    task, state = load(base / "task.json"), load(base / "state.json")
    path, request = compile_backend_request(task, state)
    print(json.dumps({"outbox_path": str(path), "request": request}, indent=2))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    base = STATE_DIR / safe_id(args.task_id)
    task, state = load(base / "task.json"), load(base / "state.json")
    evidence = load(pathlib.Path(args.evidence))
    state = transition(task, state, args.target, evidence)
    dump(base / "state.json", state)
    print(json.dumps(state, indent=2))
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    base = STATE_DIR / safe_id(args.task_id)
    task, state = load(base / "task.json"), load(base / "state.json")
    state = reconcile(task, state, pathlib.Path(args.evidence_dir))
    dump(base / "state.json", state)
    receipt = {"task_id": task["task_id"], "state": state["state"], "backend": state["backend"], "source_sha": task["source_sha"], "updated_at": utc(), "history_hash": sha256_json(state["history"])}
    dump(RECEIPTS / f"{safe_id(task['task_id'])}.json", receipt)
    print(json.dumps({"state": state, "receipt": receipt}, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init"); s.add_argument("task"); s.add_argument("--calyx"); s.set_defaults(func=cmd_init)
    s = sub.add_parser("package"); s.add_argument("task_id"); s.add_argument("--checkout-root", default="/tmp/control-worktrees"); s.set_defaults(func=cmd_package)
    s = sub.add_parser("dispatch"); s.add_argument("task_id"); s.set_defaults(func=cmd_dispatch)
    s = sub.add_parser("apply"); s.add_argument("task_id"); s.add_argument("target", choices=STATES); s.add_argument("evidence"); s.set_defaults(func=cmd_apply)
    s = sub.add_parser("reconcile"); s.add_argument("task_id"); s.add_argument("--evidence-dir", required=True); s.set_defaults(func=cmd_reconcile)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
