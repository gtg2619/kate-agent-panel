// Exercise the real runtime hook without installing or calling an agent/model.
import {installQuestions} from '../bridge/dsh_questions.mjs';

let callback;
let holds = 0;
const ctx = {
  tools: {get: () => undefined},
  plugin(plugin, config) { plugin.apply(this, config); },
  on(name, handler, options) {
    if (name !== 'user-questions/request' || !options.global) throw Error('Wrong hook');
    callback = handler;
  },
  get(name) {
    if (name !== 'userQuestions') throw Error('Wrong service');
    return {attachWait: async function* (agent, id, signal) {
      holds++;
      try { yield true; } finally { holds--; }
    }};
  },
};
if (process.argv[2]) {
  const plugin = await import(process.argv[2]);
  if (!plugin.inject.includes('tools') || !plugin.inject.includes('userQuestions')) throw Error('Missing injection');
  plugin.apply(ctx, {});
  if (!ctx.originalApplied || !ctx.questionToolLoaded) throw Error('Missing runtime plugin');
} else installQuestions(ctx);
const request = {questions:[{id:'q',question:'Pick',options:[{label:'A'},{label:'B'}],multiSelect:true}],
  wait:{timed:true,callId:'tool'},agent:{},signal:new AbortController().signal};
const answer = await callback(request);
if (holds !== 0 || JSON.stringify(answer) !== JSON.stringify({answers:[{id:'q',selected:['A','B'],custom:'extra'}]})) throw Error('Answer mismatch');
try {
  await callback({...request,wait:undefined});
  throw Error('Dismissal was accepted');
} catch (error) {
  if (!error.message.includes('dismissed')) throw error;
}
const abort = new AbortController();
const pending = callback({...request,signal:abort.signal});
setTimeout(() => abort.abort(Error('Turn stopped')), 20);
try {
  await pending;
  throw Error('Cancellation was accepted');
} catch (error) {
  if (!error.message.includes('stopped')) throw error;
}
if (holds !== 0) throw Error('Leaked question wait');
process.stdout.write('questions-ok\n');
process.exit(0);
