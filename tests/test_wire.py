"""Drive the bridge's public stdin/stdout interface, including shutdown and bad input."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest

from test_bridge import ROOT


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        env = dict(os.environ, KATE_AGENT_PI_COMMAND=shlex.join([sys.executable, str(ROOT / 'tests/fake_agent.py')]),
                   KATE_AGENT_STATE_HOME=self.temp.name + '/state', KATE_AGENT_DISABLE_NATIVE_DISCOVERY='1',
                   OPENAI_API_KEY='fixture-secret-do-not-emit')
        self.process = await asyncio.create_subprocess_exec(sys.executable, '-u', str(ROOT / 'bridge/agent_bridge.py'),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        self.events = []
        await self.until(lambda e: e['type'] == 'inventory')

    async def asyncTearDown(self):
        if self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 6)
            except asyncio.TimeoutError:
                self.process.kill(); await self.process.wait()
        stderr = (await self.process.stderr.read()).decode(errors='replace')
        self.assertNotIn('Task exception was never retrieved', stderr)
        self.assertNotIn('fixture-secret-do-not-emit', json.dumps(self.events))
        self.temp.cleanup()

    async def write(self, value):
        payload = value if isinstance(value, bytes) else json.dumps(value).encode() + b'\n'
        self.process.stdin.write(payload)
        await self.process.stdin.drain()

    async def until(self, predicate):
        async def loop():
            while line := await self.process.stdout.readline():
                event = json.loads(line)
                self.events.append(event)
                if predicate(event): return event
            self.fail('Bridge stdout ended unexpectedly')
        return await asyncio.wait_for(loop(), 5)

    async def send(self, text):
        await self.write({'type':'send','agent':'pi','cwd':self.temp.name,'text':text})

    async def test_invalid_input_returns_error_without_losing_next_command(self):
        for value in [b'{bad\n', [], {'type':'unknown'}, {'type':'send','agent':[], 'cwd':self.temp.name,'text':'x'},
                      {'type':'send','agent':'pi','cwd':None,'text':'x'},
                      {'type':'send','agent':'pi','cwd':self.temp.name,'text':[]}]:
            with self.subTest(value=value):
                await self.write(value)
                await self.until(lambda e: e['type'] == 'error')
                await self.write({'type':'discover'})
                await self.until(lambda e: e['type'] == 'inventory')

    async def test_fragmented_input_is_read_as_one_command(self):
        payload = json.dumps({'type':'send','agent':'pi','cwd':self.temp.name,'text':'fragmented \u4e2d\u6587'}, ensure_ascii=False).encode() + b'\n'
        for offset in range(0, len(payload), 3):
            await self.write(payload[offset:offset+3])
        await self.until(lambda e: e['type'] == 'status' and not e['busy'])
        self.assertTrue(any(e.get('text') == 'fragmented \u4e2d\u6587' for e in self.events))

    async def test_busy_cancel_and_next_send(self):
        await self.send('hang')
        await self.until(lambda e: e.get('text', '').startswith('child_pid='))
        await self.write({'type':'reply','id':[],'value':'once'})
        await self.until(lambda e: e['type'] == 'error')
        await self.send('must not run')
        error = await self.until(lambda e: e['type'] == 'error')
        self.assertIn('busy', error['text'].lower())
        await self.write({'type':'cancel'})
        await self.until(lambda e: e['type'] == 'requests_cleared')
        await self.send('recovered')
        await self.until(lambda e: e['type'] == 'status' and not e['busy'])
        self.assertTrue(any(e.get('text') == 'recovered' for e in self.events))

    async def test_eof_reaps_agent_tools(self):
        await self.send('hang')
        event = await self.until(lambda e: e.get('text', '').startswith('child_pid='))
        pid = int(event['text'].split('=')[1])
        self.process.stdin.close()
        await asyncio.wait_for(self.process.wait(), 6)
        self.assertEqual(self.process.returncode, 0)
        status = Path(f'/proc/{pid}/stat')
        self.assertTrue(not status.exists() or status.read_text().split()[2] == 'Z')
