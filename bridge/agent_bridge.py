#!/usr/bin/env python3
"""Kate Agent Panel's stdlib-only JSONL transport. Never manages agent installs/configs."""
from __future__ import annotations

import asyncio
import codecs
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_store import SessionStore, canonical, native_sessions, native_history, timestamp


class RPCError(RuntimeError):
    def __init__(self, error):
        self.code = error.get("code") if isinstance(error, dict) else None
        super().__init__(error.get("message", str(error)) if isinstance(error, dict) else str(error))

MAX_RECORD = 16 * 1024 * 1024
VERSION = "0.2.0"
AGENTS = {
    "deepseek": ("DeepSeek Harness", "dsh", "acp"),
    "pi": ("Pi", "pi", "rpc"),
    "hermes": ("Hermes", "hermes", "acp"),
    "codex": ("Codex", "codex", "app-server"),
    "claude": ("Claude Code", "claude", "stream-json"),
    "kimi": ("Kimi Code", "kimi", "acp"),
    "zcode": ("ZCode", "zcode", "app-server"),
}
ENV_KEYS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL",
    "KIMI_API_KEY", "MOONSHOT_API_KEY", "ZAI_API_KEY", "ZHIPU_API_KEY",
    "OPENROUTER_API_KEY", "HERMES_HOME", "CODEX_HOME", "PI_CODING_AGENT_DIR", "DSH_HOME",
)


def process_environment():
    """Inherit the process environment; discover common user bin dirs without sourcing shells."""
    env = os.environ.copy()
    home = Path.home()
    dirs = [home / ".local/bin", home / ".kimi-code/bin", home / ".cargo/bin",
            home / ".bun/bin", home / ".npm-global/bin"]
    # Compare numeric versions (v24 before v9), while preserving the user's PATH priority.
    versions = []
    for directory in (home / ".nvm/versions/node").glob("*/bin"):
        version = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", directory.parent.name)
        if version:
            versions.append((tuple(map(int, version.groups())), directory))
    dirs += [directory for _, directory in sorted(versions, reverse=True)]
    paths = env["PATH"].split(os.pathsep) if "PATH" in env else os.defpath.split(os.pathsep)
    paths += [str(p) for p in dirs if p.is_dir()]
    env["PATH"] = os.pathsep.join(dict.fromkeys(paths))
    return env


def command_for(agent, env=None):
    env = process_environment() if env is None else env
    override = env.get(f"KATE_AGENT_{agent.upper()}_COMMAND", "")
    command = shlex.split(override) if override else [AGENTS[agent][1]]
    if not command:
        return []
    executable = shutil.which(command[0], path=env.get("PATH", os.defpath))
    return [executable, *command[1:]] if executable else []


def inventory(env=None):
    env = process_environment() if env is None else env
    agents = []
    for key, (name, _, protocol) in AGENTS.items():
        try:
            command = command_for(key, env)
            error = ""
        except ValueError as exc:
            command, error = [], str(exc)
        if key == "deepseek" and command and not dsh_profile_ready(env):
            error = "Existing ACP profile required; the panel does not initialize Harness configuration"
        agents.append(dict(id=key, name=name, available=bool(command) and not error,
                           found=bool(command), executable=command[0] if command else "",
                           protocol=protocol, error=error))
    return dict(type="inventory", agents=agents, environment=[k for k in ENV_KEYS if env.get(k)],
                overrides=[k for k in env if k.startswith("KATE_AGENT_") and k.endswith("_COMMAND")])


def dsh_profile_ready(env):
    return (Path(env.get("DSH_HOME", str(Path.home() / ".dsh"))).expanduser() / "profiles/acp/cordis.yml").is_file()


class TurnTiming:
    """Non-overlapping wall time inferred from received events, not server profiling."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = self.changed = clock()
        self.id = uuid.uuid4().hex
        self.phase = "waiting"
        self.durations = dict.fromkeys(("waiting", "reasoning", "tools", "response", "user"), 0.0)
        self.tools = set()
        self.requests = set()
        self.failed = False

    def advance(self, phase):
        now = self.clock()
        self.durations[self.phase] += max(0, now - self.changed)
        self.changed, self.phase = now, phase

    def observe(self, kind, fields):
        phase = self.phase
        if kind == "tool":
            if fields.get("status") in ("done", "completed", "failed", "cancelled", "error"):
                self.tools.discard(fields.get("id"))
                phase = "waiting"
            else:
                self.tools.add(fields.get("id"))
                phase = "tools"
        elif kind == "request":
            self.requests.add(fields.get("id"))
        elif kind == "request_resolved":
            self.requests.discard(fields.get("id"))
            phase = "waiting"
        elif kind == "requests_cleared":
            self.requests.clear()
            phase = "waiting"
        elif kind == "message" and fields.get("role") in ("assistant", "reasoning"):
            phase = "reasoning" if fields["role"] == "reasoning" else "response"
        elif kind == "error":
            self.failed = True
        self.advance("user" if self.requests else "tools" if self.tools else phase)

    def finish(self, stopped=False):
        self.advance(self.phase)
        return dict(id=self.id, elapsed=sum(self.durations.values()), durations=dict(self.durations),
                    outcome="stopped" if stopped else "failed" if self.failed else "completed")


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def validate_answers(questions, value):
    if not isinstance(value, dict):
        raise ValueError("Question answers must be an object")
    answers = {}
    for question in questions:
        key = question["id"]
        answer = value.get(key)
        if answer is None or answer == "" or answer == []:
            if question.get("required"):
                raise ValueError(f"An answer is required for {question.get('question', key)}")
            continue
        kind = question.get("valueType", "string")
        if kind == "boolean" and not isinstance(answer, bool):
            raise ValueError("Expected a boolean answer")
        if kind in ("number", "integer") and (isinstance(answer, bool) or not isinstance(answer, (float, int)) or not math.isfinite(answer) or (kind == "integer" and int(answer) != answer)):
            raise ValueError("Expected a numeric answer")
        if kind == "array" and not isinstance(answer, list):
            raise ValueError("Expected multiple selected answers")
        if kind == "string" and not isinstance(answer, str) and not (question.get("multiSelect") and isinstance(answer, list)):
            raise ValueError("Expected a text answer")
        if isinstance(answer, list) and any(not isinstance(v, (str, int, float, bool)) for v in answer):
            raise ValueError("Invalid selected answer")
        options = question.get("options", [])
        if options and not question.get("allowOther", True):
            allowed = [o.get("value", o["label"]) for o in options]
            if any(v not in allowed for v in (answer if isinstance(answer, list) else [answer])):
                raise ValueError("Answer is not one of the offered choices")
        answers[key] = answer
    return answers


def form_questions(schema):
    questions = []
    for key, field in schema.get("properties", {}).items():
        one_of = field.get("oneOf", field.get("items", {}).get("anyOf", []))
        values = field.get("enum", field.get("items", {}).get("enum", [v.get("const") for v in one_of]))
        labels = field.get("enumNames", [])
        if one_of:
            labels = [v.get("title", v.get("const")) for v in one_of]
        questions.append(dict(id=key, question=field.get("title", key), detail=field.get("description", ""),
            options=[dict(label=str(labels[i] if i < len(labels) else v), value=v,
                description=one_of[i].get("description", "") if i < len(one_of) else "") for i, v in enumerate(values)],
            multiSelect=field.get("type") == "array", valueType=field.get("type", "string"),
            isSecret=field.get("format") == "password", required=key in schema.get("required", []), allowOther=not values))
    return questions


class Adapter:
    def __init__(self, bridge, agent, cwd):
        self.bridge, self.agent, self.cwd = bridge, agent, cwd
        self.process = None
        self.tasks = []
        self.serial = 0
        self.waiters = {}
        self.requests = {}
        self.session = ""
        self.turn = ""
        self.busy = False
        self.closing = False
        self.message_prefix = uuid.uuid4().hex
        self.model = ""
        self.effort = ""
        self.configuration_id = ""
        self.timing = None
        self.restore_native_settings = False
        self.connected = False

    def starting_parameters(self):
        return ("", "") if self.restore_native_settings else (self.model, self.effort)

    async def connect(self):
        raise NotImplementedError("This agent cannot inspect its session configuration")

    async def configure(self, model, effort):
        """Restart transports without live setters, retaining the native session ID."""
        previous = self.model, self.effort
        if previous == (model, effort):
            await self.connect()
            return
        await self.close()
        self.model, self.effort = model, effort
        try:
            await self.connect()
        except BaseException:
            await self.close()
            self.model, self.effort = previous
            raise

    def configuration_ready(self):
        self.emit("configuration", agent=self.agent, session=self.session, cwd=self.cwd,
                  model=self.model, effort=self.effort, requestId=self.configuration_id)

    def emit(self, kind, **fields):
        if self.timing:
            self.timing.observe(kind, fields)
        if hasattr(self.bridge, "record"):
            self.bridge.record(self, dict(type=kind, **fields))
        self.bridge.emit(dict(type=kind, **fields))

    def message(self, id, text, role="assistant", append=False):
        if text:
            self.emit("message", id=f"{self.message_prefix}:{id}", text=text, role=role, append=append)

    def status(self, busy, text=""):
        if busy and not self.timing:
            self.timing = TurnTiming()
        elif not busy and self.timing:
            summary = self.timing.finish(stopped=text == "Stopped")
            self.timing = None
            self.emit("turn_summary", **summary)
        self.busy = busy
        self.emit("status", busy=busy, text=text or ("Working" if busy else "Ready"))

    async def launch(self, args, extra_env=None, pass_fds=()):
        env = {**process_environment(), **(extra_env or {})}
        command = command_for(self.agent, env)
        if not command:
            raise RuntimeError(f"{AGENTS[self.agent][0]} executable not found")
        self.closing = False
        self.process = await asyncio.create_subprocess_exec(
            *command, *args, cwd=self.cwd, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=MAX_RECORD, pass_fds=pass_fds)
        self.tasks = [asyncio.create_task(self.read_stdout(self.process)), asyncio.create_task(self.read_stderr(self.process))]

    async def write(self, data):
        if not self.process or self.process.returncode is not None:
            raise RuntimeError("Agent process is not running")
        self.process.stdin.write(json.dumps(data, ensure_ascii=False).encode() + b"\n")
        await self.process.stdin.drain()

    async def rpc(self, method, params):
        self.serial += 1
        id = str(self.serial) if self.agent == "zcode" else self.serial
        future = asyncio.get_running_loop().create_future()
        self.waiters[id] = future
        try:
            envelope = dict(id=id, method=method, params=params)
            if self.agent != "zcode":
                envelope["jsonrpc"] = "2.0"
            await self.write(envelope)
            return await asyncio.wait_for(future, 60)
        finally:
            self.waiters.pop(id, None)

    async def read_stdout(self, process):
        try:
            while line := await process.stdout.readline():
                try:
                    data = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    self.message(uuid.uuid4().hex, line.decode(errors="replace").strip(), "diagnostic")
                    continue
                if not isinstance(data, dict):
                    continue
                if "id" in data and "method" not in data and ("result" in data or "error" in data):
                    future = self.waiters.get(data["id"])
                    if future and not future.done():
                        if "error" in data:
                            future.set_exception(RPCError(data["error"]))
                        else:
                            # Every RPC method issued by these adapters has an object result.
                            result = data.get("result")
                            if not isinstance(result, dict):
                                future.set_exception(RuntimeError("Agent returned a non-object RPC result"))
                            else:
                                future.set_result(result)
                        continue
                await self.handle(data)
            code = await process.wait()
            for future in list(self.waiters.values()):
                if not future.done():
                    future.set_exception(RuntimeError(f"Agent exited ({code})"))
            if not self.closing:
                self.requests.clear()
                self.emit("requests_cleared")
                self.process_exited(code)
        except (ValueError, OSError, RuntimeError, TypeError, KeyError, AttributeError) as exc:
            if not self.closing:
                self.emit("error", text=str(exc))
                await self.close()
                self.status(False)

    def process_exited(self, code):
        if code:
            self.emit("error", text=f"{AGENTS[self.agent][0]} exited with code {code}")
        elif self.busy:
            self.emit("error", text="Agent exited before completing the turn")
        self.status(False)

    async def read_stderr(self, process):
        # Chunking also handles progress output without LF. Kept separate from the protocol.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while chunk := await process.stderr.read(4096):
            self.message("stderr", decoder.decode(chunk), "diagnostic", append=True)
        self.message("stderr", decoder.decode(b"", final=True), "diagnostic", append=True)

    async def handle(self, data):
        pass

    async def ask(self, key, title, options=None, questions=None, **context):
        token = uuid.uuid4().hex
        if questions:
            context["questions"] = questions
        self.requests[token] = (key, context)
        self.emit("request", id=token, title=title, options=options or [], questions=questions or [])

    async def reply(self, token, value):
        raise RuntimeError("This adapter does not support replies")

    async def cancel(self):
        await self.close()
        self.status(False, "Stopped")

    async def close(self):
        self.connected = False
        self.closing = True
        self.requests.clear()
        if self.process and self.process.returncode is None:
            # A separate process group prevents a stopped turn from leaving tool children running.
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except asyncio.TimeoutError:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self.process.wait()
        for task in self.tasks:
            if task is not asyncio.current_task():
                task.cancel()
        await asyncio.gather(*[t for t in self.tasks if t is not asyncio.current_task()], return_exceptions=True)
        for future in self.waiters.values():
            if not future.done():
                future.set_exception(RuntimeError("Session closed"))
        self.waiters.clear()

    def session_ready(self):
        self.connected = True
        self.restore_native_settings = False
        self.emit("session", agent=self.agent, session=self.session, cwd=self.cwd, model=self.model, effort=self.effort)
        self.configuration_ready()


class PiAdapter(Adapter):
    async def pi_rpc(self, kind, **params):
        key = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.waiters[key] = future
        try:
            await self.write(dict(type=kind, id=key, **params))
            return await asyncio.wait_for(future, 30)
        finally:
            self.waiters.pop(key, None)

    def read_state(self, state):
        self.session = state.get("sessionFile") or self.session
        model = state.get("model")
        if isinstance(model, dict):
            self.model = "/".join(str(model[k]) for k in ("provider", "id") if model.get(k))
        if "thinkingLevel" in state:
            self.effort = state["thinkingLevel"] or ""
        self.session_ready()

    async def connect(self):
        if not self.process or self.process.returncode is not None:
            model, effort = self.starting_parameters()
            args = ["--mode", "rpc"]
            if self.session:
                args += ["--session", self.session]
            if model:
                args += ["--model", model]
            if effort:
                args += ["--thinking", effort]
            await self.launch(args)
            self.read_state(await self.pi_rpc("get_state"))

    async def configure(self, model, effort):
        await self.connect()
        if not model or not effort:
            await super().configure(model, effort)
            return
        try:
            if model != self.model:
                available = (await self.pi_rpc("get_available_models")).get("models", [])
                choices = [m for m in available if model in (m.get("id"), f"{m.get('provider')}/{m.get('id')}")]
                if len(choices) != 1:
                    raise ValueError("Pi model is unavailable or ambiguous; enter provider/model-id")
                await self.pi_rpc("set_model", provider=choices[0]["provider"], modelId=choices[0]["id"])
            if effort != self.effort:
                await self.pi_rpc("set_thinking_level", level=effort)
        finally:
            self.read_state(await self.pi_rpc("get_state"))

    async def send(self, text):
        await self.connect()
        self.turn = uuid.uuid4().hex
        self.message_index = 0
        await self.write({"type": "prompt", "id": self.turn, "message": text})

    async def handle(self, data):
        kind = data.get("type")
        if kind == "message_start":
            self.message_index += 1
        elif kind == "message_update":
            event = data.get("assistantMessageEvent", {})
            if event.get("type") in ("text_delta", "thinking_delta"):
                role = "reasoning" if event["type"] == "thinking_delta" else "assistant"
                self.message(f"{self.turn}:{self.message_index}:{role}", event.get("delta", ""), role, True)
        elif kind == "message_end":
            message = data.get("message", {})
            if message.get("errorMessage"):
                self.emit("error", text=message["errorMessage"])
        elif kind in ("tool_execution_start", "tool_execution_update", "tool_execution_end"):
            self.emit("tool", id=data.get("toolCallId", "tool"), title=data.get("toolName", "Tool"),
                      text=json.dumps(data.get("result", data.get("partialResult", data.get("args", {}))), ensure_ascii=False),
                      status="done" if kind.endswith("end") else "running")
        elif kind == "agent_settled":
            self.status(False)
        elif kind == "response":
            future = self.waiters.get(data.get("id"))
            if future and not future.done():
                if data.get("success", True):
                    future.set_result(data.get("data", {}))
                else:
                    future.set_exception(RuntimeError(data.get("error", "Pi command failed")))
                return
            if not data.get("success", True):
                self.emit("error", text=data.get("error", "Pi command failed"))
                self.status(False)
            if data.get("command") == "get_state":
                self.read_state(data.get("data", {}))
            if data.get("command") == "prompt" and data.get("data", {}).get("disposition") == "handled":
                self.status(False)
        elif kind == "extension_ui_request":
            method = data.get("method")
            if method in ("confirm", "select", "input", "editor"):
                options = [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}] if method == "confirm" else [
                    {"id": s, "label": s} for s in data.get("options", [])]
                await self.ask(data["id"], data.get("title", "") + "\n" + data.get("message", ""),
                               options=options, method=method)
            elif method in ("notify", "setStatus"):
                self.message(uuid.uuid4().hex, data.get("message", data.get("text", "")), "notice")

    async def reply(self, token, value):
        key, context = self.requests[token]
        response = dict(type="extension_ui_response", id=key)
        if isinstance(value, dict) and value.get("_cancel"):
            response["cancelled"] = True
        elif context["method"] == "confirm":
            response["confirmed"] = value == "yes"
        else:
            response["value"] = str(value)
        await self.write(response)
        self.requests.pop(token, None)


class ACPAdapter(Adapter):
    def __init__(self, *args):
        super().__init__(*args)
        self.tools = {}
        self.options = []
        self.replaying = False
        self.question_writer = None
        self.question_ready = False

    async def launch_acp(self):
        if self.agent != "deepseek":
            await self.launch(["acp"])
            return
        parent, child = socket.socketpair()
        parent.setblocking(False)
        try:
            helper = Path(__file__).resolve().with_name("dsh_questions.mjs").as_uri()
            options = os.environ.get("NODE_OPTIONS", "") + " --import=" + json.dumps(helper, ensure_ascii=False)
            await self.launch(["--profile", "acp"], {"NODE_OPTIONS": options, "KATE_AGENT_DSH_UI_FD": str(child.fileno()),
                "KATE_AGENT_DSH_UI_OWNER": ""}, pass_fds=(child.fileno(),))
            reader, self.question_writer = await asyncio.open_connection(sock=parent, limit=MAX_RECORD)
            self.tasks.append(asyncio.create_task(self.read_questions(reader)))
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()

    async def read_questions(self, reader):
        try:
            while line := await reader.readline():
                event = json.loads(line)
                if event.get("type") == "ready":
                    self.question_ready = True
                elif event.get("type") == "question":
                    await self.ask(event["id"], "DeepSeek Harness question", questions=[{**q, "required": True} for q in event["questions"]], method="dsh-question")
                elif event.get("type") == "cancel":
                    for token, (key, context) in list(self.requests.items()):
                        if key == event["id"] and context.get("method") == "dsh-question":
                            self.requests.pop(token); self.emit("request_resolved", id=token)
                elif event.get("type") == "error":
                    self.emit("error", text=event.get("text", "Question bridge failed"))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.emit("error", text=f"Question bridge: {exc}")

    async def close(self):
        await super().close()
        if self.question_writer:
            self.question_writer.close()
            try:
                await self.question_writer.wait_closed()
            except OSError:
                pass
            self.question_writer = None

    async def connect(self):
        if not self.process or self.process.returncode is not None:
            if self.agent == "deepseek" and not dsh_profile_ready(process_environment()):
                raise RuntimeError("DeepSeek Harness needs an existing ACP profile; automatic configuration is disabled")
            await self.launch_acp()
            hello = await self.rpc("initialize", {"protocolVersion": 1, "clientCapabilities": {"elicitation": {"form": {}}},
                                        "clientInfo": {"name": "kate-agent-panel", "version": VERSION}})
            if hello.get("protocolVersion") != 1:
                raise RuntimeError("Unsupported ACP protocol version; this panel requires version 1")
            capabilities = hello.get("agentCapabilities", {})
            if self.session:
                if "resume" in capabilities.get("sessionCapabilities", {}):
                    method = "session/resume"
                elif capabilities.get("loadSession"):
                    method = "session/load"
                else:
                    raise RuntimeError("This agent cannot resume the selected session; choose New Chat explicitly")
                self.replaying = True
                try:
                    result = await self.rpc(method, {"sessionId": self.session, "cwd": self.cwd, "mcpServers": []})
                finally:
                    self.replaying = False
            else:
                result = await self.rpc("session/new", {"cwd": self.cwd, "mcpServers": []})
                self.session = result["sessionId"]
                self.tools.clear()
            model, effort = self.starting_parameters()
            self.read_configuration(result)
            await self.apply_configuration(model, effort)
            self.session_ready()

    def read_configuration(self, result):
        self.options = result.get("configOptions", self.options)
        if "configOptions" in result and self.config_option("thought_level") is None:
            self.effort = ""
        for field, category in (("model", "model"), ("effort", "thought_level")):
            option = self.config_option(category)
            if option and "currentValue" in option:
                value = str(option["currentValue"] or "")
                if field == "model" and self.agent == "deepseek":
                    try:
                        pair = json.loads(value)
                        if isinstance(pair, list) and len(pair) == 2 and all(isinstance(p, str) for p in pair):
                            value = "/".join(pair)
                    except ValueError:
                        pass
                setattr(self, field, value)
        legacy = result.get("models", {})
        if legacy.get("currentModelId"):
            self.model = legacy["currentModelId"]
        self.configuration_ready()

    def config_option(self, category):
        names = ("model",) if category == "model" else ("reasoning_effort", "thinking", "thought_level", "reasoning")
        return next((o for o in self.options if o.get("id") in names or o.get("category") == category), None)

    async def apply_configuration(self, model, effort):
        for category, value in (("model", model), ("thought_level", effort)):
            if not value:
                continue
            option = self.config_option(category)
            field = "model" if category == "model" else "effort"
            if option is None:
                if category == "model":
                    if value != self.model:
                        result = await self.rpc("session/set_model", {"sessionId": self.session, "modelId": value})
                        self.model = value
                        self.read_configuration(result)
                    continue
                raise RuntimeError("This agent does not expose a reasoning effort option; leave the field empty to use its default")
            choices = []
            for entry in option.get("options", []):
                choices.extend(entry.get("options", [entry]))
            def matches_model(choice):
                raw = str(choice.get("value", ""))
                if value in (raw, choice.get("name")) or raw.endswith("/" + value):
                    return True
                if self.agent == "deepseek" and category == "model":
                    try:
                        pair = json.loads(raw)
                        return isinstance(pair, list) and len(pair) == 2 and value in (pair[1], "/".join(pair))
                    except (ValueError, TypeError):
                        pass
                return False
            matches = [c["value"] for c in choices if matches_model(c)]
            selected = matches[0] if len(matches) == 1 else value
            if selected == option.get("currentValue"):
                continue
            result = await self.rpc("session/set_config_option", {"sessionId": self.session, "configId": option["id"], "value": selected})
            setattr(self, field, value)
            self.read_configuration(result)

    async def configure(self, model, effort):
        await self.connect()
        # Empty fields retain the agent's effective default; ACP has no universal reset operation.
        try:
            await self.apply_configuration(model, effort)
        finally:
            self.session_ready()

    async def send(self, text):
        await self.connect()
        self.turn = uuid.uuid4().hex
        result = await self.rpc_prompt(text)
        self.status(False, str(result.get("stopReason", "Ready")))

    async def rpc_prompt(self, text):
        # Unlike initialization, a model turn has no arbitrary 60-second timeout.
        self.serial += 1
        key = self.serial
        future = asyncio.get_running_loop().create_future()
        self.waiters[key] = future
        try:
            await self.write(dict(jsonrpc="2.0", id=key, method="session/prompt",
                                  params={"sessionId": self.session, "prompt": [{"type": "text", "text": text}]}))
            return await future
        finally:
            self.waiters.pop(key, None)

    async def handle(self, data):
        method, params = data.get("method", ""), data.get("params", {})
        if method == "session/update":
            if self.replaying:
                return
            update = params.get("update", {})
            kind = update.get("sessionUpdate")
            if kind in ("agent_message_chunk", "agent_thought_chunk"):
                role = "reasoning" if kind == "agent_thought_chunk" else "assistant"
                content = update.get("content", {})
                if content.get("type") == "text":
                    self.message(f"{self.turn}:{role}", content.get("text", ""), role, True)
            elif kind in ("tool_call", "tool_call_update"):
                key = update.get("toolCallId", "tool")
                tool = self.tools.setdefault(key, {"title": "Tool", "text": "{}", "status": "pending"})
                for field in ("title", "status"):
                    if field in update:
                        tool[field] = update[field]
                if "content" in update or "rawInput" in update:
                    tool["text"] = json.dumps(update.get("content", update.get("rawInput")), ensure_ascii=False)[-2 * 1024 * 1024:]
                self.emit("tool", id=key, **tool)
                if len(self.tools) > 1000:
                    self.tools.pop(next(iter(self.tools)))
            elif kind == "config_option_update":
                self.read_configuration(update)
            elif kind == "plan":
                self.message("plan", json.dumps(update.get("entries", []), ensure_ascii=False), "reasoning")
            elif kind not in ("current_mode_update", "available_commands_update", "usage_update"):
                self.message(uuid.uuid4().hex, json.dumps(update, ensure_ascii=False), "diagnostic")
        elif method == "session/request_permission":
            choices = [{"id": o["optionId"], "label": o["name"]} for o in params.get("options", [])]
            await self.ask(data["id"], params.get("toolCall", {}).get("title", "Agent permission request"),
                           choices, allowed=[c["id"] for c in choices])
        elif method == "elicitation/create" and params.get("mode", "form") == "form":
            await self.ask(data["id"], params.get("message", "Agent question"), questions=form_questions(params.get("requestedSchema", {})), method="elicitation")
        elif "id" in data and method:
            await self.write(dict(jsonrpc="2.0", id=data["id"], error={"code": -32601, "message": "Client capability not supported"}))

    async def reply(self, token, value):
        key, context = self.requests[token]
        if context.get("method") == "dsh-question":
            if not self.question_writer:
                raise RuntimeError("Question connection is closed")
            cancelled = isinstance(value, dict) and value.get("_cancel") is True
            answers = []
            if not cancelled:
                values = validate_answers(context["questions"], value)
                for question in context["questions"]:
                    raw = values.get(question["id"], "")
                    selected = raw if isinstance(raw, list) else [raw]
                    options = [o["label"] for o in question.get("options", [])]
                    answers.append(dict(id=question["id"], selected=[v for v in selected if v in options],
                        custom=", ".join(str(v) for v in selected if v not in options)))
            self.question_writer.write((json.dumps(dict(id=key, cancelled=cancelled, answer={"answers": answers})) + "\n").encode())
            await self.question_writer.drain()
            self.requests.pop(token, None)
            return
        if context.get("method") == "elicitation":
            cancelled = isinstance(value, dict) and value.get("_cancel") is True
            result = {"action": "cancel"} if cancelled else {"action": "accept", "content": validate_answers(context["questions"], value)}
            await self.write(dict(jsonrpc="2.0", id=key, result=result))
            self.requests.pop(token, None)
            return
        outcome = {"outcome": "selected", "optionId": value} if value in context["allowed"] else {"outcome": "cancelled"}
        await self.write(dict(jsonrpc="2.0", id=key, result={"outcome": outcome}))
        self.requests.pop(token, None)


class CodexAdapter(Adapter):
    async def configure(self, model, effort):
        # Codex does not persist an empty thread until its first turn. Recreate
        # only that empty thread when applying startup-only configuration.
        if getattr(self, "empty_thread", False) and (model, effort) != (self.model, self.effort):
            self.session = ""
        await super().configure(model, effort)

    async def connect(self):
        if not self.process or self.process.returncode is not None:
            model, effort = self.starting_parameters()
            await self.launch(["app-server"])
            await self.rpc("initialize", {"clientInfo": {"name": "kate_agent_panel", "title": "Kate Agent Panel", "version": VERSION}, "capabilities": {"experimentalApi": True}})
            await self.write({"method": "initialized"})
            if self.session:
                result = await self.rpc("thread/resume", {"threadId": self.session, "cwd": self.cwd, "excludeTurns": True, **({"model": model} if model else {}),
                    **({"config": {"model_reasoning_effort": effort}} if effort else {})})
            else:
                result = await self.rpc("thread/start", {"cwd": self.cwd, **({"model": model} if model else {}),
                    **({"config": {"model_reasoning_effort": effort}} if effort else {})})
                self.empty_thread = True
            self.session = result["thread"]["id"]
            self.model = result.get("model") or self.model
            if "reasoningEffort" in result:
                self.effort = result["reasoningEffort"] or ""
            self.session_ready()
    async def send(self, text):
        await self.connect()
        result = await self.rpc("turn/start", {"threadId": self.session, "input": [{"type": "text", "text": text}],
            **({"model": self.model} if self.model else {}), **({"effort": self.effort} if self.effort else {})})
        self.turn = result["turn"]["id"]
        self.empty_thread = False

    async def handle(self, data):
        method, p = data.get("method", ""), data.get("params", {})
        if "id" in data and method:
            if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                await self.ask(data["id"], p.get("command") or p.get("reason") or "Approve agent changes?",
                               [{"id": "accept", "label": "Allow once"}, {"id": "decline", "label": "Deny"}],
                               method=method)
            elif method == "item/tool/requestUserInput":
                questions = [{**q, "required": True, "allowOther": q.get("isOther", False)} for q in p.get("questions", [])]
                await self.ask(data["id"], "Agent question", questions=questions, method=method)
            elif method == "mcpServer/elicitation/request" and p.get("mode", "form") in ("form", "openai/form", "openaiForm"):
                await self.ask(data["id"], p.get("message", "MCP question"), questions=form_questions(p.get("requestedSchema", {})), method=method)
            elif method == "item/permissions/requestApproval":
                await self.ask(data["id"], (p.get("reason") or "Additional permissions") + "\n" + json.dumps(p.get("permissions", {})),
                    [{"id": "accept", "label": "Allow for this turn"}, {"id": "decline", "label": "Deny"}], method=method, permissions=p.get("permissions", {}))
            else:
                await self.write({"id": data["id"], "error": {"code": -32601, "message": "Unsupported client request"}})
        elif method == "item/agentMessage/delta":
            self.message(p["itemId"], p.get("delta", ""), append=True)
        elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            self.message(p["itemId"], p.get("delta", ""), "reasoning", True)
        elif method in ("item/started", "item/completed"):
            item = p.get("item", {})
            kind = item.get("type", "")
            if kind == "agentMessage" and method.endswith("completed"):
                self.message(item["id"], item.get("text", ""))
            elif kind not in ("agentMessage", "userMessage", "reasoning"):
                self.emit("tool", id=item.get("id", "tool"), title=item.get("command", kind),
                          text=json.dumps(item, ensure_ascii=False), status="done" if method.endswith("completed") else "running")
        elif method == "turn/completed":
            error = p.get("turn", {}).get("error")
            if error:
                self.emit("error", text=error.get("message", str(error)))
            self.status(False)
        elif method == "error":
            self.emit("error", text=p.get("error", {}).get("message", str(p)))
        elif method == "turn/plan/updated":
            self.message("plan:" + str(p.get("turnId", self.turn)), json.dumps(p.get("plan", []), ensure_ascii=False), "reasoning")
        elif method == "serverRequest/resolved":
            for token, (key, _) in list(self.requests.items()):
                if key == p.get("requestId"):
                    self.requests.pop(token); self.emit("request_resolved", id=token)
        elif method in ("warning", "configWarning", "deprecationNotice"):
            self.message(uuid.uuid4().hex, p.get("message") or json.dumps(p, ensure_ascii=False), "notice")

    async def reply(self, token, value):
        key, context = self.requests[token]
        if context["method"] == "item/tool/requestUserInput":
            values = {} if isinstance(value, dict) and value.get("_cancel") else validate_answers(context["questions"], value)
            answers = {k: {"answers": v if isinstance(v, list) else [str(v)]} for k, v in values.items()}
            result = {"answers": answers}
        elif context["method"] == "mcpServer/elicitation/request":
            cancelled = isinstance(value, dict) and value.get("_cancel") is True
            result = {"action": "cancel" if cancelled else "accept", "content": None if cancelled else validate_answers(context["questions"], value), "_meta": None}
        elif context["method"] == "item/permissions/requestApproval":
            result = {"permissions": context["permissions"] if value == "accept" else {}, "scope": "turn"}
        else:
            result = {"decision": value if value in ("accept", "decline") else "decline"}
        await self.write({"id": key, "result": result})
        self.requests.pop(token, None)


class ClaudeAdapter(Adapter):
    async def control(self, request, timeout=60):
        key = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.waiters[key] = future
        try:
            await self.write({"type": "control_request", "request_id": key, "request": request})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.waiters.pop(key, None)

    async def connect(self):
        if not self.process or self.process.returncode is not None:
            model, effort = self.starting_parameters()
            args = ["--print", "--verbose", "--output-format", "stream-json", "--include-partial-messages",
                    "--input-format", "stream-json", "--permission-prompt-tool", "stdio"]
            if self.session:
                args += ["--resume", self.session]
            if model:
                args += ["--model", model]
            if effort:
                args += ["--effort", effort]
            await self.launch(args)
            result = await self.control({"subtype": "initialize", "hooks": None})
            self.model = result.get("model") or self.model
            self.effort = result.get("effort") or self.effort
            try:
                # Supported by current Claude Agent SDK; older CLIs may reject it.
                result = await self.control({"subtype": "get_context_usage"}, timeout=5)
                self.model = result.get("model") or self.model
            except (RuntimeError, asyncio.TimeoutError):
                pass
            self.restore_native_settings = False
            self.connected = True
            self.configuration_ready()
    async def send(self, text):
        self.turn = uuid.uuid4().hex
        self.message_id = self.turn
        await self.connect()
        await self.write({"type": "user", "message": {"role": "user", "content": text},
                          "parent_tool_use_id": None, "session_id": self.session})

    async def handle(self, data):
        kind = data.get("type")
        if kind == "system" and data.get("subtype") == "init":
            self.model = data.get("model") or self.model
            self.effort = data.get("effort") or self.effort
            self.configuration_ready()
        if data.get("session_id"):
            if self.session != data["session_id"]:
                self.session = data["session_id"]
                self.session_ready()
        if kind == "control_response":
            response = data.get("response", {})
            future = self.waiters.get(response.get("request_id"))
            if future and not future.done():
                if response.get("subtype") == "error":
                    future.set_exception(RuntimeError(response.get("error", "Claude initialization failed")))
                else:
                    future.set_result(response.get("response", {}))
        elif kind == "control_request":
            request = data.get("request", {})
            if request.get("subtype") == "can_use_tool":
                tool = request.get("tool_name", "Tool")
                inputs = request.get("input", {})
                if tool == "AskUserQuestion":
                    questions = [{**q, "id": q.get("question", str(i)), "required": True} for i, q in enumerate(inputs.get("questions", []))]
                    await self.ask(data["request_id"], "Claude question", questions=questions, tool=tool, inputs=inputs)
                else:
                    await self.ask(data["request_id"], tool + "\n" + json.dumps(inputs, ensure_ascii=False),
                                   [{"id": "allow", "label": "Allow once"}, {"id": "deny", "label": "Deny"}], tool=tool, inputs=inputs)
            else:
                await self.write({"type": "control_response", "response": {"subtype": "error", "request_id": data["request_id"],
                                  "error": "Unsupported client request"}})
        elif kind == "control_cancel_request":
            for token, (key, _) in list(self.requests.items()):
                if key == data.get("request_id"):
                    self.requests.pop(token)
                    self.emit("request_resolved", id=token)
        elif kind == "stream_event":
            event = data.get("event", {})
            if event.get("type") == "message_start":
                self.message_id = event.get("message", {}).get("id", self.turn)
            elif event.get("type") == "content_block_delta":
                delta = event.get("delta", {})
                role = "reasoning" if delta.get("type") == "thinking_delta" else "assistant"
                self.message(f"{self.message_id}:{role}", delta.get("text", delta.get("thinking", "")), role, True)
        elif kind == "assistant":
            message = data.get("message", {})
            mid = message.get("id", self.message_id)
            self.message(f"{mid}:assistant", text_content(message.get("content", [])))
            for part in message.get("content", []):
                if part.get("type") == "tool_use":
                    self.emit("tool", id=part["id"], title=part.get("name", "Tool"),
                              text=json.dumps(part.get("input", {}), ensure_ascii=False), status="running")
        elif kind == "user":
            for part in data.get("message", {}).get("content", []):
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    self.emit("tool", id=part.get("tool_use_id", "tool"), title="Tool result",
                              text=text_content(part.get("content", "")), status="error" if part.get("is_error") else "done")
        elif kind == "result":
            if data.get("is_error"):
                self.emit("error", text=data.get("result") or "\n".join(data.get("errors", [])) or "Claude turn failed")
            if data.get("permission_denials"):
                self.message(uuid.uuid4().hex, "Claude denied tools under its existing permission policy: " +
                             json.dumps(data["permission_denials"], ensure_ascii=False), "notice")
            self.status(False)

    async def reply(self, token, value):
        key, context = self.requests[token]
        if context["tool"] == "AskUserQuestion" and isinstance(value, dict) and not value.get("_cancel"):
            answers = validate_answers(context["questions"], value)
            answers = {k: ", ".join(v) if isinstance(v, list) else v for k, v in answers.items()}
            response = {"behavior": "allow", "updatedInput": {**context["inputs"], "answers": answers}}
        elif value == "allow":
            response = {"behavior": "allow", "updatedInput": context["inputs"]}
        else:
            response = {"behavior": "deny", "message": "User declined from Kate Agent Panel"}
        await self.write({"type": "control_response", "response": {"subtype": "success", "request_id": key, "response": response}})
        self.requests.pop(token, None)


class ZCodeAdapter(Adapter):
    def read_configuration(self, result):
        settings = result.get("settings", {})
        if settings:
            previous = getattr(self, "settings", {})
            merged = {**previous, **settings}
            if "model" in settings:
                merged["model"] = {**previous.get("model", {}), **settings["model"]}
                choices = {json.dumps(o.get("ref", {}), sort_keys=True): o
                           for o in previous.get("model", {}).get("available", []) + settings["model"].get("available", [])}
                merged["model"]["available"] = list(choices.values())
            self.settings = merged
        current = settings.get("model", {}).get("current", {})
        if current.get("modelId"):
            self.model = "/".join(str(current[k]) for k in ("providerId", "modelId") if current.get(k))
        effort = settings.get("thoughtLevel", {}).get("current", current.get("options", {}).get("reasoningLevel"))
        if effort is not None:
            self.effort = str(effort)
        elif settings.get("thoughtLevel", {}).get("enabled") is False:
            self.effort = ""
        self.configuration_ready()

    async def apply_configuration(self, model, effort):
        settings = getattr(self, "settings", {})
        if model and model != self.model:
            available = [o for o in settings.get("model", {}).get("available", []) if o.get("ref")]
            choices = [o for o in available if model in (o["ref"]["modelId"], o["ref"]["providerId"] + "/" + o["ref"]["modelId"])]
            if len(choices) != 1:
                raise ValueError("ZCode model is unavailable or ambiguous; enter an existing provider-id/model-id")
            selection = dict(choices[0]["ref"])
            current = settings.get("model", {}).get("current", {})
            same_model = all(current.get(k) == selection[k] for k in ("providerId", "modelId"))
            selected_effort = effort or (current.get("options", {}).get("reasoningLevel") if same_model else "") or choices[0].get("reasoning", {}).get("defaultLevel")
            if selected_effort:
                selection["options"] = {"reasoningLevel": selected_effort}
            result = await self.rpc("session/setModel", {"sessionId": self.session, "model": selection, "persistAsWorkspaceLastUsed": False})
            self.model, self.effort = model, selected_effort or ""
            self.read_configuration(result)
        elif effort and effort != self.effort:
            result = await self.rpc("session/setThoughtLevel", {"sessionId": self.session, "thoughtLevel": effort, "persistAsWorkspaceLastUsed": False})
            self.effort = effort
            self.read_configuration(result)

    async def connect(self):
        if not self.process or self.process.returncode is not None:
            await self.launch(["app-server", "--cwd", self.cwd])
            workspace = {"workspacePath": self.cwd, "workspaceKey": self.cwd}
            if self.session:
                result = await self.rpc("session/resume", {"sessionId": self.session, "workspace": workspace})
            else:
                result = await self.rpc("session/create", {"workspace": workspace, "mode": "build"})
            self.session = result["session"]["sessionId"]
            model, effort = self.starting_parameters()
            self.model = self.effort = ""
            self.read_configuration(result)
            await self.apply_configuration(model, effort)
            await self.rpc("session/subscribe", {"sessionId": self.session, "deliveryKind": "desktop-continuous",
                "afterSeq": result.get("runtime", {}).get("eventSeq", 0), "includeSnapshot": False})
            self.session_ready()

    async def configure(self, model, effort):
        await self.connect()
        try:
            await self.apply_configuration(model, effort)
        finally:
            self.session_ready()

    async def send(self, text):
        await self.connect()
        self.turn = uuid.uuid4().hex
        await self.rpc("session/send", {"sessionId": self.session, "content": text})

    async def handle(self, data):
        method, params = data.get("method", ""), data.get("params", {})
        if "id" in data and method:
            if method == "session/requestRuntimePreferences":
                await self.write({"id": data["id"], "result": {"nativeSearchEnhancementsEnabled": False,
                    "askUserQuestionAutoResolutionEnabled": False}})
            elif method == "interaction/requestPermission":
                choices = [{"id": str(i), "label": o.get("name", "Permission")} for i, o in enumerate(params.get("options", []))]
                await self.ask(data["id"], params.get("reason", "ZCode permission"), options=choices,
                    method=method, permissions=params.get("options", []))
            elif method == "interaction/requestUserInput":
                questions = [{**q, "id": q["question"], "required": True} for q in params.get("questions", [])]
                if not questions:
                    questions = [{"id": "answer", "question": params.get("prompt", "Answer"), "required": True}]
                await self.ask(data["id"], params.get("prompt", "ZCode question"), questions=questions, method=method)
            else:
                await self.write({"id": data["id"], "error": {"code": -32601, "message": "Client capability not supported"}})
            return
        if method == "session/event":
            data = params
            if self.session and data.get("sessionId", self.session) != self.session:
                return
        elif method == "state.updated":
            if params.get("sessionId", self.session) == self.session and isinstance(params.get("patch"), dict):
                self.read_configuration({"settings": params["patch"]})
            return
        elif method:
            if method == "startup/storageState" and params.get("phase") == "failed":
                self.emit("error", text=json.dumps(params.get("error", params), ensure_ascii=False))
            elif method not in ("session/state", "session/lifecycle/changed", "startup/storageState"):
                self.message(uuid.uuid4().hex, json.dumps(data, ensure_ascii=False), "diagnostic")
            return
        if data.get("sessionId"):
            self.session = data["sessionId"]
        kind = data.get("type")
        payload = data.get("payload", {})
        if kind == "state.updated":
            self.read_configuration(payload)
        elif kind == "model.streaming" and payload.get("kind") in ("text_delta", "reasoning_delta"):
            role = "reasoning" if payload["kind"] == "reasoning_delta" else "assistant"
            self.message(self.turn if role == "assistant" else self.turn + ":reasoning", payload.get("delta", ""), role, True)
        elif kind == "tool.updated":
            self.emit("tool", id=payload.get("toolCallId", payload.get("id", data.get("eventId", "tool"))),
                      title=payload.get("toolName", payload.get("name", "Tool")),
                      text=json.dumps(payload, ensure_ascii=False), status=payload.get("kind", "running"))
        elif kind == "turn.failed":
            self.emit("error", text=json.dumps(payload, ensure_ascii=False))
            self.status(False)
        elif kind in ("turn.completed", "turn.cancelled"):
            if payload.get("response"):
                self.message(self.turn, payload["response"])
            if payload.get("resultType", "").startswith("error_"):
                self.emit("error", text=payload["resultType"].replace("_", " "))
            self.status(False)
        elif kind not in ("message.upserted", "turn.started", "session.created", "session.resumed", "permission.requested", "userInput.requested"):
            # Keep evolving ZCode events inspectable rather than guessing a schema and losing data.
            self.message(uuid.uuid4().hex, json.dumps(data, ensure_ascii=False), "diagnostic")

    async def reply(self, token, value):
        key, context = self.requests[token]
        if context["method"] == "interaction/requestPermission":
            options = context["permissions"]
            selected = options[int(value)] if isinstance(value, str) and value.isdigit() and int(value) < len(options) else None
            result = selected.get("response", {"decision": "deny"}) if selected else {"decision": "deny"}
        elif isinstance(value, dict) and value.get("_cancel"):
            result = {"action": "cancel"}
        else:
            values = validate_answers(context["questions"], value)
            result = {"action": "accept", "content": {"answers": {k: ", ".join(map(str, v)) if isinstance(v, list) else v for k, v in values.items()}}}
        await self.write({"id": key, "result": result})
        self.requests.pop(token, None)


ADAPTERS = {"pi": PiAdapter, "hermes": ACPAdapter, "codex": CodexAdapter,
            "claude": ClaudeAdapter, "kimi": ACPAdapter, "zcode": ZCodeAdapter, "deepseek": ACPAdapter}


class Bridge:
    def __init__(self, emit=None, store=None):
        self.emit = emit or (lambda data: print(json.dumps(data, ensure_ascii=False), flush=True))
        self.adapter = None
        self.send_task = None
        self.store = store
        self.history = {}
        self.session_title = ""
        self.session_key = ""
        self.flush_handle = None
        self.native_cache = {}
        self.session_lock = asyncio.Lock()
        self.sessions_request = ""
        self.session_epoch = 0
        self.configuration_task = None

    def record(self, adapter, event):
        if adapter is not self.adapter or not self.store:
            return
        if event["type"] in ("message", "tool", "error", "turn_summary"):
            key = (event["type"], event.get("role", ""), event.get("id", uuid.uuid4().hex))
            previous = self.history.get(key, {})
            merged = dict(event)
            if event.get("append"):
                merged["text"] = (previous.get("text", "") + event.get("text", ""))[-256 * 1024:]
            merged["append"] = False
            self.history[key] = merged
            while len(self.history) > 200:
                self.history.pop(next(iter(self.history)))
        if event["type"] in ("session", "status") and not adapter.busy:
            self.flush()
        elif not self.flush_handle:
            self.flush_handle = asyncio.get_running_loop().call_later(.5, self.flush)

    def flush(self):
        if self.flush_handle:
            self.flush_handle.cancel(); self.flush_handle = None
        adapter = self.adapter
        if self.store and adapter and adapter.session and (self.history or self.session_title):
            try:
                self.session_key = self.store.save(dict(agent=adapter.agent, session=adapter.session, cwd=adapter.cwd,
                    model=adapter.model, effort=adapter.effort, title=self.session_title, updated=time.time()),
                    list(self.history.values()), select=True)
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                self.emit({"type": "error", "text": f"Could not save the panel session: {exc}"})

    async def sessions(self, command):
        if not self.store:
            self.store = SessionStore()
        directory = command.get("cwd", "")
        if not isinstance(directory, str) or not directory:
            raise ValueError("A working directory is required")
        cwd = canonical(directory)
        if not Path(cwd).is_dir():
            raise ValueError("Working directory does not exist")
        request_id = command.get("requestId", "")
        self.sessions_request = request_id
        epoch = self.session_epoch
        self.flush()
        cached = self.store.list(cwd)
        self.emit(dict(type="sessions", cwd=cwd, sessions=cached, requestId=request_id, loading=True, current=self.session_key))
        async def restore(items):
            if not command.get("autoResume") or self.adapter or self.sessions_request != request_id or self.session_epoch != epoch:
                return
            last = self.store.last(cwd)
            available = {a["id"] for a in inventory()["agents"] if a["available"]}
            selected = next((i for i in items if i["id"] == last and i["agent"] in available), None)
            if last is None:
                selected = next((i for i in items if i["agent"] in available), None)
            elif last and not selected:
                self.emit(dict(type="message", id=uuid.uuid4().hex, role="notice",
                    text="The previous session's CLI is unavailable. Select an installed agent or check Local Sessions."))
            if selected:
                async with self.session_lock:
                    if self.sessions_request == request_id and self.session_epoch == epoch:
                        await self.resume(dict(cwd=cwd, id=selected["id"], requestId=request_id))
        # A known choice (including New Chat) must not wait for CLI metadata scans.
        early = self.store.last(cwd) is not None
        if early:
            await restore(cached)
            self.emit(dict(type="sessions_complete", requestId=request_id, cwd=cwd))
        if time.monotonic() - self.native_cache.get(cwd, 0) > 10 or command.get("refresh"):
            discovered = [] if os.environ.get("KATE_AGENT_DISABLE_NATIVE_DISCOVERY") else await asyncio.to_thread(native_sessions, cwd, process_environment())
            # Current Kimi exposes a read-only metadata command; legacy versions
            # continue to work through their ACP session/list endpoint on demand.
            kimi = command_for("kimi")
            if kimi and not os.environ.get("KATE_AGENT_DISABLE_NATIVE_DISCOVERY"):
                process = None
                try:
                    process = await asyncio.create_subprocess_exec(*kimi, "session", "list", "--cwd", cwd, "--limit", "200", "--json",
                        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                        cwd=cwd, start_new_session=True, env=process_environment(), limit=MAX_RECORD)
                    stdout, _ = await asyncio.wait_for(process.communicate(), 3)
                    if process.returncode == 0:
                        data = json.loads(stdout)
                        for item in data if isinstance(data, list) else data.get("sessions", []):
                            if not isinstance(item, dict) or item.get("archived"):
                                continue
                            directory = item.get("cwd", item.get("workDir", item.get("workingDirectory", "")))
                            if isinstance(directory, str) and directory and canonical(directory) == cwd:
                                discovered.append(dict(agent="kimi", session=item.get("sessionId", item.get("id")), cwd=cwd,
                                    title=item.get("title") or item.get("lastPrompt") or item.get("sessionId", item.get("id", "")),
                                    source=item.get("sessionDir") or item.get("id", "kimi"),
                                    updated=timestamp(item.get("updatedAt", item.get("updated_at", 0)))))
                except (OSError, ValueError, TypeError, AttributeError, asyncio.TimeoutError):
                    pass
                finally:
                    if process and process.returncode is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        await process.wait()
            known = {(item["agent"], item["session"]): item for item in self.store.list(cwd)}
            for item in discovered:
                previous = known.get((item["agent"], item["session"]))
                if item.get("session") and (not previous or previous.get("source")):
                    self.store.save(item)
            self.native_cache[cwd] = time.monotonic()
        items = self.store.list(cwd)
        self.emit(dict(type="sessions", cwd=cwd, sessions=items, requestId=request_id, loading=False, current=self.session_key))
        if not early:
            await restore(items)
            self.emit(dict(type="sessions_complete", requestId=request_id, cwd=cwd))

    async def resume(self, command):
        if self.adapter and self.adapter.busy:
            raise ValueError("Stop the current turn before switching sessions")
        if not self.store:
            raise ValueError("Refresh Local Sessions before resuming")
        item = self.store.get(command["id"], command["cwd"])
        self.session_epoch += 1
        epoch = self.session_epoch
        history = item["history"] or await asyncio.to_thread(native_history, item)
        if self.session_epoch != epoch:
            return
        self.flush()
        if self.adapter:
            await self.adapter.close()
        if self.session_epoch != epoch:
            return
        self.adapter = ADAPTERS[item["agent"]](self, item["agent"], item["cwd"])
        self.adapter.session, self.adapter.model, self.adapter.effort = item["session"], item["model"], item["effort"]
        self.adapter.restore_native_settings = True
        self.session_key, self.session_title = item["id"], item["title"]
        self.history = {(e["type"], e.get("role", ""), e.get("id", str(i))): e for i, e in enumerate(history)}
        self.store.save(item, select=True)
        self.emit(dict(type="resumed", id=item["id"], agent=item["agent"], session=item["session"], cwd=item["cwd"],
            model=item["model"], effort=item["effort"], title=item["title"], history=history, requestId=command.get("requestId", "")))
        self.adapter.status(False, "Resumed — ready")

    async def send(self, command):
        self.session_epoch += 1
        agent = command.get("agent", "")
        if not isinstance(agent, str) or agent not in ADAPTERS:
            raise ValueError("Agent protocol is not supported yet")
        directory = command.get("cwd", "")
        if not isinstance(directory, str) or not directory:
            raise ValueError("A working directory is required")
        cwd = str(Path(directory).expanduser().resolve())
        if not Path(cwd).is_dir():
            raise ValueError("Working directory does not exist")
        text = command.get("text", "")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("A message or pending context is required")
        if self.adapter and self.adapter.busy:
            self.emit({"type": "error", "text": "Agent is busy; keep new context in Pending until this turn finishes"})
            return
        if self.adapter and (self.adapter.agent != agent or self.adapter.cwd != cwd):
            self.flush()
            await self.adapter.close()
            self.adapter = None
            self.history.clear(); self.session_title = ""; self.session_key = ""
        if self.adapter is None:
            self.adapter = ADAPTERS[agent](self, agent, cwd)
        model, effort = command.get("model", self.adapter.model), command.get("effort", self.adapter.effort)
        if not isinstance(model, str) or not isinstance(effort, str) or len(model) > 512 or len(effort) > 128:
            raise ValueError("Invalid model or reasoning effort")
        if (model, effort) != (self.adapter.model, self.adapter.effort):
            if self.adapter.process and self.adapter.process.returncode is None:
                await self.adapter.configure(model.strip(), effort.strip())
            else:
                self.adapter.model, self.adapter.effort = model.strip(), effort.strip()
        if not self.session_title:
            self.session_title = command.get("displayText", text).strip()[:160] or "Editor context"
        self.record(self.adapter, dict(type="message", role="user", id=uuid.uuid4().hex, text=command.get("displayText", text), attachments=command.get("attachments", [])))
        self.adapter.status(True)
        await self.adapter.send(text)

    async def configuration(self, command):
        async with self.session_lock:
            agent, directory = command.get("agent"), command.get("cwd")
            if not isinstance(agent, str) or agent not in ADAPTERS:
                raise ValueError("Agent protocol is not supported yet")
            if not isinstance(directory, str) or not directory or not Path(directory).expanduser().is_dir():
                raise ValueError("Choose an existing working directory")
            cwd = canonical(directory)
            if self.adapter and self.adapter.busy:
                raise ValueError("Stop the current turn before changing its configuration")
            model, effort = command.get("model", ""), command.get("effort", "")
            if not isinstance(model, str) or not isinstance(effort, str) or len(model) > 512 or len(effort) > 128:
                raise ValueError("Invalid model or reasoning effort")
            self.session_epoch += 1
            if self.adapter and (self.adapter.agent != agent or self.adapter.cwd != cwd):
                self.flush()
                await self.adapter.close()
                self.adapter = None
                self.history.clear(); self.session_title = ""; self.session_key = ""
            if self.adapter is None:
                self.adapter = ADAPTERS[agent](self, agent, cwd)
            adapter = self.adapter
            adapter.configuration_id = command.get("requestId", "")
            try:
                if command.get("type") == "configure":
                    await adapter.configure(model.strip(), effort.strip())
                else:
                    await adapter.connect()
            finally:
                adapter.configuration_ready()
                self.flush()

    async def dispatch(self, command):
        try:
            kind = command.get("type")
            if kind == "discover":
                self.emit(inventory())
            elif kind == "send":
                if self.configuration_task and not self.configuration_task.done():
                    await asyncio.shield(self.configuration_task)
                await self.send(command)
            elif kind == "sessions":
                await self.sessions(command)
            elif kind in ("prepare", "configure"):
                await self.configuration(command)
            elif kind == "resume":
                async with self.session_lock:
                    await self.resume(command)
            elif kind in ("cancel", "reset"):
                self.sessions_request = ""
                self.session_epoch += 1
                if self.configuration_task and not self.configuration_task.done():
                    self.configuration_task.cancel()
                    await asyncio.gather(self.configuration_task, return_exceptions=True)
                had_adapter = self.adapter is not None
                if self.send_task and not self.send_task.done():
                    self.send_task.cancel()
                    await asyncio.gather(self.send_task, return_exceptions=True)
                if self.adapter:
                    await self.adapter.cancel()
                    self.flush()
                    if kind == "reset":
                        if self.store and command.get("forgetLast", True):
                            self.store.forget_last(self.adapter.cwd)
                            if command.get("cwd"):
                                self.store.forget_last(command["cwd"])
                        self.adapter = None
                        self.history.clear(); self.session_title = ""; self.session_key = ""
                elif kind == "reset" and self.store and command.get("forgetLast") and command.get("cwd"):
                    self.store.forget_last(command["cwd"])
                self.emit({"type": "requests_cleared"})
                if not had_adapter:
                    self.emit({"type": "status", "busy": False, "text": "Ready"})
            elif kind == "reply" and self.adapter:
                await self.adapter.reply(command["id"], command.get("value", ""))
                self.adapter.emit("request_resolved", id=command["id"])
            else:
                raise ValueError("Unknown bridge command")
        except (ValueError, KeyError, TypeError, AttributeError, OSError, RuntimeError, sqlite3.Error, asyncio.TimeoutError) as exc:
            self.emit({"type": "error", "text": str(exc) or "Agent initialization timed out", "requestId": command.get("requestId", ""), "operation": command.get("type", "")})
            if command.get("type") == "reply":
                self.emit({"type": "request_failed", "id": command.get("id", ""), "text": str(exc)})
            if self.adapter and (command.get("type") == "prepare"
                    or command.get("type") == "configure" and not self.adapter.connected):
                await self.adapter.close()
            if command.get("type") == "send":
                if self.adapter:
                    if self.adapter.timing:
                        self.adapter.timing.failed = True
                    await self.adapter.close()
                    self.adapter.status(False)
                else:
                    self.emit({"type": "status", "busy": False, "text": "Ready"})
        finally:
            if command.get("type") in ("prepare", "configure"):
                self.emit(dict(type="configuration_complete", requestId=command.get("requestId", "")))

    async def run(self):
        reader = asyncio.StreamReader(limit=MAX_RECORD)
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)
        tasks = set()
        self.emit(inventory())
        try:
            while line := await reader.readline():
                try:
                    data = json.loads(line)
                    if not isinstance(data, dict):
                        raise ValueError("Expected a JSON object")
                except ValueError as exc:
                    self.emit({"type": "error", "text": str(exc)})
                    continue
                # Finish teardown before admitting the next prepare/send. Otherwise a
                # buffered reset can accidentally cancel the configuration task after it.
                if data.get("type") in ("reset", "cancel"):
                    await self.dispatch(data)
                    continue
                if data.get("type") == "send" and self.send_task and not self.send_task.done():
                    self.emit({"type": "error", "text": "A turn is already running"})
                    continue
                if data.get("type") in ("prepare", "configure") and self.configuration_task and not self.configuration_task.done():
                    self.configuration_task.cancel()
                    await asyncio.gather(self.configuration_task, return_exceptions=True)
                    if self.adapter:
                        await self.adapter.close()
                task = asyncio.create_task(self.dispatch(data))
                if data.get("type") == "send":
                    self.send_task = task
                elif data.get("type") in ("prepare", "configure"):
                    self.configuration_task = task
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.flush()
            if self.adapter:
                await self.adapter.close()


if __name__ == "__main__":
    asyncio.run(Bridge().run())
