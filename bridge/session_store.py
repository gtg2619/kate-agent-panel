"""Panel-owned session index and read-only import of local CLI session metadata."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid
from contextlib import contextmanager, closing
from datetime import datetime
from collections import deque


def canonical(path):
    return str(Path(path).expanduser().resolve())


def timestamp(value):
    if isinstance(value, (int, float)):
        return (value / 1000 if value > 10**11 else value) if math.isfinite(value) else 0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0


def modified(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def records(path, limit=128):
    """Bound reads: session discovery must not parse entire conversation logs."""
    try:
        with path.open("rb") as stream:
            for _ in range(limit):
                line = stream.readline(512 * 1024)
                if not line:
                    break
                if len(line) >= 512 * 1024:
                    break
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        yield item
                except (ValueError, UnicodeError):
                    continue
    except OSError:
        return


def conversation_message(message):
    """Configuration, notices and empty streamed placeholders are not conversations."""
    if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
        return False
    content = message.get("text", message.get("content"))
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(isinstance(part, dict) and (str(part.get("text", "")).strip()
                   or part.get("type") in ("image", "input_image", "image_url", "tool_use", "tool_call", "audio")) for part in content)
    return False


def log_activity(path, agent):
    """True/False for observed activity/empty logs, None for unreadable or bounded scans."""
    try:
        with Path(path).open("rb") as stream:
            size = 0
            for _ in range(128):
                line = stream.readline(512 * 1024)
                if not line:
                    return False
                size += len(line)
                if len(line) >= 512 * 1024 or size > 4 * 1024 * 1024:
                    return None
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                message = event.get("message", {})
                if agent == "codex":
                    payload = event.get("payload", {})
                    if not isinstance(payload, dict):
                        continue
                    if event.get("type") == "event_msg" and payload.get("type") == "user_message" and payload.get("message"):
                        return True
                    message = payload if event.get("type") == "response_item" else {}
                    text = content_text(message.get("content"))
                    if message.get("role") == "user" and text.lstrip().startswith(("<environment_context>", "# AGENTS.md instructions")):
                        continue
                if conversation_message(message):
                    return True
            return None
    except OSError:
        return None


def history_activity(history):
    # Old, truncated histories may retain only tool work or a completed turn.
    return isinstance(history, list) and any(conversation_message(entry)
        or (isinstance(entry, dict) and entry.get("type") in ("tool", "turn_summary")) for entry in history)


def native_sessions(cwd, env):
    """Read metadata only; never modify a CLI database, profile, or log."""
    cwd = canonical(cwd)
    home = Path.home()
    found = []

    def add(agent, session, directory, title="", updated=0, path="", model="", effort="", activity=None):
        if not session or not isinstance(directory, str) or not directory or canonical(directory) != cwd:
            return
        if activity is False:
            return
        found.append(dict(agent=agent, session=str(session), cwd=cwd, title=str(title or session)[:160],
                          updated=timestamp(updated), source=str(path), model=str(model or ""), effort=str(effort or ""), has_content=True))

    codex = Path(env.get("CODEX_HOME", home / ".codex")).expanduser()
    # SQLite is a fast metadata index; fall back to JSONL on older CLI versions.
    indexed = False
    for db in sorted(codex.glob("state_*.sqlite"), reverse=True)[:1]:
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2)) as connection:
                connection.row_factory = sqlite3.Row
                columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
                activity = [name for name in ("has_user_event", "tokens_used") if name in columns]
                used = " AND (" + " OR ".join(f"{name}>0" for name in activity) + ")" if activity else ""
                for row in connection.execute("SELECT * FROM threads WHERE archived=0" + used + " ORDER BY updated_at DESC LIMIT 2000"):
                    row = dict(row)
                    if str(row.get("source", "")).startswith("subagent"):
                        continue
                    add("codex", row["id"], row["cwd"], row.get("title"), row.get("updated_at"),
                        row.get("rollout_path", ""), row.get("model"), row.get("reasoning_effort"),
                        activity=True if activity else log_activity(row.get("rollout_path", ""), "codex"))
                indexed = True
        except (sqlite3.Error, OSError, ValueError, KeyError):
            pass
    if not indexed:
        for path in sorted((codex / "sessions").glob("*/*/*/*.jsonl"), reverse=True)[:2000]:
            for item in records(path, 1):
                data = item.get("payload", {})
                if item.get("type") == "session_meta" and isinstance(data, dict):
                    add("codex", data.get("id"), data.get("cwd"), path.stem, modified(path), path, activity=log_activity(path, "codex"))

    pi = Path(env.get("PI_CODING_AGENT_DIR", home / ".pi/agent")).expanduser() / "sessions"
    for path in sorted(pi.glob("*/*.jsonl"), reverse=True)[:2000]:
        header = next(records(path, 1), {})
        if header.get("type") != "session" or not isinstance(header.get("cwd"), str) or not header["cwd"] or canonical(header["cwd"]) != cwd:
            continue
        title = ""
        for item in records(path, 40):
            if item.get("type") == "session_info":
                title = item.get("name", title)
            message = item.get("message", {})
            if not title and isinstance(message, dict) and message.get("role") == "user":
                title = content_text(message.get("content"))
        add("pi", str(path), header["cwd"], title, modified(path), path, activity=log_activity(path, "pi"))

    claude = Path(env.get("CLAUDE_CONFIG_DIR", home / ".claude")).expanduser() / "projects"
    for path in sorted(claude.glob("*/*.jsonl"), reverse=True)[:2000]:
        for item in records(path, 60):
            if item.get("isSidechain") or not isinstance(item.get("cwd"), str) or not item["cwd"]:
                continue
            if canonical(item["cwd"]) != cwd:
                break
            message = item.get("message", {})
            title = content_text(message.get("content", "")) if isinstance(message, dict) else ""
            add("claude", item.get("sessionId", path.stem), item["cwd"], title, modified(path), path, activity=log_activity(path, "claude"))
            break

    dsh = Path(env.get("DSH_HOME", home / ".dsh")).expanduser()
    for path in sorted((dsh / "storages/session_projcache/sessions").glob("*.json"), reverse=True)[:2000]:
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            record = json.loads(path.read_text()).get("record", {})
            identity, rows = record.get("identity", {}), record.get("rows", {})
            title = rows.get("title", "")
            if isinstance(title, dict):
                title = title.get("val", title.get("title", title.get("value", "")))
            counters = []
            for key, field in (("titleInput", "count"), ("turnBoundary", "lastTurn")):
                value = rows.get(key, {})
                if isinstance(value, dict):
                    value = value.get("val", value)
                    if isinstance(value, dict) and isinstance(value.get(field), (int, float)):
                        counters.append(value[field])
            add("deepseek", path.stem, identity.get("cwd"), title, path.stat().st_mtime, path,
                activity=any(value > 0 for value in counters) if counters else None)
        except (OSError, ValueError, TypeError, AttributeError):
            continue

    zcode = Path(env.get("ZCODE_SESSION_DB_PATH") or str(Path(env.get("ZCODE_STORAGE_DIR", home / ".zcode")) / "cli/db/db.sqlite")).expanduser()
    if zcode.is_file():
        try:
            with closing(sqlite3.connect(zcode.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2)) as connection:
                has_messages = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='message'").fetchone()
                used = " AND EXISTS(SELECT 1 FROM message WHERE session_id=session.id AND CASE WHEN json_valid(data) THEN json_extract(data,'$.role') END IN ('user','assistant'))" if has_messages else ""
                for row in connection.execute("SELECT id,directory,title,time_updated FROM session WHERE time_deleted IS NULL AND parent_id IS NULL" + used + " ORDER BY time_updated DESC LIMIT 2000"):
                    add("zcode", *row, path=zcode)
        except (sqlite3.Error, OSError):
            pass
    hermes = Path(env.get("HERMES_HOME", home / ".hermes")).expanduser() / "state.db"
    if hermes.is_file():
        try:
            with closing(sqlite3.connect(hermes.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2)) as connection:
                connection.row_factory = sqlite3.Row
                has_messages = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()
                used = " AND EXISTS(SELECT 1 FROM messages WHERE session_id=sessions.id AND role IN ('user','assistant') AND length(trim(coalesce(content,'')))>0)" if has_messages else ""
                for row in connection.execute("SELECT * FROM sessions WHERE source='acp'" + used + " ORDER BY started_at DESC LIMIT 2000"):
                    row = dict(row)
                    meta = row.get("model_config") or {}
                    if isinstance(meta, str):
                        try:
                            meta = json.loads(meta)
                        except ValueError:
                            meta = {}
                    if not isinstance(meta, dict):
                        meta = {}
                    add("hermes", row["id"], row.get("cwd") or meta.get("cwd"), row.get("title"),
                        row.get("ended_at") or row.get("started_at"), hermes)
        except (sqlite3.Error, OSError, KeyError):
            pass
    return sorted(found, key=lambda item: item["updated"], reverse=True)[:500]


def content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text"))
    return ""


def bounded_history(history):
    bounded, size = [], 2
    for entry in reversed(list(history)[-200:]):
        length = len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) + 2
        if size + length > 8 * 1024 * 1024:
            break
        bounded.append(entry)
        size += length
    return list(reversed(bounded))


def native_history(item):
    if item["agent"] not in ("pi", "codex", "claude", "hermes"):
        return []
    path = Path(item.get("source", ""))
    if not path.is_file():
        return []
    result = deque(maxlen=200)
    if item["agent"] == "hermes":
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
                rows = db.execute("SELECT id,role,content FROM messages WHERE session_id=? AND role IN ('user','assistant') ORDER BY id DESC LIMIT 200", (item["session"],)).fetchall()
                for key, role, content in reversed(rows):
                    result.append(dict(type="message", id=f"import-{key}", role=role, text=str(content or "")[:256 * 1024]))
        except (sqlite3.Error, OSError):
            pass
        return bounded_history(result)
    for index, event in enumerate(records(path, 5000)):
        message = event.get("message", {})
        if item["agent"] == "codex" and event.get("type") == "response_item":
            message = event.get("payload", {})
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            continue
        text = content_text(message.get("content"))
        if text:
            result.append(dict(type="message", id=f"import-{index}", role=message["role"], text=text[:256 * 1024]))
    return bounded_history(result)


class SessionStore:
    def __init__(self, directory=None):
        directory = directory or os.environ.get("KATE_AGENT_STATE_HOME") or str(Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "kate-agent-panel")
        directory = Path(directory).expanduser()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / "sessions.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, agent TEXT NOT NULL, session TEXT NOT NULL, cwd TEXT NOT NULL, title TEXT NOT NULL, updated REAL NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL, source TEXT NOT NULL, history TEXT NOT NULL DEFAULT '[]', UNIQUE(agent,session,cwd))")
            db.execute("CREATE TABLE IF NOT EXISTS workspaces (cwd TEXT PRIMARY KEY, last_session TEXT)")
            if "has_content" not in {row[1] for row in db.execute("PRAGMA table_info(sessions)")}:
                db.execute("ALTER TABLE sessions ADD COLUMN has_content INTEGER NOT NULL DEFAULT 0")
                for row in db.execute("SELECT id,history FROM sessions WHERE history!='[]'").fetchall():
                    try:
                        used = history_activity(json.loads(row["history"]))
                    except (ValueError, TypeError):
                        used = False
                    if used:
                        db.execute("UPDATE sessions SET has_content=1 WHERE id=?", (row["id"],))
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, item, history=None, select=False):
        item = {**item, "cwd": canonical(item["cwd"])}
        with self.connect() as db:
            previous = db.execute("SELECT id,has_content FROM sessions WHERE agent=? AND session=? AND cwd=?", (item["agent"], item["session"], item["cwd"])).fetchone()
            key = previous["id"] if previous else item.get("id", uuid.uuid4().hex)
            values = (key, item["agent"], item["session"], canonical(item["cwd"]), str(item.get("title") or item["session"])[:160],
                      item.get("updated", time.time()), item.get("model", ""), item.get("effort", ""), str(item.get("source", "")))
            db.execute("INSERT INTO sessions(id,agent,session,cwd,title,updated,model,effort,source) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,updated=excluded.updated,model=excluded.model,effort=excluded.effort,source=excluded.source", values)
            if history is not None:
                db.execute("UPDATE sessions SET history=? WHERE id=?", (json.dumps(bounded_history(history), ensure_ascii=False), key))
            used = bool(item.get("has_content") or (previous and previous["has_content"])
                        or history_activity(history))
            db.execute("UPDATE sessions SET has_content=? WHERE id=?", (int(used), key))
            if select and used:
                db.execute("INSERT INTO workspaces VALUES(?,?) ON CONFLICT(cwd) DO UPDATE SET last_session=excluded.last_session", (canonical(item["cwd"]), key))
        return key

    def list(self, cwd):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,agent,session,cwd,title,updated,model,effort,source FROM sessions WHERE cwd=? AND has_content=1 ORDER BY updated DESC LIMIT 500", (canonical(cwd),))]

    def get(self, key, cwd):
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=? AND cwd=?", (key, canonical(cwd))).fetchone()
        if not row:
            raise ValueError("Session is unavailable in this working directory")
        item = dict(row)
        item["history"] = json.loads(item["history"])
        return item

    def last(self, cwd):
        with self.connect() as db:
            row = db.execute("SELECT last_session FROM workspaces WHERE cwd=?", (canonical(cwd),)).fetchone()
        return row[0] if row else None

    def forget_last(self, cwd):
        with self.connect() as db:
            db.execute("INSERT INTO workspaces VALUES(?,'') ON CONFLICT(cwd) DO UPDATE SET last_session=''", (canonical(cwd),))
