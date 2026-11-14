#!/usr/bin/env python3
"""A model-free Pi RPC subprocess, with deliberately fragmented UTF-8 records."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

def argument(flag, default):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default

model = argument('--model', 'fake-model')
effort = argument('--thinking', 'off')
state = Path(os.environ['KATE_AGENT_STATE_HOME']) / 'fake-native.json' if os.environ.get('KATE_AGENT_STATE_HOME') else None
if '--session' in sys.argv and state and state.is_file():
    saved = json.loads(state.read_text())
    model, effort = argument('--model', saved['model']), argument('--thinking', saved['effort'])

def save():
    if state:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(dict(model=model, effort=effort)))

def emit(data):
    encoded = (json.dumps(data, ensure_ascii=False) + '\n').encode()
    for i in range(0, len(encoded), 3):
        os.write(1, encoded[i:i + 3])

for raw in sys.stdin.buffer:
    request = json.loads(raw)
    if request.get('type') == 'get_state':
        emit({'type': 'response', 'id': request.get('id'), 'command': 'get_state', 'success': True,
              'data': {'sessionFile': 'fake-session', 'model': {'id': model}, 'thinkingLevel': effort}})
    elif request.get('type') in ('get_available_models', 'set_model', 'set_thinking_level'):
        kind = request['type']
        if kind == 'set_model': model = request['modelId']
        if kind == 'set_thinking_level': effort = request['level']
        if kind != 'get_available_models': save()
        emit({'type': 'response', 'id': request['id'], 'command': kind, 'success': True,
              'data': {'models': [{'provider': 'fake', 'id': m} for m in ('fake-model', 'test-model', 'saved-model', 'other-model')]}})
    elif request.get('type') == 'prompt':
        save()
        prompt = request['message']
        if prompt == 'crash' or prompt.startswith('crash\n\nEditor context snapshots'):
            print('intentional error', file=sys.stderr, flush=True); sys.exit(7)
        if prompt == 'hang':
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])
            emit({'type': 'extension_ui_request', 'method': 'notify', 'message': f'child_pid={child.pid}'})
            time.sleep(300)
        emit({'type': 'message_start'})
        reply = 'Received the editor snapshot. We can discuss the selected code.\n\n$$\n\\int_0^1 x^2\\,dx = \\frac{1}{3}\n$$' if 'Editor context snapshots' in prompt else prompt
        for delta in ['Formula: ', '$E=mc^2$\n\n', 'File: `project/src/test.cpp:L123-234`\n\n', reply]:
            emit({'type': 'message_update', 'assistantMessageEvent': {'type': 'text_delta', 'delta': delta}})
        emit({'type': 'tool_execution_start', 'toolCallId': 't1', 'toolName': 'read', 'args': {'path': 'src/test.cpp'}})
        emit({'type': 'tool_execution_end', 'toolCallId': 't1', 'toolName': 'read', 'result': {'text': 'done'}})
        emit({'type': 'agent_settled'})
