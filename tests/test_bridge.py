import asyncio
import importlib.util
import json
import os
from pathlib import Path
import signal
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('agent_bridge', ROOT / 'bridge/agent_bridge.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DiscoveryTests(unittest.TestCase):
    def test_dsh_is_first_even_when_unavailable(self):
        result = module.inventory({'PATH': ''})
        self.assertEqual(result['agents'][0]['id'], 'deepseek')
        self.assertFalse(any(agent['available'] for agent in result['agents']))

    def test_explicit_environment_does_not_inherit_overrides(self):
        with patch.dict(os.environ, {'KATE_AGENT_PI_COMMAND': sys.executable, 'OPENAI_API_KEY': 'ambient-secret'}), \
             patch.object(module, 'process_environment', side_effect=AssertionError('Unexpected inheritance')):
            result = module.inventory({})
        self.assertEqual(result['overrides'], [])
        self.assertEqual(result['environment'], [])

    def test_user_path_precedes_numeric_nvm_versions_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            custom = home / 'custom'; custom.mkdir()
            for version in ('v9.11.2', 'v24.9.0', 'v24.16.0', 'v22.15.0'):
                (home / '.nvm/versions/node' / version / 'bin').mkdir(parents=True)
            local = home / '.local/bin'; local.mkdir(parents=True)
            with patch.object(Path, 'home', return_value=home), \
                 patch.dict(os.environ, {'PATH': os.pathsep.join((str(custom), str(local), str(custom)))}, clear=True):
                paths = module.process_environment()['PATH'].split(os.pathsep)
            self.assertEqual(paths[:2], [str(custom), str(local)])
            self.assertEqual([Path(p).parent.name for p in paths[2:]], ['v24.16.0', 'v24.9.0', 'v22.15.0', 'v9.11.2'])
            with patch.object(Path, 'home', return_value=home), patch.dict(os.environ, {}, clear=True):
                paths = module.process_environment()['PATH'].split(os.pathsep)
            self.assertEqual(paths[:len(os.defpath.split(os.pathsep))], os.defpath.split(os.pathsep))
            self.assertNotIn('', paths)

    def test_invalid_override_does_not_break_other_agents(self):
        result = module.inventory({'PATH': '', 'KATE_AGENT_DEEPSEEK_COMMAND': '"unterminated',
                                   'KATE_AGENT_PI_COMMAND': shlex.quote(sys.executable)})
        self.assertIn('quotation', result['agents'][0]['error'])
        self.assertTrue(next(agent for agent in result['agents'] if agent['id'] == 'pi')['available'])

    def test_discovery_does_not_execute_commands_or_read_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory, 'executed')
            executable = Path(directory, 'fake agent')
            executable.write_text('#!/bin/sh\ntouch ' + shlex.quote(str(marker)) + '\n')
            executable.chmod(0o755)
            env = {'PATH':'/usr/bin', 'KATE_AGENT_PI_COMMAND':shlex.join([str(executable)])}
            with patch.object(Path, 'read_text', side_effect=AssertionError('Discovery read a file')):
                result = module.inventory(env)
            self.assertTrue(next(a for a in result['agents'] if a['id'] == 'pi')['available'])
            self.assertFalse(marker.exists())

    def test_credentials_never_leave_inventory(self):
        env = {'PATH': '/usr/bin', 'OPENAI_API_KEY': 'secret-value', 'DEEPSEEK_API_KEY': 'secret-2'}
        result = module.inventory(env)
        self.assertIn('OPENAI_API_KEY', result['environment'])
        self.assertNotIn('secret-value', json.dumps(result))
        self.assertNotIn('secret-2', json.dumps(result))

    def test_override_is_argv_not_shell(self):
        env = {'PATH': '/usr/bin', 'KATE_AGENT_PI_COMMAND': f'{sys.executable} "arg with spaces" "$(touch bad)"'}
        self.assertEqual(module.command_for('pi', env)[1:], ['arg with spaces', '$(touch bad)'])

    def test_deepseek_does_not_initialize_configuration(self):
        with tempfile.TemporaryDirectory() as home:
            env = {'PATH': '/usr/bin', 'DSH_HOME': home, 'KATE_AGENT_DEEPSEEK_COMMAND': sys.executable}
            info = next(a for a in module.inventory(env)['agents'] if a['id'] == 'deepseek')
            self.assertFalse(info['available'])
            self.assertEqual(list(Path(home).iterdir()), [])
            profile = Path(home) / 'profiles/acp/cordis.yml'
            profile.parent.mkdir(parents=True); profile.write_text('existing')
            self.assertTrue(module.dsh_profile_ready(env))


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.bridge = module.Bridge(self.events.append)

    async def test_codex_stream_and_approval(self):
        adapter = module.CodexAdapter(self.bridge, 'codex', '/tmp')
        sent = []
        async def write(record): sent.append(record)
        adapter.write = write
        await adapter.handle({'method': 'item/agentMessage/delta', 'params': {'itemId': 'a', 'delta': 'hello'}})
        await adapter.handle({'method': 'item/completed', 'params': {'item': {'id': 'a', 'type': 'agentMessage', 'text': 'hello world'}}})
        self.assertTrue(self.events[0]['append']); self.assertFalse(self.events[1]['append'])
        await adapter.handle({'id': 9, 'method': 'item/commandExecution/requestApproval', 'params': {'command': 'make'}})
        self.assertEqual(sent, [])  # Never automatically approve a request.
        await adapter.reply(self.events[-1]['id'], 'accept')
        self.assertEqual(sent[-1], {'id': 9, 'result': {'decision': 'accept'}})
        await adapter.handle({'method': 'turn/completed', 'params': {'turn': {'error': {'message': 'quota'}}}})
        self.assertEqual(self.events[-2]['text'], 'quota'); self.assertFalse(self.events[-1]['busy'])

    async def test_acp_permissions_and_tools(self):
        adapter = module.ACPAdapter(self.bridge, 'kimi', '/tmp')
        sent = []
        async def write(record): sent.append(record)
        adapter.write = write
        await adapter.handle({'method': 'session/update', 'params': {'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': '\u4e2d\u6587'}}}})
        self.assertEqual(self.events[-1]['text'], '\u4e2d\u6587')
        await adapter.handle({'id': 3, 'method': 'session/request_permission', 'params': {'toolCall': {'title': 'edit'}, 'options': [{'optionId': 'once', 'name': 'Allow once'}]}})
        await adapter.reply(self.events[-1]['id'], 'invented')
        self.assertEqual(sent[-1]['result']['outcome'], {'outcome': 'cancelled'})
        await adapter.handle({'id': 4, 'method': 'fs/read_text_file', 'params': {'path': '/etc/passwd'}})
        self.assertEqual(sent[-1]['error']['code'], -32601)

    async def test_pi_waits_for_settled_not_agent_end(self):
        adapter = module.PiAdapter(self.bridge, 'pi', '/tmp')
        adapter.busy = True
        await adapter.handle({'type': 'agent_end'})
        self.assertTrue(adapter.busy)
        await adapter.handle({'type': 'agent_settled'})
        self.assertFalse(adapter.busy)

    async def test_claude_final_replaces_stream_and_denials_visible(self):
        adapter = module.ClaudeAdapter(self.bridge, 'claude', '/tmp')
        adapter.message_id = 'a'
        await adapter.handle({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'hello'}}})
        await adapter.handle({'type': 'assistant', 'message': {'id': 'a', 'content': [{'type': 'text', 'text': 'hello'}]}})
        self.assertEqual(self.events[-1]['id'], self.events[-2]['id'])
        self.assertFalse(self.events[-1]['append'])
        await adapter.handle({'type': 'result', 'session_id': 'sess', 'permission_denials': [{'tool_name': 'Bash'}]})
        self.assertIn('Bash', self.events[-2]['text']); self.assertEqual(adapter.session, 'sess')

    async def test_zcode_keeps_unknown_events_and_session(self):
        adapter = module.ZCodeAdapter(self.bridge, 'zcode', '/tmp')
        adapter.busy = True
        adapter.session = 's1'
        await adapter.handle({'method':'session/event','params':{'sessionId':'s1','type': 'model.streaming', 'payload': {'kind': 'text_delta', 'delta': 'streamed'}}})
        self.assertTrue(self.events[-1]['append']); self.assertEqual(self.events[-1]['text'], 'streamed')
        await adapter.handle({'type': 'future-event', 'payload': 'progress'})
        self.assertIn('progress', self.events[-1]['text'])
        await adapter.handle({'method':'session/event','params':{'sessionId':'s1','type':'turn.completed','payload':{'response':'answer','resultType':'success'}}})
        self.assertEqual(adapter.session, 's1'); self.assertEqual(self.events[-2]['text'], 'answer')
        self.assertFalse(adapter.busy)

    async def test_codex_question_and_unrecognized_approval(self):
        adapter = module.CodexAdapter(self.bridge, 'codex', '/tmp')
        sent = []
        async def write(value): sent.append(value)
        adapter.write = write
        await adapter.handle({'id':'question','method':'item/tool/requestUserInput','params':{'questions':[{'id':'q','question':'Which file?'}]}})
        await adapter.reply(self.events[-1]['id'], {'q':'a file.cpp'})
        self.assertEqual(sent[-1], {'id':'question','result':{'answers':{'q':{'answers':['a file.cpp']}}}})
        await adapter.handle({'id':2,'method':'item/fileChange/requestApproval','params':{'reason':'Edit source'}})
        await adapter.reply(self.events[-1]['id'], 'invented approval')
        self.assertEqual(sent[-1]['result']['decision'], 'decline')

    async def test_claude_questions_denials_and_cancelled_requests(self):
        adapter = module.ClaudeAdapter(self.bridge, 'claude', '/tmp')
        sent = []
        async def write(value): sent.append(value)
        adapter.write = write
        inputs = {'questions':[{'question':'Which file?', 'options':[{'label':'a.cpp'}]}]}
        await adapter.handle({'type':'control_request','request_id':'q','request':{'subtype':'can_use_tool','tool_name':'AskUserQuestion','input':inputs}})
        await adapter.reply(self.events[-1]['id'], {'Which file?':'a.cpp'})
        self.assertEqual(sent[-1]['response']['response']['updatedInput'], {**inputs,'answers':{'Which file?':'a.cpp'}})
        await adapter.handle({'type':'control_request','request_id':'deny','request':{'subtype':'can_use_tool','tool_name':'Bash','input':{'command':'echo test'}}})
        await adapter.reply(self.events[-1]['id'], 'deny')
        self.assertEqual(sent[-1]['response']['response']['behavior'], 'deny')
        await adapter.handle({'type':'control_request','request_id':'cancel','request':{'subtype':'can_use_tool','tool_name':'Bash','input':{}}})
        token = self.events[-1]['id']; count = len(sent)
        await adapter.handle({'type':'control_cancel_request','request_id':'cancel'})
        self.assertNotIn(token, adapter.requests)
        with self.assertRaises(KeyError): await adapter.reply(token, 'allow')
        self.assertEqual(len(sent), count)

    async def test_pi_extension_decisions_and_multiline_input(self):
        adapter = module.PiAdapter(self.bridge, 'pi', '/tmp')
        sent = []
        async def write(value): sent.append(value)
        adapter.write = write
        for method, value in [('confirm','no'), ('select','First'), ('editor','line one\nline two')]:
            await adapter.handle({'type':'extension_ui_request','id':method,'method':method,'title':'Fixture','options':['First','Second']})
            self.assertIn('request', self.events[-1]['type'])
            await adapter.reply(self.events[-1]['id'], value)
        self.assertEqual(sent[0], {'type':'extension_ui_response','id':'confirm','confirmed':False})
        self.assertEqual(sent[1]['value'], 'First')
        self.assertEqual(sent[2]['value'], 'line one\nline two')


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.bridge = module.Bridge(self.events.append)
        self.env = patch.dict(os.environ, {'KATE_AGENT_PI_COMMAND': f'{sys.executable} {ROOT / "tests/fake_agent.py"}'})
        self.env.start()
        self.temp = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        if self.bridge.adapter: await self.bridge.adapter.close()
        self.temp.cleanup(); self.env.stop()

    async def wait_for(self, predicate):
        async def loop():
            while not predicate(): await asyncio.sleep(.01)
        await asyncio.wait_for(loop(), 5)

    async def send(self, text):
        await self.bridge.dispatch({'type': 'send', 'agent': 'pi', 'cwd': self.temp.name, 'text': text})

    async def test_fragmented_utf8_and_two_turns(self):
        text = 'literal $(touch injected) `code` \u2028 \u4e2d\u6587'
        await self.send(text)
        await self.wait_for(lambda: self.bridge.adapter and not self.bridge.adapter.busy)
        messages = [e['text'] for e in self.events if e['type'] == 'message']
        self.assertIn(text, messages); self.assertIn('Formula: ', messages)
        self.assertFalse(Path(self.temp.name, 'injected').exists())
        process = self.bridge.adapter.process
        await self.send('second')
        await self.wait_for(lambda: not self.bridge.adapter.busy)
        self.assertIs(self.bridge.adapter.process, process)
        self.assertEqual(self.bridge.adapter.session, 'fake-session')

    async def test_nonzero_exit_recovers(self):
        await self.send('crash')
        await self.wait_for(lambda: any(e['type'] == 'error' for e in self.events))
        self.assertTrue(any('code 7' in e.get('text', '') for e in self.events))
        await self.send('recovered')
        await self.wait_for(lambda: not self.bridge.adapter.busy)
        self.assertTrue(any(e.get('text') == 'recovered' for e in self.events))

    async def test_cancel_stops_process_group(self):
        await self.send('hang')
        await self.wait_for(lambda: any(e.get('text', '').startswith('child_pid=') for e in self.events))
        pid = int(next(e['text'].split('=')[1] for e in self.events if e.get('text', '').startswith('child_pid=')))
        await self.bridge.dispatch({'type': 'cancel'})
        self.assertIsNotNone(self.bridge.adapter.process.returncode)
        # A killed child can briefly remain a zombie until the OS reaps it.
        status = Path(f'/proc/{pid}/stat')
        self.assertTrue(not status.exists() or status.read_text().split()[2] == 'Z')

    async def test_failure_keeps_bridge_available(self):
        with patch.dict(os.environ, {'KATE_AGENT_PI_COMMAND': '/nonexistent'}): await self.send('hello')
        self.assertTrue(any(e['type'] == 'error' for e in self.events))
        self.assertFalse(self.bridge.adapter.busy)

    async def test_codex_subprocess_handshake_and_continuation(self):
        with patch.dict(os.environ, {'KATE_AGENT_CODEX_COMMAND': f'{sys.executable} {ROOT / "tests/fake_protocol_agent.py"} codex'}):
            await self.bridge.dispatch({'type': 'send', 'agent': 'codex', 'cwd': self.temp.name, 'text': 'hello'})
            await self.wait_for(lambda: not self.bridge.adapter.busy)
            process = self.bridge.adapter.process
            await self.bridge.dispatch({'type': 'send', 'agent': 'codex', 'cwd': self.temp.name, 'text': 'again'})
            await self.wait_for(lambda: not self.bridge.adapter.busy)
            self.assertIs(self.bridge.adapter.process, process)
            self.assertTrue(any(e.get('text') == 'answer 2' for e in self.events))

    async def test_zcode_reuses_server_after_turn_completed(self):
        with patch.dict(os.environ, {'KATE_AGENT_ZCODE_COMMAND': f'{sys.executable} {ROOT / "tests/fake_protocol_agent.py"} zcode'}):
            await self.bridge.dispatch({'type': 'send', 'agent': 'zcode', 'cwd': self.temp.name, 'text': 'first'})
            await self.wait_for(lambda: any(e.get('text') == 'first' for e in self.events))
            self.assertTrue(self.bridge.adapter.busy)
            await self.wait_for(lambda: not self.bridge.adapter.busy)
            process = self.bridge.adapter.process
            self.assertIsNone(process.returncode)
            await self.bridge.dispatch({'type': 'send', 'agent': 'zcode', 'cwd': self.temp.name, 'text': 'second'})
            await self.wait_for(lambda: not self.bridge.adapter.busy)
            self.assertTrue(any(e.get('text') == 'second' for e in self.events))
            self.assertIs(self.bridge.adapter.process, process)
            self.assertFalse(any(e['type'] == 'error' for e in self.events))

    async def test_acp_and_claude_bidirectional_permissions(self):
        for agent, mode, answer in [('kimi', 'acp', 'once'), ('claude', 'claude', 'allow')]:
            self.events.clear()
            with patch.dict(os.environ, {f'KATE_AGENT_{agent.upper()}_COMMAND': f'{sys.executable} {ROOT / "tests/fake_protocol_agent.py"} {mode}'}):
                task = asyncio.create_task(self.bridge.dispatch({'type': 'send', 'agent': agent, 'cwd': self.temp.name, 'text': 'hello'}))
                await self.wait_for(lambda: any(e['type'] == 'request' for e in self.events))
                request = next(e for e in self.events if e['type'] == 'request')
                self.assertTrue(self.bridge.adapter.busy)
                await self.bridge.dispatch({'type': 'reply', 'id': request['id'], 'value': answer})
                await asyncio.wait_for(task, 5)
                await self.wait_for(lambda: not self.bridge.adapter.busy)
                self.assertTrue(any(e.get('text') == 'approved answer' for e in self.events))
                await self.bridge.dispatch({'type': 'reset'})


if __name__ == '__main__': unittest.main()
