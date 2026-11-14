"""DSH's question side channel: real Node/socket lifecycle, no model calls."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class HarnessQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_question_wait_answer_dismiss_and_cancellation(self):
        node = shutil.which('node')
        if not node: self.skipTest('Node is required for the DSH runtime hook')
        parent, child = socket.socketpair(); parent.setblocking(False)
        process = None; writer = None
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        packages = Path(temp.name) / 'node_modules/@deepseek-ai'
        acp = packages / 'dsh-acp/lib/index.js'; acp.parent.mkdir(parents=True)
        acp.write_text("export const inject=['agents']; export function apply(ctx) {ctx.originalApplied=true;}")
        ask = packages / 'dsh-tool-ask-user/lib/index.js'; ask.parent.mkdir(parents=True)
        ask.write_text("export function apply(ctx,config) {if(config.mode!=='legacy') throw Error('Wrong tool mode'); ctx.questionToolLoaded=true;}")
        for package in ('dsh-acp','dsh-tool-ask-user'):
            (packages/package/'package.json').write_text(json.dumps({'name':'@deepseek-ai/'+package,'type':'module','main':'lib/index.js'}))
        try:
            process = await asyncio.create_subprocess_exec(node,str(ROOT/'tests/dsh_question_peer.mjs'),acp.as_uri(),
                env={**os.environ,'KATE_AGENT_DSH_UI_FD':str(child.fileno()),'KATE_AGENT_DSH_UI_OWNER':''},
                pass_fds=(child.fileno(),), stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            child.close()
            reader,writer = await asyncio.open_connection(sock=parent)
            async def event():
                raw = await asyncio.wait_for(reader.readline(),5)
                if not raw:
                    _,stderr = await process.communicate()
                    self.fail('Question peer closed: '+stderr.decode())
                return json.loads(raw)
            self.assertEqual((await event())['type'],'ready')
            first = await event(); self.assertEqual(first['type'],'question')
            writer.write((json.dumps({'id':first['id'],'answer':{'answers':[{'id':'q','selected':['A','B'],'custom':'extra'}]}})+'\n').encode())
            await writer.drain()
            second = await event(); self.assertEqual(second['type'],'question')
            writer.write((json.dumps({'id':second['id'],'cancelled':True})+'\n').encode()); await writer.drain()
            third = await event(); self.assertEqual(third['type'],'question')
            cancelled = await event(); self.assertEqual(cancelled,{'type':'cancel','id':third['id']})
            out,err = await asyncio.wait_for(process.communicate(),5)
            self.assertEqual(process.returncode,0,err.decode()); self.assertEqual(out,b'questions-ok\n')
        finally:
            child.close()
            if process and process.returncode is None:
                process.kill(); await process.wait()
            if writer:
                writer.close()
                try: await writer.wait_closed()
                except OSError: pass
            else: parent.close()
