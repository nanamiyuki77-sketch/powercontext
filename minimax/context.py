"""Run from minimax/. Only the dedicated sdd_sources table is managed here."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re

import pymysql
from powercontext.builtin.persistence.sources import SourceRepository
from powercontext.builtin.persistence.sqlite import SQLiteConfig, SQLiteProfile
from powercontext.builtin.persistence.tables import SHARED_TABLES
from powercontext.builtin.sources import CONTENT_SOURCE_ADAPTER, ContentSource
from powercontext.sources import SourceMaterialization


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def materials(project, run, question, refresh):
    db = pymysql.connect(host=os.getenv("SDD_HOST", "127.0.0.1"),
                         port=int(os.getenv("SDD_PORT", "2881")),
                         user=os.getenv("SDD_USER", "root"), password=os.getenv("SDD_PASSWORD", ""),
                         database=os.getenv("SDD_DATABASE", "test"), charset="utf8mb4")
    with db, db.cursor() as cur:
        cur.execute("SELECT VERSION(), @@version_comment")
        version = cur.fetchone()
        if not any(tag in " ".join(version).lower() for tag in ("seekdb", "oceanbase")):
            raise RuntimeError("Connect to seekdb or OceanBase")
        if refresh:
            docs = []
            for path in sorted(project.glob("*/*")):
                if path.suffix in (".md", ".py"):
                    content = path.read_text(encoding="utf-8")
                    docs.append((run, path.parent.name, path.relative_to(project).as_posix(),
                                 digest(content), content))
            if not docs:
                raise ValueError("No project sources found")
            cur.execute("""CREATE TABLE IF NOT EXISTS sdd_sources (
                run_id VARCHAR(64), topic VARCHAR(64), path VARCHAR(255), sha CHAR(64),
                body MEDIUMTEXT NOT NULL, PRIMARY KEY(run_id, path))""")
            cur.execute("DELETE FROM sdd_sources WHERE run_id=%s", (run,))
            cur.executemany("INSERT INTO sdd_sources VALUES (%s,%s,%s,%s,%s)", docs)
            db.commit()
        terms = sorted(set(re.findall(r"[a-z0-9_]+", question.lower())))
        if not terms:
            raise ValueError("Task must include a module name")
        sql = "SELECT path, sha, body FROM sdd_sources WHERE run_id=%s AND topic IN %s ORDER BY path"
        cur.execute(sql, (run, tuple(terms)))
        return {"database": version, "sql": sql, "parameters": [run, terms],
                "sources": [dict(path=p, sha256=s, content=b) for p, s, b in cur.fetchall()]}


async def journal(directory, scope, checkpoint):
    config = SQLiteConfig(url="sqlite+aiosqlite:///" + (directory / "progress.db").resolve().as_posix())
    repo = SourceRepository((CONTENT_SOURCE_ADAPTER,))
    async with SQLiteProfile.open(config, tables=SHARED_TABLES) as profile:
        async with profile.database.transaction() as connection:
            if checkpoint is not None:
                content = json.dumps(checkpoint, sort_keys=True)
                source = ContentSource(name=digest(content), materialization=SourceMaterialization.CAPTURED,
                                       content=content)
                await repo.add(connection, scope, source)
            records = await repo.list(connection, scope)
            if records:
                record = records[-1]
                return {"position": record.journal_position, "ref": record.ref.model_dump(mode="json"),
                        **json.loads(record.value.content)}
            return {"position": 0, "done": [], "todo": ["inspect sources"], "checks": []}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["import", "context", "save"])
    parser.add_argument("--project", type=Path, default=Path("sample"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--task", type=Path, default=Path("task.json"))
    parser.add_argument("--checkpoint", type=Path)
    args = parser.parse_args()
    task = json.loads(args.task.read_text(encoding="utf-8"))
    args.state.mkdir(parents=True, exist_ok=True)
    run = digest(str(args.state.resolve()))
    checkpoint = None
    if args.command == "save":
        if not args.checkpoint:
            parser.error("save requires --checkpoint")
        checkpoint = json.loads(args.checkpoint.read_text(encoding="utf-8"))
        if not all(isinstance(checkpoint.get(k), list) for k in ("done", "todo", "checks")):
            parser.error("checkpoint requires done, todo and checks lists")
    result = {"task": task}
    if args.command != "save":
        result.update(materials(args.project, run, task["question"], args.command == "import"))
    result["progress"] = asyncio.run(journal(args.state, task["id"], checkpoint))
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
