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
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print('x' * (4 * 1024 * 1024), flush=True)
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
elif mode in ('stubborn', 'stubborn-pipes'):
    wait_for_prompt()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(300)'],
                             start_new_session=mode == 'stubborn-pipes')
    emit({'type': 'extension_ui_request', 'method': 'notify', 'message': f'child_pid={child.pid}'})
    time.sleep(300)
elif mode == 'exit':
    sys.exit(7)
elif mode == 'stall':
    sys.stdin.read()
elif mode == 'acp':
    session = f'session-{os.getpid()}'
    pending = None
    child = None
    cancelled_answers = 0
    cancel_requested = False
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
            if text == 'bash':
                pending = data['id']
                child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
                Path('bash.pid').write_text(str(child.pid))
                for update in [
                    {'sessionUpdate': 'agent_message_chunk', 'messageId': 'first', 'content': {'type': 'text', 'text': 'Checking first.'}},
                    {'sessionUpdate': 'tool_call', 'toolCallId': 'first-tool', 'title': 'Read', 'status': 'in_progress'},
                    {'sessionUpdate': 'agent_message_chunk', 'messageId': 'second', 'content': {'type': 'text', 'text': 'Running '}},
                    {'sessionUpdate': 'tool_call_update', 'toolCallId': 'first-tool', 'status': 'completed'},
                    {'sessionUpdate': 'agent_message_chunk', 'messageId': 'second', 'content': {'type': 'text', 'text': 'Bash.'}},
                    {'sessionUpdate': 'tool_call', 'toolCallId': 'bash-tool', 'title': 'Bash', 'status': 'in_progress'},
                ]:
                    emit({'method': 'session/update', 'params': {'sessionId': session, 'update': update}})
                continue
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
        elif method == 'session/cancel':
            assert 'id' not in data and params['sessionId'] == session
            if pending is not None:
                if child is not None:
                    child.terminate(); child.wait(timeout=2)
                    child = None
                else:
                    cancel_requested = True
                    emit({'id': 'late-permission', 'method': 'session/request_permission', 'params': {
                        'toolCall': {'title': 'Permission racing Stop'}, 'options': [{'optionId': 'once', 'name': 'Allow once', 'kind': 'allow_once'}]}})
                    continue
                Path('cancelled').write_text('ACP cancellation received')
                emit({'id': pending, 'result': {'stopReason': 'cancelled'}})
                pending = None
        elif data.get('id') in ('permission', 'late-permission'):
            outcome = data['result']['outcome']['outcome']
            if outcome == 'selected':
                Path('approved').write_text('explicit permission')
            if cancel_requested:
                assert outcome == 'cancelled'
                cancelled_answers += 1
                if cancelled_answers == 2:
                    Path('permissions-cancelled').write_text('Pending and late requests cancelled')
                    emit({'id': pending, 'result': {'stopReason': 'cancelled'}})
            else:
                emit({'id': pending, 'result': {'stopReason': 'end_turn'}})
