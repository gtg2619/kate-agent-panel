"""Read-only native imports, durable panel history, and workspace isolation."""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

from test_bridge import module
from session_store import SessionStore, canonical, native_sessions, native_history


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = self.root / 'workspace'; self.cwd.mkdir()
        self.store = SessionStore(self.root / 'panel')

    def item(self, session='one', **extra):
        return dict(agent='codex', session=session, cwd=str(self.cwd), title='Question', **extra)

    def test_exact_workspace_symlinks_and_restart(self):
        link = self.root / 'linked'; link.symlink_to(self.cwd, target_is_directory=True)
        history = [dict(type='message', id='a', role='assistant', text='Saved reply')]
        key = self.store.save(self.item(model='gpt-test', effort='high'), history, select=True)
        reopened = SessionStore(self.root / 'panel')
        self.assertEqual(reopened.last(link), key)
        self.assertEqual(reopened.get(key, link)['history'], history)
        self.assertEqual(reopened.list(link)[0]['effort'], 'high')
        self.assertEqual(reopened.list(self.root), [])
        with self.assertRaisesRegex(ValueError, 'working directory'):
            reopened.get(key, self.root)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_upsert_preserves_history_and_new_chat_intent(self):
        key = self.store.save(self.item(), [{'type': 'message', 'text': 'original'}], select=True)
        self.assertEqual(key, self.store.save(self.item(updated=12)))
        self.assertEqual(len(self.store.get(key, self.cwd)['history']), 1)
        self.store.forget_last(self.cwd)
        self.assertEqual(self.store.last(self.cwd), '')
        self.assertIsNone(self.store.last(self.root))

    def test_history_limits_and_concurrent_stores(self):
        self.store.save(self.item(), [{'type': 'message', 'text': str(i)} for i in range(300)])
        other = SessionStore(self.root / 'panel')
        self.assertEqual(other.get(other.list(self.cwd)[0]['id'], self.cwd)['history'][0]['text'], '100')
        other.save(self.item('second'))
        self.assertEqual(len(self.store.list(self.cwd)), 2)

    def test_history_size_is_limited_in_utf8_bytes(self):
        text = '\U0001f680' * (256 * 1024)
        key = self.store.save(self.item(), [{'type':'message','text':text} for _ in range(10)])
        history = self.store.get(key,self.cwd)['history']
        self.assertLessEqual(len(json.dumps(history,ensure_ascii=False).encode()), 8 * 1024 * 1024)
        self.assertGreater(len(history),0)

    def test_native_sources_filter_workspace_and_never_write(self):
        env = {key: str(self.root / key) for key in ('CODEX_HOME', 'PI_CODING_AGENT_DIR', 'CLAUDE_CONFIG_DIR', 'DSH_HOME', 'ZCODE_STORAGE_DIR', 'HERMES_HOME')}
        codex = Path(env['CODEX_HOME']); codex.mkdir()
        with closing(sqlite3.connect(codex / 'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE threads(id,cwd,title,updated_at,archived,source,rollout_path,model,reasoning_effort)')
            for id, cwd, archived, source in [('c1', str(self.cwd), 0, 'cli'), ('other', str(self.root), 0, 'cli'), ('archived', str(self.cwd), 1, 'cli'), ('child', str(self.cwd), 0, 'subagent')]:
                db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?)', (id,cwd,'Test',10,archived,source,'','model','low'))
        pi = Path(env['PI_CODING_AGENT_DIR']) / 'sessions/project/a.jsonl'; pi.parent.mkdir(parents=True)
        pi.write_text(json.dumps({'type':'session','cwd':str(self.cwd),'id':'pi'}) + '\n' + json.dumps({'type':'message','message':{'role':'user','content':'Pi title'}}))
        claude = Path(env['CLAUDE_CONFIG_DIR']) / 'projects/project/a.jsonl'; claude.parent.mkdir(parents=True)
        claude.write_text(json.dumps({'cwd':str(self.cwd),'sessionId':'cl1','message':{'role':'user','content':'Claude title'}}))
        dsh = Path(env['DSH_HOME']) / 'storages/session_projcache/sessions/d1.json'; dsh.parent.mkdir(parents=True)
        dsh.write_text(json.dumps({'record':{'identity':{'cwd':str(self.cwd)},'rows':{'title':'DSH title'}}}))
        zcode = Path(env['ZCODE_STORAGE_DIR']) / 'cli/db/db.sqlite'; zcode.parent.mkdir(parents=True)
        with closing(sqlite3.connect(zcode)) as db, db:
            db.execute('CREATE TABLE session(id,directory,title,time_updated,time_deleted,parent_id)')
            db.execute('INSERT INTO session VALUES(?,?,?,?,NULL,NULL)', ('z1',str(self.cwd),'ZCode title',10000))
        hermes = Path(env['HERMES_HOME']) / 'state.db'; hermes.parent.mkdir()
        with closing(sqlite3.connect(hermes)) as db, db:
            db.execute('CREATE TABLE sessions(id,source,started_at,ended_at,model_config,cwd,title)')
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?)', ('h1','acp',1,2,json.dumps({'cwd':str(self.cwd)}),None,'Hermes title'))
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(Path, 'home', return_value=self.root):
            items = native_sessions(self.cwd, env)
        self.assertEqual({i['agent'] for i in items}, {'codex','pi','claude','deepseek','zcode','hermes'})
        self.assertEqual([i['session'] for i in items if i['agent']=='codex'], ['c1'])
        self.assertEqual(native_history(next(i for i in items if i['agent']=='pi'))[0]['text'], 'Pi title')
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_corrupt_metadata_does_not_block_other_agents(self):
        codex = self.root / '.codex'; codex.mkdir()
        (codex / 'state_5.sqlite').write_text('not a database')
        dsh = self.root / '.dsh/storages/session_projcache/sessions'; dsh.mkdir(parents=True)
        (dsh / 'bad.json').write_text('{')
        pi = self.root / '.pi/agent/sessions/project'; pi.mkdir(parents=True)
        (pi / 'bad.jsonl').write_text('broken\n')
        with patch.object(Path, 'home', return_value=self.root):
            self.assertEqual(native_sessions(self.cwd, {}), [])


class SessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.cwd = self.temp.name
        self.store = SessionStore(Path(self.cwd) / 'panel')
        self.events = []
        self.bridge = module.Bridge(self.events.append, self.store)
        self.env = patch.dict(os.environ, {'KATE_AGENT_DISABLE_NATIVE_DISCOVERY':'1', 'KATE_AGENT_PI_COMMAND': 'python3'})
        self.env.start(); self.addCleanup(self.env.stop)

    async def asyncTearDown(self):
        self.bridge.flush()
        if self.bridge.adapter: await self.bridge.adapter.close()

    def saved(self, **extra):
        return self.store.save(dict(agent='pi',session='native-pi',cwd=self.cwd,title='Last conversation',model='provider/model',effort='low',**extra),
            [dict(type='message',id='old',role='assistant',text='Old answer')], select=True)

    async def test_auto_resume_is_lazy_and_restores_configuration(self):
        key = self.saved()
        with patch.object(module.Adapter, 'launch', side_effect=AssertionError('Eager CLI launch')):
            await self.bridge.dispatch(dict(type='sessions',cwd=self.cwd,requestId='list',autoResume=True))
        self.assertEqual(self.bridge.session_key, key)
        self.assertEqual(self.bridge.adapter.session, 'native-pi')
        self.assertEqual(self.bridge.adapter.model, 'provider/model')
        self.assertIsNone(self.bridge.adapter.process)
        self.assertEqual(next(e for e in self.events if e['type']=='resumed')['history'][0]['text'], 'Old answer')

    async def test_new_chat_remains_new_after_restart(self):
        self.saved()
        await self.bridge.dispatch(dict(type='reset',cwd=self.cwd,forgetLast=True))
        await self.bridge.dispatch(dict(type='sessions',cwd=self.cwd,autoResume=True))
        self.assertIsNone(self.bridge.adapter)
        self.assertEqual(len(self.store.list(self.cwd)), 1)

    async def test_cancelled_discovery_cannot_restore_a_stale_workspace(self):
        self.saved()
        self.store.forget_last(self.cwd)
        gate = asyncio.Event()
        async def scan(*args): await gate.wait(); return []
        with patch.dict(os.environ, {'KATE_AGENT_DISABLE_NATIVE_DISCOVERY':''}), patch.object(module.asyncio, 'to_thread', scan), patch.object(module, 'command_for', return_value=[]):
            task = asyncio.create_task(self.bridge.dispatch(dict(type='sessions',cwd=self.cwd,autoResume=True,requestId='old')))
            await asyncio.sleep(.02)
            await self.bridge.dispatch(dict(type='reset',cwd=self.cwd,forgetLast=True))
            gate.set(); await task
        self.assertIsNone(self.bridge.adapter)
        self.assertFalse(any(e['type']=='resumed' for e in self.events))

    async def test_cached_resume_is_ready_before_slow_native_scan(self):
        self.saved()
        started, release = asyncio.Event(), asyncio.Event()
        async def scan(*args): started.set(); await release.wait(); return []
        with patch.dict(os.environ, {'KATE_AGENT_DISABLE_NATIVE_DISCOVERY':''}), patch.object(module.asyncio, 'to_thread', scan), patch.object(module, 'command_for', return_value=[]):
            with patch.object(module, 'inventory', return_value={'agents':[{'id':'pi','available':True}]}):
                task = asyncio.create_task(self.bridge.dispatch(dict(type='sessions',cwd=self.cwd,autoResume=True,requestId='fast')))
                await asyncio.wait_for(started.wait(), 1)
                self.assertTrue(any(e['type']=='sessions_complete' for e in self.events))
                self.assertIsNotNone(self.bridge.adapter)
                release.set(); await task

    async def test_invalid_send_and_resume_release_busy_state(self):
        await self.bridge.dispatch(dict(type='send',agent='pi',cwd='/missing-directory',text='hello'))
        self.assertEqual(self.events[-1]['type'], 'status'); self.assertFalse(self.events[-1]['busy'])
        self.events.clear()
        await self.bridge.dispatch(dict(type='resume',id=self.saved(),cwd='/wrong-directory',requestId='bad'))
        self.assertEqual(self.events[-1]['operation'], 'resume')
        self.assertEqual(self.events[-1]['requestId'], 'bad')
        self.assertIsNone(self.bridge.adapter)

    async def test_unavailable_previous_cli_does_not_resume_another_cli(self):
        self.saved()
        with patch.object(module,'inventory',return_value={'agents':[{'id':'pi','available':False}]}):
            await self.bridge.dispatch(dict(type='sessions',cwd=self.cwd,autoResume=True))
        self.assertIsNone(self.bridge.adapter)
        self.assertTrue(any(e.get('role')=='notice' and 'unavailable' in e['text'] for e in self.events))

    async def test_new_chat_during_resume_process_shutdown_wins(self):
        key = self.saved()
        a = module.PiAdapter(self.bridge,'pi',self.cwd); self.bridge.adapter = a
        entered, release = asyncio.Event(), asyncio.Event()
        async def close(): entered.set(); await release.wait()
        a.close = close
        task = asyncio.create_task(self.bridge.dispatch(dict(type='resume',id=key,cwd=self.cwd)))
        await entered.wait()
        reset = asyncio.create_task(self.bridge.dispatch(dict(type='reset',cwd=self.cwd,forgetLast=True)))
        await asyncio.sleep(.01); release.set(); await asyncio.gather(task,reset)
        self.assertIsNone(self.bridge.adapter)
        self.assertFalse(any(e['type']=='resumed' for e in self.events))

    async def test_streams_are_merged_before_persistence(self):
        a = module.PiAdapter(self.bridge,'pi',self.cwd); self.bridge.adapter = a; a.session = 's'
        a.message('a','one',append=True); a.message('a','two',append=True); a.status(False)
        item = self.store.get(self.bridge.session_key,self.cwd)
        self.assertEqual(len(item['history']),1)
        self.assertEqual(item['history'][0]['text'],'onetwo')

    async def test_kimi_cli_listing_uses_closed_stdin_and_filters_directory(self):
        script = Path(self.cwd) / 'list.py'
        script.write_text("import json,sys\nassert sys.argv[1:3]==['session','list']\nassert sys.stdin.read()==''\ncwd=sys.argv[sys.argv.index('--cwd')+1]\nprint(json.dumps([{'id':'native-kimi','workDir':cwd,'lastPrompt':'Recent question','updatedAt':1700000000000},{'id':'other','workDir':'/unrelated'},{'id':'archived','workDir':cwd,'archived':True}]))\n")
        with patch.dict(os.environ, {'KATE_AGENT_DISABLE_NATIVE_DISCOVERY':'','KATE_AGENT_KIMI_COMMAND':shlex.join([sys.executable,str(script)])}), patch.object(module,'native_sessions',return_value=[]):
            await self.bridge.dispatch(dict(type='sessions',cwd=self.cwd))
        items = self.store.list(self.cwd)
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['session'],'native-kimi')
        self.assertEqual(items[0]['title'],'Recent question')
        self.assertEqual(items[0]['updated'],1700000000)

    async def test_model_change_before_launch_keeps_native_session(self):
        a = module.PiAdapter(self.bridge,'pi',self.cwd); self.bridge.adapter = a; a.session = 's'
        a.close, a.send = AsyncMock(), AsyncMock()
        await self.bridge.dispatch(dict(type='send',agent='pi',cwd=self.cwd,text='hello',model='next',effort='high'))
        a.close.assert_not_awaited(); a.send.assert_awaited_once_with('hello')
        self.assertEqual((a.session,a.model,a.effort),('s','next','high'))
        a.close = AsyncMock()
