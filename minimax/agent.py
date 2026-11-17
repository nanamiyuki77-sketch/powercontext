"""Ask MiniMax for a single file; save the proposal for human inspection."""
import json
from pathlib import Path
import shutil
import subprocess
import sys


def propose(context, output):
    messages = [{"role": "user", "content":
                 "Implement the task using these retrieved sources and previous checks. "
                 "Return only the complete catalog.py, no Markdown. Make the smallest edit.\n"
                 + json.dumps(context, ensure_ascii=False)}]
    output.mkdir(parents=True, exist_ok=True)
    request = output / "messages.json"
    request.write_text(json.dumps(messages, indent=2) + "\n", encoding="utf-8")
    model = "MiniMax-M3.1-Flash"
    mmx = shutil.which("mmx.cmd") or shutil.which("mmx")
    if not mmx:
        raise RuntimeError("Install and authenticate mmx-cli first")
    proc = subprocess.run([mmx, "text", "chat", "--model", model, "--messages-file", str(request),
                           "--output", "json"], capture_output=True, encoding="utf-8", timeout=180)
    if proc.returncode:
        raise RuntimeError(f"MiniMax CLI failed (exit {proc.returncode}). Check mmx quota and network.")
    reply = json.loads(proc.stdout)
    answer = "".join(b["text"] for b in reply["content"] if b["type"] == "text")
    evidence = {k: reply.get(k) for k in ("id", "model", "usage", "stop_reason")}
    evidence.update(command="mmx text chat --model " + model + " --messages-file messages.json --output json",
                    cli=subprocess.check_output([mmx, "--version"], text=True).strip(), text=answer)
    (output / "reply.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    source = answer.strip()
    if source.startswith("```") and source.endswith("```"):
        source = "\n".join(source.splitlines()[1:-1])
    (output / "catalog.py").write_text(source + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    propose(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")), Path(sys.argv[2]))
