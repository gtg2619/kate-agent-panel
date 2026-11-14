#!/usr/bin/env python3
"""Kate Agent Panel's stdlib-only JSONL transport. Never manages agent installs/configs."""
from __future__ import annotations

import asyncio
import codecs
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import sys
import uuid

MAX_RECORD = 16 * 1024 * 1024
AGENTS = {
    "pi": ("Pi", "pi", "rpc"),
    "hermes": ("Hermes", "hermes", "acp"),
    "codex": ("Codex", "codex", "app-server"),
    "claude": ("Claude Code", "claude", "stream-json"),
    "kimi": ("Kimi Code", "kimi", "acp"),
    "zcode": ("ZCode", "zcode", "stream-json"),
    "deepseek": ("DeepSeek Harness", "dsh", "acp"),
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
    dirs += sorted((home / ".nvm/versions/node").glob("*/bin"), reverse=True)
    env["PATH"] = os.pathsep.join([env.get("PATH", "")] + [str(p) for p in dirs if p.is_dir()])
    return env


def command_for(agent, env=None):
    env = env or process_environment()
    override = env.get(f"KATE_AGENT_{agent.upper()}_COMMAND", "")
    command = shlex.split(override) if override else [AGENTS[agent][1]]
    if not command:
        return []
    executable = shutil.which(command[0], path=env.get("PATH"))
    return [executable, *command[1:]] if executable else []


def inventory(env=None):
    env = env or process_environment()
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


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


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

    def emit(self, kind, **fields):
        self.bridge.emit(dict(type=kind, **fields))

    def message(self, id, text, role="assistant", append=False):
        if text:
            self.emit("message", id=f"{self.message_prefix}:{id}", text=text, role=role, append=append)

    def status(self, busy, text=""):
        self.busy = busy
        self.emit("status", busy=busy, text=text or ("Working" if busy else "Ready"))

    async def launch(self, args):
        command = command_for(self.agent)
        if not command:
            raise RuntimeError(f"{AGENTS[self.agent][0]} executable not found")
        self.closing = False
        self.process = await asyncio.create_subprocess_exec(
            *command, *args, cwd=self.cwd, env=process_environment(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=MAX_RECORD)
        self.tasks = [asyncio.create_task(self.read_stdout(self.process)), asyncio.create_task(self.read_stderr(self.process))]

    async def write(self, data):
        if not self.process or self.process.returncode is not None:
            raise RuntimeError("Agent process is not running")
        self.process.stdin.write(json.dumps(data, ensure_ascii=False).encode() + b"\n")
        await self.process.stdin.drain()

    async def rpc(self, method, params):
        self.serial += 1
        id = self.serial
        future = asyncio.get_running_loop().create_future()
        self.waiters[id] = future
        try:
            await self.write(dict(jsonrpc="2.0", id=id, method=method, params=params))
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
                            future.set_exception(RuntimeError(str(data["error"])))
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
        self.requests[token] = (key, context)
        self.emit("request", id=token, title=title, options=options or [], questions=questions or [])

    async def reply(self, token, value):
        raise RuntimeError("This adapter does not support replies")

    async def cancel(self):
        await self.close()
        self.status(False, "Stopped")

    async def close(self):
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


class PiAdapter(Adapter):
    async def send(self, text):
        if not self.process or self.process.returncode is not None:
            args = ["--mode", "rpc"]
            if self.session:
                args += ["--session", self.session]
            await self.launch(args)
            await self.write({"type": "get_state", "id": "state"})
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
            if not data.get("success", True):
                self.emit("error", text=data.get("error", "Pi command failed"))
                self.status(False)
            if data.get("command") == "get_state":
                self.session = data.get("data", {}).get("sessionFile", "")
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
        key, context = self.requests.pop(token)
        response = dict(type="extension_ui_response", id=key)
        if context["method"] == "confirm":
            response["confirmed"] = value == "yes"
        else:
            response["value"] = str(value)
        await self.write(response)


class ACPAdapter(Adapter):
    def __init__(self, *args):
        super().__init__(*args)
        self.tools = {}

    async def send(self, text):
        if not self.process or self.process.returncode is not None:
            if self.agent == "deepseek" and not dsh_profile_ready(process_environment()):
                raise RuntimeError("DeepSeek Harness needs an existing ACP profile; automatic configuration is disabled")
            await self.launch(["--profile", "acp"] if self.agent == "deepseek" else ["acp"])
            hello = await self.rpc("initialize", {"protocolVersion": 1, "clientCapabilities": {},
                                        "clientInfo": {"name": "kate-agent-panel", "version": "0.1.0"}})
            if hello.get("protocolVersion") != 1:
                raise RuntimeError("Unsupported ACP protocol version; this panel requires version 1")
            if self.session and hello.get("agentCapabilities", {}).get("loadSession"):
                await self.rpc("session/load", {"sessionId": self.session, "cwd": self.cwd, "mcpServers": []})
            else:
                if self.session:
                    self.message(uuid.uuid4().hex, "This agent does not support reloading a stopped session; starting a new session.", "notice")
                result = await self.rpc("session/new", {"cwd": self.cwd, "mcpServers": []})
                self.session = result["sessionId"]
                self.tools.clear()
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
        elif method == "session/request_permission":
            choices = [{"id": o["optionId"], "label": o["name"]} for o in params.get("options", [])]
            await self.ask(data["id"], params.get("toolCall", {}).get("title", "Agent permission request"),
                           choices, allowed=[c["id"] for c in choices])
        elif "id" in data and method:
            await self.write(dict(jsonrpc="2.0", id=data["id"], error={"code": -32601, "message": "Client capability not supported"}))

    async def reply(self, token, value):
        key, context = self.requests.pop(token)
        outcome = {"outcome": "selected", "optionId": value} if value in context["allowed"] else {"outcome": "cancelled"}
        await self.write(dict(jsonrpc="2.0", id=key, result={"outcome": outcome}))


class CodexAdapter(Adapter):
    async def send(self, text):
        if not self.process or self.process.returncode is not None:
            await self.launch(["app-server"])
            await self.rpc("initialize", {"clientInfo": {"name": "kate_agent_panel", "title": "Kate Agent Panel", "version": "0.1.0"}})
            await self.write({"method": "initialized"})
            if self.session:
                result = await self.rpc("thread/resume", {"threadId": self.session, "cwd": self.cwd})
            else:
                result = await self.rpc("thread/start", {"cwd": self.cwd})
            self.session = result["thread"]["id"]
        result = await self.rpc("turn/start", {"threadId": self.session, "input": [{"type": "text", "text": text}]})
        self.turn = result["turn"]["id"]

    async def handle(self, data):
        method, p = data.get("method", ""), data.get("params", {})
        if "id" in data and method:
            if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                await self.ask(data["id"], p.get("command") or p.get("reason") or "Approve agent changes?",
                               [{"id": "accept", "label": "Allow once"}, {"id": "decline", "label": "Deny"}],
                               method=method)
            elif method == "item/tool/requestUserInput":
                await self.ask(data["id"], "Agent question", questions=p.get("questions", []), method=method)
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

    async def reply(self, token, value):
        key, context = self.requests.pop(token)
        if context["method"] == "item/tool/requestUserInput":
            answers = {k: {"answers": [str(v)]} for k, v in value.items()} if isinstance(value, dict) else {}
            result = {"answers": answers}
        else:
            result = {"decision": value if value in ("accept", "decline") else "decline"}
        await self.write({"id": key, "result": result})


class ClaudeAdapter(Adapter):
    async def send(self, text):
        self.turn = uuid.uuid4().hex
        self.message_id = self.turn
        if not self.process or self.process.returncode is not None:
            args = ["--print", "--verbose", "--output-format", "stream-json", "--include-partial-messages",
                    "--input-format", "stream-json", "--permission-prompt-tool", "stdio"]
            if self.session:
                args += ["--resume", self.session]
            await self.launch(args)
            key = uuid.uuid4().hex
            future = asyncio.get_running_loop().create_future()
            self.waiters[key] = future
            try:
                await self.write({"type": "control_request", "request_id": key,
                                  "request": {"subtype": "initialize", "hooks": None}})
                await asyncio.wait_for(future, 60)
            finally:
                self.waiters.pop(key, None)
        await self.write({"type": "user", "message": {"role": "user", "content": text},
                          "parent_tool_use_id": None, "session_id": self.session})

    async def handle(self, data):
        kind = data.get("type")
        if data.get("session_id"):
            self.session = data["session_id"]
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
                    questions = [{**q, "id": q.get("question", str(i))} for i, q in enumerate(inputs.get("questions", []))]
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
        key, context = self.requests.pop(token)
        if context["tool"] == "AskUserQuestion" and isinstance(value, dict):
            response = {"behavior": "allow", "updatedInput": {**context["inputs"], "answers": value}}
        elif value == "allow":
            response = {"behavior": "allow", "updatedInput": context["inputs"]}
        else:
            response = {"behavior": "deny", "message": "User declined from Kate Agent Panel"}
        await self.write({"type": "control_response", "response": {"subtype": "success", "request_id": key, "response": response}})


class ZCodeAdapter(Adapter):
    def __init__(self, *args):
        super().__init__(*args)
        self.result_received = False

    async def send(self, text):
        if self.process:
            if self.process.returncode is None:
                await asyncio.wait_for(self.process.wait(), 10)
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.result_received = False
        self.turn = uuid.uuid4().hex
        args = ["--prompt", text, "--cwd", self.cwd, "--mode", "build", "--output-format", "stream-json"]
        if self.session:
            args += ["--resume", self.session]
        await self.launch(args)
        self.process.stdin.close()

    def process_exited(self, code):
        if code == 0 and self.result_received:
            self.status(False)
        else:
            super().process_exited(code)

    async def handle(self, data):
        if data.get("sessionId"):
            self.session = data["sessionId"]
        kind = data.get("type")
        payload = data.get("payload", {})
        if kind == "model.streaming" and payload.get("kind") in ("text_delta", "reasoning_delta"):
            role = "reasoning" if payload["kind"] == "reasoning_delta" else "assistant"
            self.message(self.turn if role == "assistant" else self.turn + ":reasoning", payload.get("delta", ""), role, True)
        elif kind == "tool.updated":
            self.emit("tool", id=payload.get("toolCallId", payload.get("id", data.get("eventId", "tool"))),
                      title=payload.get("toolName", payload.get("name", "Tool")),
                      text=json.dumps(payload, ensure_ascii=False), status=payload.get("kind", "running"))
        elif kind == "turn.failed":
            self.emit("error", text=json.dumps(payload, ensure_ascii=False))
        elif kind == "permission.requested":
            self.message(uuid.uuid4().hex, "ZCode requested tool permission. Its prompt transport cannot accept interactive approvals; "
                         "the CLI will enforce its build-mode permission policy.\n" + json.dumps(payload, ensure_ascii=False), "notice")
        elif kind == "result":
            result = data.get("result", data)
            if isinstance(result, dict):
                self.session = result.get("sessionId", self.session)
                self.message(self.turn, result.get("response", ""))
            else:
                self.message(self.turn, str(result))
            # Session persistence/cleanup may continue after this record. Keep the
            # turn busy until EOF and exit, so a quick reply cannot race the old reader.
            self.result_received = True
        elif kind not in ("message.upserted", "turn.started", "turn.completed", "session.created", "session.resumed"):
            # Keep evolving ZCode events inspectable rather than guessing a schema and losing data.
            self.message(uuid.uuid4().hex, json.dumps(data, ensure_ascii=False), "diagnostic")


ADAPTERS = {"pi": PiAdapter, "hermes": ACPAdapter, "codex": CodexAdapter,
            "claude": ClaudeAdapter, "kimi": ACPAdapter, "zcode": ZCodeAdapter, "deepseek": ACPAdapter}


class Bridge:
    def __init__(self, emit=None):
        self.emit = emit or (lambda data: print(json.dumps(data, ensure_ascii=False), flush=True))
        self.adapter = None
        self.send_task = None

    async def send(self, command):
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
            await self.adapter.close()
            self.adapter = None
        if self.adapter is None:
            self.adapter = ADAPTERS[agent](self, agent, cwd)
        self.adapter.status(True)
        await self.adapter.send(text)

    async def dispatch(self, command):
        try:
            kind = command.get("type")
            if kind == "discover":
                self.emit(inventory())
            elif kind == "send":
                await self.send(command)
            elif kind in ("cancel", "reset"):
                if self.send_task and not self.send_task.done():
                    self.send_task.cancel()
                    await asyncio.gather(self.send_task, return_exceptions=True)
                if self.adapter:
                    await self.adapter.cancel()
                    if kind == "reset":
                        self.adapter = None
                self.emit({"type": "requests_cleared"})
            elif kind == "reply" and self.adapter:
                await self.adapter.reply(command["id"], command.get("value", ""))
                self.emit({"type": "request_resolved", "id": command["id"]})
            else:
                raise ValueError("Unknown bridge command")
        except (ValueError, KeyError, TypeError, AttributeError, OSError, RuntimeError, asyncio.TimeoutError) as exc:
            self.emit({"type": "error", "text": str(exc) or "Agent initialization timed out"})
            if command.get("type") == "send" and self.adapter:
                await self.adapter.close()
                self.adapter.status(False)

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
                if data.get("type") == "send" and self.send_task and not self.send_task.done():
                    self.emit({"type": "error", "text": "A turn is already running"})
                    continue
                task = asyncio.create_task(self.dispatch(data))
                if data.get("type") == "send":
                    self.send_task = task
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            if self.adapter:
                await self.adapter.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(Bridge().run())
