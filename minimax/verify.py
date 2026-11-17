"""Run the demonstration from minimax/ with a fresh output directory."""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def put(path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()


def module(run, action, *extra):
    cmd = [sys.executable, str(ROOT / "context.py"), action, "--project", str(run / "project"),
           "--task", str(ROOT / "task.json"), "--state", str(run / "state"), *map(str, extra)]
    return json.loads(subprocess.check_output(cmd, encoding="utf-8"))


def tests(run, phase, suite):
    loans = run / "project/loans"
    args = ["-B", "-m", "unittest", "-v", "test_catalog." + suite]
    proc = subprocess.run([sys.executable, *args], cwd=loans, capture_output=True, encoding="utf-8",
                          env={**os.environ, "PYTHONUTF8": "1"})
    log = {"command": "python " + " ".join(args), "cwd": "project/loans", "exit": proc.returncode,
           "source_sha256_lf": sha(loans / "catalog.py"), "tests_sha256_lf": sha(loans / "test_catalog.py"),
           "output": proc.stdout + proc.stderr}
    put(run / f"{phase}-{suite}.json", log)
    expected = 1 if (phase, suite) == ("before", "Target") else 0
    if log["exit"] != expected or (expected and not all(x in log["output"] for x in
            ("AssertionError", "test_due_today_is_still_on_time", "failures=1"))):
        raise RuntimeError(log)
    return {k: log[k] for k in ("command", "exit", "source_sha256_lf", "tests_sha256_lf")}


def checkpoint(run, done, todo, checks):
    put(run / "checkpoint.json", dict(done=done, todo=todo, checks=checks))
    saved = module(run, "save", "--checkpoint", run / "checkpoint.json")["progress"]
    put(run / f"saved-{saved['position']}.json", saved)
    restored = module(run, "context")
    assert saved == restored["progress"] and saved["checks"] == checks
    return restored


def prepare(run):
    run.mkdir(parents=True, exist_ok=False)
    shutil.copytree(ROOT / "sample", run / "project")
    context = module(run, "import")
    expected = {"loans/catalog.py", "loans/spec.md", "loans/test_catalog.py"}
    assert {s["path"] for s in context["sources"]} == expected
    assert all(s["sha256"] == sha(run / "project" / s["path"]) for s in context["sources"])
    assert context["progress"]["position"] == 0
    put(run / "initial.json", context)
    checks = [tests(run, "before", suite) for suite in ("Target", "Regression")]
    restored = checkpoint(run, ["baseline tested"], ["fix overdue boundary"], checks)
    assert restored["progress"]["position"] == 1
    put(run / "context.json", restored)


def finish(run):
    loans = run / "project/loans"
    before = (ROOT / "sample/loans/catalog.py").read_text(encoding="utf-8")
    after = (ROOT / "solution/catalog.py").read_text(encoding="utf-8")
    reply = json.loads((ROOT / "evidence/minimax/reply.json").read_text(encoding="utf-8"))
    assert after.strip() == reply["text"].strip()
    assert (loans / "catalog.py").read_text(encoding="utf-8") == before
    (run / "change.patch").write_text("".join(difflib.unified_diff(
        before.splitlines(True), after.splitlines(True), "a/loans/catalog.py", "b/loans/catalog.py")),
        encoding="utf-8", newline="\n")
    shutil.copyfile(ROOT / "solution/catalog.py", loans / "catalog.py")
    module(run, "import")
    checks = [tests(run, "after", suite) for suite in ("Target", "Regression")]
    baseline = json.loads((run / "context.json").read_text(encoding="utf-8"))["progress"]["checks"]
    assert all(a["tests_sha256_lf"] == b["tests_sha256_lf"] for a, b in zip(checks, baseline, strict=True))
    ctx = checkpoint(run, ["baseline tested", "boundary fixed", "regression tested"], [], baseline + checks)
    assert ctx["progress"]["position"] == 2
    put(run / "final.json", ctx)
    spec = loans / "spec.md"
    addition = "\nLOAN-2 clarification: the business date uses the circulation desk calendar.\n"
    with spec.open("a", encoding="utf-8") as f:
        f.write(addition)
    module(run, "import")
    updated = module(run, "context")
    row = next(s for s in updated["sources"] if s["path"] == "loans/spec.md")
    assert addition in row["content"] and row["sha256"] == sha(spec)
    assert updated["progress"] == ctx["progress"]
    put(run / "reimport.json", updated)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["prepare", "finish", "all"])
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    run = args.run.resolve()
    if args.phase in ("prepare", "all"):
        prepare(run)
    if args.phase in ("finish", "all"):
        finish(run)
    print(f"{args.phase}: PASS")
