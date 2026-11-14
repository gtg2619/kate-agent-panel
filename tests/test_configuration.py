"""Effective CLI configuration, idle edits, and client-observed turn timings."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_bridge import module, ROOT
from session_store import SessionStore


class ConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_preparation_reaps_process_and_can_retry(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            'KATE_AGENT_KIMI_COMMAND': shlex.join([sys.executable, str(ROOT / 'tests/adversarial_agent.py'), 'acp']),
            'FAKE_ACP_VERSION': '99',
        }):
            events = []; bridge = module.Bridge(events.append)
            command = dict(type='prepare', agent='kimi', cwd=folder, requestId='retry')
            try:
                await bridge.dispatch(command)
                failed = bridge.adapter.process
                self.assertIsNotNone(failed.returncode)
                os.environ['FAKE_ACP_VERSION'] = '1'
                await bridge.dispatch(command)
                self.assertIsNot(bridge.adapter.process, failed)
                self.assertTrue(bridge.adapter.connected)
            finally:
                if bridge.adapter: await bridge.adapter.close()

    async def test_pi_prepare_edit_and_resume_report_native_values_without_prompt(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            'KATE_AGENT_PI_COMMAND': shlex.join([sys.executable, str(ROOT / 'tests/fake_agent.py')]),
            'KATE_AGENT_STATE_HOME': folder + '/state',
        }):
            events = []
            bridge = module.Bridge(events.append, SessionStore(Path(folder) / 'state'))
            base = dict(agent='pi', cwd=folder, requestId='current')
            try:
                await bridge.dispatch(dict(type='prepare', **base))
                a = bridge.adapter
                process, session = a.process, a.session
                self.assertEqual((a.model, a.effort), ('fake-model', 'off'))
                self.assertFalse(any(e['type'] in ('message', 'turn_summary') for e in events))
                self.assertEqual(bridge.store.list(folder), [])
                await bridge.dispatch(dict(type='configure', model='other-model', effort='high', **base))
                self.assertIs(a.process, process)
                self.assertEqual((a.session, a.model, a.effort), (session, 'other-model', 'high'))
                await bridge.dispatch(dict(type='send', text='Remember this', **base))
                for _ in range(100):
                    if not a.busy: break
                    await asyncio.sleep(.01)
                self.assertFalse(a.busy)
                bridge.flush()
                item = bridge.store.get(bridge.session_key, folder)
                self.assertEqual((item['model'], item['effort']), ('other-model', 'high'))
                self.assertEqual(sum(e['type'] == 'turn_summary' for e in item['history']), 1)
                await bridge.dispatch(dict(type='configure', model='nonexistent', effort='high', **base))
                self.assertEqual((a.model, a.effort), ('other-model', 'high'))
                self.assertTrue(any(e['type'] == 'error' and e.get('operation') == 'configure' for e in events))
                self.assertEqual(events[-1]['type'], 'configuration_complete')
                # A native session edited outside Kate takes precedence over the panel's cache.
                (Path(folder) / 'state/fake-native.json').write_text(json.dumps(dict(model='external-model', effort='low')))
                await bridge.dispatch(dict(type='resume', id=bridge.session_key, cwd=folder))
                await bridge.dispatch(dict(type='prepare', **base))
                self.assertEqual((bridge.adapter.model, bridge.adapter.effort), ('external-model', 'low'))
            finally:
                if bridge.adapter: await bridge.adapter.close()

    async def test_acp_uses_current_values_and_live_updates_and_keeps_session(self):
        events = []
        a = module.ACPAdapter(module.Bridge(events.append), 'deepseek', '/tmp')
        options = [dict(id='model', category='model', currentValue='["official","first"]',
                        options=[dict(group='Models', options=[dict(value='["official","first"]', name='First'),
                                                              dict(value='["official","second"]', name='Second')])]),
                   dict(id='reasoning_effort', category='thought_level', currentValue='high',
                        options=[dict(value=v, name=v) for v in ('off', 'high', 'max')])]
        async def launch(): a.process = SimpleNamespace(returncode=None)
        a.launch_acp = launch
        calls = []
        async def rpc(method, params):
            calls.append((method, params))
            if method == 'initialize': return dict(protocolVersion=1, agentCapabilities={})
            if method == 'session/set_config_option':
                option = next(o for o in options if o['id'] == params['configId'])
                if params['value'] == 'invalid': raise module.RPCError(dict(message='Unsupported effort'))
                option['currentValue'] = params['value']
            return dict(sessionId='native', configOptions=options)
        a.rpc = rpc
        with patch.object(module, 'dsh_profile_ready', return_value=True): await a.connect()
        self.assertEqual((a.model, a.effort), ('official/first', 'high'))
        self.assertFalse(any(m == 'session/set_config_option' for m, _ in calls))
        await a.configure('official/second', 'max')
        self.assertEqual((a.model, a.effort, a.session), ('official/second', 'max', 'native'))
        with self.assertRaises(module.RPCError): await a.configure(a.model, 'invalid')
        self.assertEqual(a.effort, 'max')
        options[1]['currentValue'] = 'off'
        await a.handle(dict(method='session/update', params=dict(update=dict(sessionUpdate='config_option_update', configOptions=options))))
        self.assertEqual(events[-1]['effort'], 'off')
        self.assertEqual(sum(m == 'session/new' for m, _ in calls), 1)

    async def test_codex_reports_thread_response_defaults_and_applies_effort_at_creation(self):
        events = []
        a = module.CodexAdapter(module.Bridge(events.append), 'codex', '/tmp')
        async def launch(args): a.process = SimpleNamespace(returncode=None)
        a.launch, a.write = launch, AsyncMock()
        a.rpc = AsyncMock(side_effect=[{}, dict(thread=dict(id='thread'), model='native-model', reasoningEffort='high')])
        await a.connect()
        self.assertEqual((a.model, a.effort), ('native-model', 'high'))
        self.assertEqual(events[-1]['type'], 'configuration')

    async def test_legacy_acp_and_claude_init_update_model(self):
        events = []
        a = module.ACPAdapter(module.Bridge(events.append), 'hermes', '/tmp')
        a.read_configuration(dict(models=dict(currentModelId='legacy-model')))
        self.assertEqual(a.model, 'legacy-model')
        c = module.ClaudeAdapter(module.Bridge(events.append), 'claude', '/tmp')
        await c.handle(dict(type='system', subtype='init', model='resolved-model', session_id='session'))
        self.assertEqual(c.model, 'resolved-model')

    async def test_zcode_settings_notifications_refresh_fields_without_losing_catalog(self):
        a = module.ZCodeAdapter(module.Bridge(lambda e: None), 'zcode', '/tmp')
        a.session = 'session'
        refs = [dict(providerId='provider', modelId=name) for name in ('first', 'second')]
        a.read_configuration(dict(settings=dict(model=dict(current=refs[0], available=[dict(ref=r) for r in refs]),
                                                thoughtLevel=dict(current='high', enabled=True))))
        await a.handle(dict(method='state.updated', params=dict(sessionId='session', patch=dict(
            model=dict(current=refs[1], available=[dict(ref=refs[1])]), thoughtLevel=dict(enabled=False)))))
        self.assertEqual((a.model, a.effort), ('provider/second', ''))
        self.assertEqual(len(a.settings['model']['available']), 2)
        await a.handle(dict(method='state.updated', params=dict(sessionId='other', patch=dict(model=dict(current=refs[0])))))
        self.assertEqual(a.model, 'provider/second')

    async def test_codex_recreates_only_unsaved_empty_threads_on_edit(self):
        a = module.CodexAdapter(module.Bridge(lambda e: None), 'codex', '/tmp')
        async def launch(args): a.process = SimpleNamespace(returncode=None)
        async def close(): a.process = None
        calls = []
        async def rpc(method, params):
            calls.append((method, params))
            if method == 'initialize': return {}
            return dict(thread=dict(id=params.get('threadId', 'new-thread')), model='model',
                        reasoningEffort=params.get('config', {}).get('model_reasoning_effort', 'high'))
        a.launch, a.close, a.rpc, a.write = launch, close, rpc, AsyncMock()
        await a.connect()
        await a.configure('model', 'low')
        self.assertEqual(sum(m == 'thread/start' for m, _ in calls), 2)
        a.empty_thread = False
        await a.configure('model', 'high')
        self.assertEqual(calls[-1][0], 'thread/resume')
        self.assertEqual(calls[-1][1]['config'], dict(model_reasoning_effort='high'))


class TimingTests(unittest.TestCase):
    def test_non_overlapping_tools_and_user_wait_sum_to_total(self):
        now = [0.0]
        timing = module.TurnTiming(lambda: now[0])
        def at(second, kind, **fields):
            now[0] = second; timing.observe(kind, fields)
        at(2, 'message', role='reasoning')
        at(5, 'tool', id='one', status='running')
        at(6, 'tool', id='two', status='running')
        at(7, 'tool', id='one', status='done')
        at(8, 'request', id='permission')
        at(11, 'request_resolved', id='permission')
        at(12, 'tool', id='two', status='done')
        at(13, 'message', role='assistant')
        now[0] = 15
        summary = timing.finish()
        self.assertEqual(summary['elapsed'], 15)
        self.assertEqual(summary['durations'], dict(waiting=3, reasoning=3, tools=4, user=3, response=2))
        self.assertEqual(sum(summary['durations'].values()), summary['elapsed'])

    def test_zero_duration_error_and_stop(self):
        timing = module.TurnTiming(lambda: 1)
        timing.observe('error', dict(text='failure'))
        self.assertEqual(timing.finish()['outcome'], 'failed')
        self.assertEqual(timing.finish(True)['outcome'], 'stopped')
        self.assertEqual(timing.finish()['elapsed'], 0)

    def test_completed_status_emits_one_summary_per_turn(self):
        events = []
        a = module.Adapter(module.Bridge(events.append), 'pi', '/tmp')
        a.status(True); a.status(True); a.status(False); a.status(False)
        self.assertEqual(sum(e['type'] == 'turn_summary' for e in events), 1)
