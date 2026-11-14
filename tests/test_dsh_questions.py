"""DSH's question side channel: real Node/socket lifecycle, no model calls."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import shlex
import tempfile
import unittest
from unittest.mock import patch

from test_bridge import module, ROOT


class HarnessQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_question_wait_answer_dismiss_and_cancellation(self):
        node = os.environ.get('KATE_AGENT_TEST_NODE') or shutil.which('node')
        if not node: self.skipTest('Node is required for the DSH runtime hook')
        events = []
        adapter = module.ACPAdapter(module.Bridge(events.append), 'deepseek', '/tmp')
        temp = tempfile.TemporaryDirectory(prefix='kate # percent% quote" \u03b1 '); self.addCleanup(temp.cleanup)
        packages = Path(temp.name) / 'node_modules/@deepseek-ai'
        acp = packages / 'dsh-acp/lib/index.js'; acp.parent.mkdir(parents=True)
        acp.write_text("export const inject=['agents']; export function apply(ctx) {ctx.originalApplied=true;}")
        ask = packages / 'dsh-tool-ask-user/lib/index.js'; ask.parent.mkdir(parents=True)
        ask.write_text("export function apply(ctx,config) {if(config.mode!=='legacy') throw Error('Wrong tool mode'); ctx.questionToolLoaded=true;}")
        for package in ('dsh-acp','dsh-tool-ask-user'):
            (packages/package/'package.json').write_text(json.dumps({'name':'@deepseek-ai/'+package,'type':'module','main':'lib/index.js'}))
        # Exercise the adapter's real --import parsing, preserving an existing preload.
        helper = Path(temp.name) / 'dsh_questions.mjs'
        shutil.copyfile(ROOT / 'bridge/dsh_questions.mjs', helper)
        peer = Path(temp.name) / 'peer.mjs'
        peer.write_text((ROOT / 'tests/dsh_question_peer.mjs').read_text()
                        .replace('../bridge/dsh_questions.mjs', './dsh_questions.mjs')
                        .replace('let callback;', "if (!globalThis.existingPreload) throw Error('Lost NODE_OPTIONS');\nlet callback;"))
        preload = Path(temp.name) / 'existing.mjs'; preload.write_text('globalThis.existingPreload = true;')
        async def until(predicate):
            async def wait():
                while not predicate(): await asyncio.sleep(.01)
            await asyncio.wait_for(wait(), 5)
        try:
            with patch.object(module, '__file__', str(helper.with_name('agent_bridge.py'))), patch.dict(os.environ, {
                'KATE_AGENT_DEEPSEEK_COMMAND': shlex.join([node, str(peer), acp.as_uri()]),
                'NODE_OPTIONS': '--import=' + json.dumps(preload.as_uri()),
            }):
                await adapter.launch_acp()
            questions = lambda: [event for event in events if event['type'] == 'request']
            await until(lambda: len(questions()) == 1)
            self.assertTrue(adapter.question_ready)
            await adapter.reply(questions()[0]['id'], {'q': ['A', 'B', 'extra']})
            await until(lambda: len(questions()) == 2)
            await adapter.reply(questions()[1]['id'], {'_cancel': True})
            await until(lambda: len(questions()) == 3)
            await until(lambda: any(event['type'] == 'request_resolved' and event['id'] == questions()[2]['id'] for event in events))
            await asyncio.wait_for(adapter.process.wait(), 5)
            await until(lambda: any(event.get('text') == 'questions-ok' for event in events))
            self.assertEqual(adapter.process.returncode, 0)
            self.assertFalse(any(event['type'] == 'error' for event in events))
        finally:
            await adapter.close()
