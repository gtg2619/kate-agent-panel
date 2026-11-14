"""Deterministic process failures and ACP lifecycle scenarios; never calls a model."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

mode = sys.argv[1]

def emit(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)

def wait_for_prompt():
    for line in sys.stdin:
        request = json.loads(line)
        if request.get('type') == 'get_state':
            emit({'type':'response', 'command':'get_state', 'id':request['id'], 'success':True, 'data':{}})
        elif request.get('type') == 'prompt':
            return

if mode == 'oversize':
    print('x' * 4096, flush=True)
    time.sleep(300)
elif mode == 'stderr':
    wait_for_prompt()
    for byte in 'UTF-8: \u4e2d\u6587\n'.encode():
        os.write(2, bytes([byte])); time.sleep(.005)
    emit({'type': 'agent_settled'})
    sys.stdin.read()
elif mode == 'noise':
    wait_for_prompt()
    for record in [b'plain log\n', b'\xff\n', b'[]\n', b'null\n', b'{broken\n']:
        os.write(1, record)
    emit({'type': 'agent_settled'})
    sys.stdin.read()
elif mode == 'stubborn':
    wait_for_prompt()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(300)'])
    emit({'type': 'extension_ui_request', 'method': 'notify', 'message': f'child_pid={child.pid}'})
    time.sleep(300)
elif mode == 'exit':
    sys.exit(7)
elif mode == 'stall':
    sys.stdin.read()
elif mode == 'acp':
    session = f'session-{os.getpid()}'
    pending = None
    for raw in sys.stdin:
        data = json.loads(raw)
        method, params = data.get('method'), data.get('params', {})
        if method == 'initialize':
            assert params['clientCapabilities'] == {'elicitation': {'form': {}}}
            if os.environ.get('FAKE_ACP_BAD_RESULT') == '1':
                emit({'id':data['id'], 'result':[]})
                continue
            emit({'id': data['id'], 'result': {'protocolVersion': int(os.environ.get('FAKE_ACP_VERSION', '1')),
                 'agentCapabilities': {'loadSession': os.environ.get('FAKE_ACP_RELOAD', '1') == '1'}}})
        elif method == 'session/new':
            emit({'id': data['id'], 'result': {'sessionId': session}})
        elif method == 'session/load':
            session = params['sessionId']
            emit({'id': data['id'], 'result': {}})
        elif method == 'session/prompt':
            assert params['sessionId'] == session
            text = params['prompt'][0]['text']
            if text == 'hang':
                emit({'method': 'session/update', 'params': {'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'waiting'}}}})
                continue
            if text == 'fail':
                emit({'id': data['id'], 'error': {'code': -32000, 'message': 'fixture refusal'}})
                continue
            if text == 'permit':
                pending = data['id']
                emit({'id': 'permission', 'method': 'session/request_permission', 'params': {
                    'toolCall': {'title': 'Read fixture'}, 'options': [{'optionId': 'once', 'name': 'Allow once', 'kind': 'allow_once'}]}})
                continue
            emit({'method': 'session/update', 'params': {'sessionId': session, 'update': {
                'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': session + ':' + text}}}})
            emit({'id': data['id'], 'result': {'stopReason': 'end_turn'}})
        elif data.get('id') == 'permission':
            outcome = data['result']['outcome']['outcome']
            if outcome == 'selected':
                Path('approved').write_text('explicit permission')
            emit({'id': pending, 'result': {'stopReason': 'end_turn'}})
