"""Session-scoped model overrides and interactive protocol round trips."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_bridge import module


class ParameterTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, agent):
        self.events, self.calls = [], []
        a = module.ADAPTERS[agent](module.Bridge(self.events.append),agent,'/tmp')
        async def launch(args, **kwargs):
            self.calls.append(('launch',args)); a.process = SimpleNamespace(returncode=None)
        a.launch = launch
        a.write = AsyncMock()
        return a

    async def test_codex_model_and_effort_are_session_scoped(self):
        for resumed in (False, True):
            a = self.adapter('codex'); a.model, a.effort = 'test-model','high'
            a.session = 'prior' if resumed else ''
            async def rpc(method, params):
                self.calls.append((method,params))
                return {'thread':{'id':'prior'},'turn':{'id':'turn'}}
            a.rpc = rpc
            await a.send('hello')
            thread = next(p for m,p in self.calls if m in ('thread/start','thread/resume'))
            self.assertEqual(thread['model'],'test-model')
            self.assertEqual(self.calls[-1][1]['effort'],'high')
            self.assertEqual(a.session,'prior')
            self.assertFalse(any(m=='config/write' for m,p in self.calls))

    async def test_blank_fields_do_not_override_codex_defaults(self):
        a = self.adapter('codex')
        async def rpc(method,params):
            self.calls.append((method,params)); return {'thread':{'id':'s'},'turn':{'id':'t'}}
        a.rpc = rpc; await a.send('hello')
        for method,params in self.calls:
            if method != 'launch':
                self.assertNotIn('effort',params); self.assertNotIn('model',params)

    async def test_pi_and_claude_model_flags_are_plain_arguments(self):
        for agent in ('pi','claude'):
            a = self.adapter(agent); a.model = 'provider/model with spaces'; a.effort = 'high'; a.session = 'existing'
            async def write(data):
                if data.get('type') == 'control_request':
                    a.waiters[data['request_id']].set_result({})
            a.write = write
            await a.send('hello')
            args = self.calls[0][1]
            self.assertEqual(args[args.index('--model')+1], a.model)
            flag = '--thinking' if agent == 'pi' else '--effort'
            self.assertEqual(args[args.index(flag)+1],'high')
            self.assertIn('existing',args)

    async def test_acp_model_and_effort_follow_advertised_config_ids(self):
        for agent, effort_id, model in [('kimi','thinking','kimi-model'),('deepseek','reasoning_effort','["provider","deepseek-flash"]'),('hermes','reasoning','hermes-model')]:
            a = self.adapter(agent); a.model = 'deepseek-flash' if agent=='deepseek' else model; a.effort = 'high'; a.session = 'previous'
            async def launch(): a.process = SimpleNamespace(returncode=None)
            a.launch_acp = launch
            options = [{'id':'model','category':'model','options':[{'value':model,'name':'A model'}]}, {'id':effort_id,'category':'thought_level','options':[{'value':'high','name':'High'}]}]
            async def rpc(method,params):
                self.calls.append((method,params))
                if method=='initialize': return {'protocolVersion':1,'agentCapabilities':{'sessionCapabilities':{'resume':{}}}}
                return {'sessionId':'previous','configOptions':options}
            a.rpc = rpc
            with patch.object(module, 'dsh_profile_ready', return_value=True):
                await a.connect()
            settings = [p for m,p in self.calls if m=='session/set_config_option']
            self.assertEqual(settings,[{'sessionId':'previous','configId':'model','value':model},{'sessionId':'previous','configId':effort_id,'value':'high'}])
            self.assertIn(('session/resume',{'sessionId':'previous','cwd':'/tmp','mcpServers':[]}),self.calls)

    async def test_acp_rejects_unsupported_effort_instead_of_ignoring_it(self):
        a = self.adapter('kimi'); a.effort='high'
        async def rpc(method,params): return {'protocolVersion':1,'agentCapabilities':{},'sessionId':'s'}
        a.rpc = rpc
        with self.assertRaisesRegex(RuntimeError,'reasoning effort'):
            await a.connect()

    async def test_zcode_model_is_resolved_without_changing_workspace_defaults(self):
        a = self.adapter('zcode'); a.model='provider/model'; a.effort='high'
        async def rpc(method,params):
            self.calls.append((method,params))
            return {'session':{'sessionId':'s'},'settings':{'model':{'available':[{'ref':{'providerId':'provider','modelId':'model'}}]}}}
        a.rpc = rpc; await a.send('hello')
        selection = next(p for m,p in self.calls if m=='session/setModel')
        self.assertFalse(selection['persistAsWorkspaceLastUsed'])
        self.assertEqual(selection['model']['options'],{'reasoningLevel':'high'})
        self.assertEqual(self.calls[-1],('session/send',{'sessionId':'s','content':'hello'}))

    async def test_zcode_blank_effort_uses_advertised_model_default(self):
        a = self.adapter('zcode'); a.model = 'model'
        async def rpc(method,params):
            self.calls.append((method,params))
            return {'session':{'sessionId':'s'},'settings':{'model':{'available':[{'ref':{'providerId':'provider','modelId':'model'},'reasoning':{'defaultLevel':'none'}}]}}}
        a.rpc = rpc; await a.send('hello')
        selection = next(p for m,p in self.calls if m=='session/setModel')
        self.assertEqual(selection['model']['options'],{'reasoningLevel':'none'})


class ChoiceTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, agent):
        self.events = []
        self.bridge = module.Bridge(self.events.append)
        a = module.ADAPTERS[agent](self.bridge,agent,'/tmp'); self.bridge.adapter = a
        a.write = AsyncMock(); return a

    def token(self): return next(e['id'] for e in reversed(self.events) if e['type']=='request')

    async def test_kimi_form_supports_single_multiple_boolean_and_numeric_answers(self):
        a = self.adapter('kimi')
        schema = {'properties':{
            'q0':{'title':'Pick one','type':'string','oneOf':[{'const':'first','title':'First','description':'A choice'},{'const':'second','title':'Second'}]},
            'q1':{'title':'Pick several','type':'array','items':{'anyOf':[{'const':'a','title':'A'},{'const':'b','title':'B'}]}},
            'enabled':{'type':'boolean'}, 'count':{'type':'integer'}},'required':['q0','q1','enabled','count']}
        await a.handle({'id':'form','method':'elicitation/create','params':{'mode':'form','message':'Choose','requestedSchema':schema}})
        self.assertFalse(a.write.called)
        request = self.events[-1]
        self.assertTrue(request['questions'][1]['multiSelect'])
        self.assertFalse(request['questions'][0]['allowOther'])
        self.assertEqual(request['questions'][0]['options'][0]['description'],'A choice')
        answers = {'q0':'first','q1':['a','b'],'enabled':False,'count':2}
        await a.reply(self.token(), answers)
        self.assertEqual(a.write.call_args.args[0]['result'],{'action':'accept','content':answers})

    async def test_invalid_form_reply_is_retryable_and_can_be_dismissed(self):
        a = self.adapter('kimi')
        await a.handle({'id':7,'method':'elicitation/create','params':{'requestedSchema':{'properties':{'choice':{'type':'string','enum':['a','b']}},'required':['choice']}}})
        token = self.token()
        await self.bridge.dispatch({'type':'reply','id':token,'value':{'choice':'invented'}})
        self.assertIn(token,a.requests)
        self.assertEqual(self.events[-1]['type'],'request_failed')
        self.assertFalse(a.write.called)
        await self.bridge.dispatch({'type':'reply','id':token,'value':{'_cancel':True}})
        self.assertEqual(a.write.call_args.args[0]['result'],{'action':'cancel'})
        self.assertNotIn(token,a.requests)

    async def test_codex_other_secret_and_cancel(self):
        a = self.adapter('codex')
        question = {'id':'q','question':'Choose','isOther':True,'isSecret':True,'options':[{'label':'A'}]}
        for answer,expected in [({'q':'custom'},{'q':{'answers':['custom']}}),({'_cancel':True},{})]:
            await a.handle({'id':'codex-question','method':'item/tool/requestUserInput','params':{'questions':[question]}})
            self.assertTrue(self.events[-1]['questions'][0]['isSecret'])
            await a.reply(self.token(),answer)
            self.assertEqual(a.write.call_args.args[0]['result'],{'answers':expected})

    async def test_claude_multiselect_and_dismiss_are_not_tool_approval(self):
        a = self.adapter('claude')
        request = {'type':'control_request','request_id':'c','request':{'subtype':'can_use_tool','tool_name':'AskUserQuestion','input':{'questions':[{'question':'Choose','multiSelect':True,'options':[{'label':'A'},{'label':'B'}]}]}}}
        await a.handle(request); await a.reply(self.token(),{'Choose':['A','B']})
        self.assertEqual(a.write.call_args.args[0]['response']['response']['updatedInput']['answers'],{'Choose':'A, B'})
        await a.handle(request); await a.reply(self.token(),{'_cancel':True})
        self.assertEqual(a.write.call_args.args[0]['response']['response']['behavior'],'deny')

    async def test_zcode_runtime_preferences_and_questions(self):
        a = self.adapter('zcode')
        await a.handle({'id':'preferences','method':'session/requestRuntimePreferences','params':{}})
        self.assertFalse(a.write.call_args.args[0]['result']['askUserQuestionAutoResolutionEnabled'])
        await a.handle({'id':'q','method':'interaction/requestUserInput','params':{'questions':[{'question':'Which?','multiSelect':True,'options':[{'value':'a','label':'A'},{'value':'b','label':'B'}]}]}})
        await a.reply(self.token(),{'Which?':['a','b']})
        self.assertEqual(a.write.call_args.args[0]['result'],{'action':'accept','content':{'answers':{'Which?':'a, b'}}})
        await a.handle({'id':'p','method':'interaction/requestPermission','params':{'options':[{'name':'Allow once','response':{'decision':'allow'}}]}})
        await a.reply(self.token(),'0')
        self.assertEqual(a.write.call_args.args[0]['result'],{'decision':'allow'})

    async def test_reply_write_failure_preserves_request_for_retry(self):
        for agent,method,params,value in [('kimi','session/request_permission',{'options':[{'optionId':'once','name':'Allow once'}]},'once'),
                ('codex','item/commandExecution/requestApproval',{},'accept'),
                ('zcode','interaction/requestUserInput',{'questions':[{'question':'Pick','options':[{'label':'A'}]}]},{'Pick':'A'})]:
            a = self.adapter(agent); await a.handle({'id':3,'method':method,'params':params}); token = self.token()
            a.write.side_effect = BrokenPipeError('Closed pipe')
            await self.bridge.dispatch({'type':'reply','id':token,'value':value})
            self.assertIn(token,a.requests)
            self.assertEqual(self.events[-1]['type'],'request_failed')

    async def test_dsh_selected_and_custom_answers_use_native_shape(self):
        a = self.adapter('deepseek')
        writer = SimpleNamespace(write=lambda raw: frames.append(json.loads(raw)),drain=AsyncMock())
        frames = []; a.question_writer = writer
        await a.ask('native-question','Choose',questions=[{'id':'q','question':'Pick','options':[{'label':'A'}],'multiSelect':True,'required':True}],method='dsh-question')
        await a.reply(self.token(),{'q':['A','custom']})
        self.assertEqual(frames[0]['answer'],{'answers':[{'id':'q','selected':['A'],'custom':'custom'}]})
        self.assertFalse(a.requests)

    async def test_codex_mcp_form_and_turn_permissions(self):
        a = self.adapter('codex')
        await a.handle({'id':'form','method':'mcpServer/elicitation/request','params':{'mode':'form','requestedSchema':{'properties':{'flag':{'type':'boolean'}},'required':['flag']}}})
        await a.reply(self.token(),{'flag':False})
        self.assertEqual(a.write.call_args.args[0]['result'],{'action':'accept','content':{'flag':False},'_meta':None})
        requested = {'network':{'enabled':True}}
        for answer,expected in [('decline',{}),('accept',requested)]:
            await a.handle({'id':'p','method':'item/permissions/requestApproval','params':{'permissions':requested}})
            await a.reply(self.token(),answer)
            self.assertEqual(a.write.call_args.args[0]['result'],{'permissions':expected,'scope':'turn'})

    async def test_pi_native_cancel_and_codex_server_resolved(self):
        a = self.adapter('pi')
        await a.handle({'type':'extension_ui_request','id':'input','method':'input','title':'Enter value'})
        await a.reply(self.token(),{'_cancel':True})
        self.assertEqual(a.write.call_args.args[0],{'type':'extension_ui_response','id':'input','cancelled':True})
        a = self.adapter('codex')
        await a.handle({'id':'p','method':'item/fileChange/requestApproval','params':{}})
        token = self.token()
        await a.handle({'method':'serverRequest/resolved','params':{'requestId':'p'}})
        self.assertNotIn(token,a.requests)
        self.assertEqual(self.events[-1],{'type':'request_resolved','id':token})

    def test_typed_answers_reject_invalid_values(self):
        for kind,value in [('boolean','yes'),('integer',1.5),('number',float('inf')),('number',True),('array','one'),('string',{'bad':'value'})]:
            with self.subTest(kind=kind,value=value), self.assertRaises(ValueError):
                module.validate_answers([{'id':'q','valueType':kind}],{'q':value})
