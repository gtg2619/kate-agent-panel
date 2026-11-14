// Add a human answerer to the existing ACP frontend in this process only.
// The installed modules, profile files, permissions, and model defaults stay owned by DSH.
import * as nodeModule from 'node:module';
import { Socket } from 'node:net';
import { createInterface } from 'node:readline';
import { randomUUID } from 'node:crypto';

const stateKey = Symbol.for('kate-agent-panel.dsh-questions');

export function installQuestions(ctx) {
  const state = globalThis[stateKey];
  if (!state) return;
  ctx.on('user-questions/request', async (request) => {
    const id = randomUUID();
    const lifetime = new AbortController();
    let claim;
    try {
      if (request.wait?.timed && request.agent) {
        const service = ctx.get('userQuestions');
        if (!service?.attachWait) throw new Error('This DSH version cannot hold a question open');
        claim = service.attachWait(request.agent, request.wait.callId, lifetime.signal);
        await claim.next();
      }
      return await new Promise((resolve, reject) => {
        const abort = () => {
          state.pending.delete(id);
          state.send({type: 'cancel', id});
          reject(request.signal?.reason || new Error('Question cancelled'));
        };
        state.pending.set(id, {
          resolve: value => { request.signal?.removeEventListener('abort', abort); resolve(value); },
          reject: error => { request.signal?.removeEventListener('abort', abort); reject(error); },
        });
        request.signal?.addEventListener('abort', abort, {once: true});
        if (request.signal?.aborted) { abort(); return; }
        state.send({type: 'question', id, questions: request.questions});
      });
    } finally {
      lifetime.abort();
      await claim?.return();
      state.pending.delete(id);
    }
  }, {global: true});
  state.send({type: 'ready'});
}

if (process.env.KATE_AGENT_DSH_UI_FD && !process.env.KATE_AGENT_DSH_UI_OWNER) {
  process.env.KATE_AGENT_DSH_UI_OWNER = String(process.pid);
  const socket = new Socket({fd: Number(process.env.KATE_AGENT_DSH_UI_FD), readable: true, writable: true});
  const pending = new Map();
  const state = {pending, send: value => socket.write(JSON.stringify(value) + '\n')};
  globalThis[stateKey] = state;
  const rejectAll = () => {
    for (const request of pending.values()) request.reject(new Error('Kate question connection closed'));
    pending.clear();
  };
  socket.on('error', rejectAll); socket.on('close', rejectAll);
  createInterface({input: socket}).on('line', line => {
    try {
      const reply = JSON.parse(line);
      const request = pending.get(reply.id);
      if (!request) return;
      pending.delete(reply.id);
      if (reply.cancelled) request.reject(new Error('User dismissed the question'));
      else request.resolve(reply.answer);
    } catch (error) {
      state.send({type: 'error', text: String(error)});
    }
  });
  // Decorate the plugin's public apply entry point; do not patch its source on disk.
  if (typeof nodeModule.registerHooks !== 'function') throw new Error('DSH question support requires Node.js 22.15+ or 24+');
  nodeModule.registerHooks({resolve(specifier, context, nextResolve) {
    const result = nextResolve(specifier, context);
    if (!result.url.endsWith('/@deepseek-ai/dsh-acp/lib/index.js')) return result;
    const original = result.url + '?kate-original';
    const askUser = nextResolve('@deepseek-ai/dsh-tool-ask-user', {...context, parentURL: result.url}).url;
    const wrapper = `export * from ${JSON.stringify(original)};
      import {apply as originalApply, inject as originalInject} from ${JSON.stringify(original)};
      import * as askUser from ${JSON.stringify(askUser)};
      import {installQuestions} from ${JSON.stringify(import.meta.url)};
      export const inject = [...originalInject, 'tools', 'userQuestions'];
      export function apply(ctx, config) {
        installQuestions(ctx);
        if (!ctx.tools.get('ask_user_question')) ctx.plugin(askUser, {mode:'legacy'});
        return originalApply(ctx, config);
      }`;
    return {url: 'data:text/javascript,' + encodeURIComponent(wrapper), shortCircuit: true};
  }});
}
