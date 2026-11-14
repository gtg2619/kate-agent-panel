"""Lifecycle contracts inspired by ACP-TCK, Zed, and Avante's ACP integration."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_bridge import module, ROOT


def fixture(mode):
    return shlex.join([sys.executable, str(ROOT / 'tests/adversarial_agent.py'), mode])


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.bridge = module.Bridge(self.events.append)
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            'KATE_AGENT_KIMI_COMMAND': fixture('acp'),
            'KATE_AGENT_HERMES_COMMAND': fixture('acp'),
            'KATE_AGENT_DEEPSEEK_COMMAND': fixture('acp'),
            'DSH_HOME': self.temp.name,
        })
        self.env.start()
        profile = Path(self.temp.name, 'profiles/acp/cordis.yml')
        profile.parent.mkdir(parents=True); profile.write_text('[]')

    async def asyncTearDown(self):
        if self.bridge.send_task:
            self.bridge.send_task.cancel()
            await asyncio.gather(self.bridge.send_task, return_exceptions=True)
        if self.bridge.adapter:
            await self.bridge.adapter.close()
        self.env.stop(); self.temp.cleanup()

    async def until(self, predicate):
        async def loop():
            while not predicate():
                await asyncio.sleep(.01)
        await asyncio.wait_for(loop(), 5)

    async def send(self, text, agent='kimi', cwd=None):
        await asyncio.wait_for(self.bridge.dispatch({'type': 'send', 'agent': agent,
            'cwd': cwd or self.temp.name, 'text': text}), 5)

    async def test_each_acp_agent_preserves_session_across_turns(self):
        for agent in ('kimi', 'hermes', 'deepseek'):
            with self.subTest(agent=agent):
                await self.send('first', agent)
                adapter = self.bridge.adapter
                process, session = adapter.process, adapter.session
                await self.send('second', agent)
                self.assertIs(adapter.process, process)
                self.assertEqual(adapter.session, session)
                self.assertTrue(any(e.get('text') == session + ':second' for e in self.events))
                self.assertFalse(any(e['type'] == 'error' for e in self.events))

    async def test_stop_and_resume_negotiates_load_session(self):
        await self.send('first')
        previous = self.bridge.adapter.session
        process = self.bridge.adapter.process
        await self.bridge.dispatch({'type': 'cancel'})
        self.assertIsNotNone(process.returncode)
        await self.send('resumed')
        self.assertEqual(self.bridge.adapter.session, previous)
        self.assertIsNot(self.bridge.adapter.process, process)
        self.assertTrue(any(e.get('text') == previous + ':resumed' for e in self.events))

    async def test_unsupported_reload_does_not_silently_replace_a_session(self):
        with patch.dict(os.environ, {'FAKE_ACP_RELOAD': '0'}):
            await self.send('first')
            previous = self.bridge.adapter.session
            await self.bridge.dispatch({'type': 'cancel'})
            await self.send('again')
            self.assertEqual(self.bridge.adapter.session, previous)
            self.assertTrue(any(e['type'] == 'error' and 'cannot resume' in e.get('text', '') for e in self.events))

    async def test_agent_and_workspace_changes_close_the_previous_process(self):
        await self.send('first')
        first = self.bridge.adapter.process
        await self.send('switched', 'hermes')
        self.assertIsNotNone(first.returncode)
        second = self.bridge.adapter.process
        folder = Path(self.temp.name, 'next'); folder.mkdir()
        await self.send('moved', 'hermes', str(folder))
        self.assertIsNotNone(second.returncode)
        self.assertEqual(self.bridge.adapter.cwd, str(folder))

    async def test_cancel_pending_permission_never_runs_tool_and_late_reply_is_rejected(self):
        task = asyncio.create_task(self.bridge.dispatch({'type':'send','agent':'kimi','cwd':self.temp.name,'text':'permit'}))
        self.bridge.send_task = task
        await self.until(lambda: any(e['type'] == 'request' for e in self.events))
        request = next(e for e in self.events if e['type'] == 'request')
        self.assertFalse(Path(self.temp.name, 'approved').exists())
        await self.bridge.dispatch({'type': 'cancel'})
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        self.assertFalse(self.bridge.adapter.requests)
        self.assertFalse(self.bridge.adapter.busy)
        await self.bridge.dispatch({'type':'reply','id':request['id'],'value':'once'})
        self.assertFalse(Path(self.temp.name, 'approved').exists())
        await self.send('after-stop')
        self.assertTrue(any(e.get('text', '').endswith(':after-stop') for e in self.events))

    async def test_prompt_failure_closes_process_and_next_turn_recovers(self):
        await self.send('fail')
        self.assertTrue(any(e['type'] == 'error' and 'fixture refusal' in e['text'] for e in self.events))
        self.assertIsNotNone(self.bridge.adapter.process.returncode)
        await self.send('recovered')
        self.assertTrue(any(e.get('text', '').endswith(':recovered') for e in self.events))

    async def test_protocol_version_mismatch_is_not_silently_accepted(self):
        with patch.dict(os.environ, {'FAKE_ACP_VERSION': '99'}):
            await self.send('must-not-run')
        self.assertTrue(any(e['type'] == 'error' and 'version' in e['text'].lower() for e in self.events))
        self.assertFalse(any('must-not-run' in e.get('text', '') for e in self.events))

    async def test_malformed_rpc_result_is_reported_and_session_can_restart(self):
        with patch.dict(os.environ, {'FAKE_ACP_BAD_RESULT':'1'}):
            await self.send('must-not-run')
        self.assertTrue(any(e['type'] == 'error' and 'RPC result' in e['text'] for e in self.events))
        self.assertIsNotNone(self.bridge.adapter.process.returncode)
        await self.send('restarted')
        self.assertTrue(any(e.get('text', '').endswith(':restarted') for e in self.events))

    async def test_partial_tool_updates_preserve_title_input_and_status(self):
        adapter = module.ACPAdapter(self.bridge, 'kimi', self.temp.name)
        async def update(value):
            await adapter.handle({'method':'session/update','params':{'update':value}})
        await update({'sessionUpdate':'tool_call','toolCallId':'read','title':'Read source','status':'pending','rawInput':{'path':'src/a.cpp'}})
        await update({'sessionUpdate':'tool_call_update','toolCallId':'read','status':'in_progress'})
        tool = self.events[-1]
        self.assertEqual(tool['title'], 'Read source')
        self.assertIn('src/a.cpp', tool['text'])
        await update({'sessionUpdate':'tool_call_update','toolCallId':'read','content':[]})
        self.assertEqual(self.events[-1]['text'], '[]')
        self.assertEqual(self.events[-1]['status'], 'in_progress')

    async def test_unknown_requests_return_method_not_found(self):
        adapter = module.ACPAdapter(self.bridge, 'kimi', self.temp.name)
        replies = []
        async def write(value): replies.append(value)
        adapter.write = write
        for method in ('fs/write_text_file', 'terminal/create', 'future/extension'):
            await adapter.handle({'id': method, 'method': method, 'params': {}})
        self.assertEqual([r['error']['code'] for r in replies], [-32601] * 3)
        self.assertEqual([r['id'] for r in replies], ['fs/write_text_file', 'terminal/create', 'future/extension'])

    async def test_cancel_during_initialization_reaps_the_process(self):
        with patch.dict(os.environ, {'KATE_AGENT_KIMI_COMMAND': fixture('stall')}):
            task = asyncio.create_task(self.bridge.dispatch({'type':'send','agent':'kimi','cwd':self.temp.name,'text':'first'}))
            self.bridge.send_task = task
            await self.until(lambda: self.bridge.adapter and self.bridge.adapter.process)
            await self.bridge.dispatch({'type':'cancel'})
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        self.assertFalse(self.bridge.adapter.busy)
        self.assertIsNotNone(self.bridge.adapter.process.returncode)
        self.assertFalse(self.bridge.adapter.waiters)
        await self.send('reconnected')
        self.assertTrue(any(e.get('text', '').endswith(':reconnected') for e in self.events))

    async def test_exit_during_handshake_does_not_wait_for_timeout(self):
        with patch.dict(os.environ, {'KATE_AGENT_KIMI_COMMAND': fixture('exit')}):
            await self.send('first')
        self.assertTrue(any(e['type'] == 'error' for e in self.events))
        self.assertFalse(self.bridge.adapter.busy)
        self.assertFalse(self.bridge.adapter.waiters)
        await self.send('reconnected')
        self.assertTrue(any(e.get('text', '').endswith(':reconnected') for e in self.events))

    async def test_stdout_noise_is_diagnostic_and_does_not_break_transport(self):
        with patch.dict(os.environ, {'KATE_AGENT_PI_COMMAND': fixture('noise')}):
            await self.send('first', 'pi')
            await self.until(lambda: not self.bridge.adapter.busy)
        self.assertTrue(any(e.get('role') == 'diagnostic' and 'plain log' in e['text'] for e in self.events))
        self.assertFalse(any(e['type'] == 'error' for e in self.events))

    async def test_stderr_preserves_fragmented_utf8(self):
        with patch.dict(os.environ, {'KATE_AGENT_PI_COMMAND': fixture('stderr')}):
            await self.send('first', 'pi')
            await self.until(lambda: not self.bridge.adapter.busy)
        output = ''.join(e['text'] for e in self.events if e.get('role') == 'diagnostic')
        self.assertEqual(output, 'UTF-8: \u4e2d\u6587\n')

    async def test_oversized_record_fails_and_reaps_child(self):
        with patch.dict(os.environ, {'KATE_AGENT_PI_COMMAND': fixture('oversize')}), patch.object(module, 'MAX_RECORD', 1024):
            await self.send('first', 'pi')
            await self.until(lambda: not self.bridge.adapter.busy)
        self.assertTrue(any(e['type'] == 'error' for e in self.events))
        self.assertIsNotNone(self.bridge.adapter.process.returncode)

    async def test_sigterm_resistant_group_is_killed(self):
        with patch.dict(os.environ, {'KATE_AGENT_PI_COMMAND': fixture('stubborn')}):
            await self.send('first', 'pi')
            await self.until(lambda: any(e.get('text', '').startswith('child_pid=') for e in self.events))
            pid = int(next(e['text'].split('=')[1] for e in self.events if e.get('text', '').startswith('child_pid=')))
            await asyncio.wait_for(self.bridge.dispatch({'type':'cancel'}), 5)
        self.assertIsNotNone(self.bridge.adapter.process.returncode)
        status = Path(f'/proc/{pid}/stat')
        self.assertTrue(not status.exists() or status.read_text().split()[2] == 'Z')
