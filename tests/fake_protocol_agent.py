"""Minimal subprocess peers that validate client handshakes without a model or credentials."""
import json
import sys
import time

mode = sys.argv[1]
session = 'session-test'
turn = 0
pending = None

def emit(data): print(json.dumps(data, ensure_ascii=False), flush=True)

def response(id, result): emit({'jsonrpc': '2.0', 'id': id, 'result': result})

for raw in sys.stdin:
    data = json.loads(raw)
    method = data.get('method')
    if mode == 'zcode':
        assert 'jsonrpc' not in data
        assert isinstance(data['id'], str)
        if method in ('session/create', 'session/resume'):
            if method.endswith('resume'): assert data['params']['sessionId'] == session
            response(data['id'], {'session': {'sessionId': session}, 'runtime': {'eventSeq': 0}})
        elif method == 'session/subscribe':
            assert data['params']['deliveryKind'] == 'desktop-continuous'
            response(data['id'], {'events': []})
        elif method == 'session/send':
            assert data['params']['sessionId'] == session
            response(data['id'], {'accepted': True})
            emit({'method': 'session/event', 'params': {'sessionId': session, 'type': 'model.streaming', 'payload': {'kind': 'text_delta', 'delta': data['params']['content']}}})
            time.sleep(.1)
            emit({'method': 'session/event', 'params': {'sessionId': session, 'type': 'turn.completed', 'payload': {}}})
    elif mode == 'codex':
        if method == 'initialize':
            assert data['params']['clientInfo']['name'] == 'kate_agent_panel'
            response(data['id'], {'userAgent': 'fake'})
        elif method in ('thread/start', 'thread/resume'):
            response(data['id'], {'thread': {'id': session}})
        elif method == 'turn/start':
            assert data['params']['threadId'] == session
            assert data['params']['input'][0]['type'] == 'text'
            turn += 1
            response(data['id'], {'turn': {'id': str(turn)}})
            emit({'method': 'item/agentMessage/delta', 'params': {'itemId': str(turn), 'delta': f'answer {turn}'}})
            emit({'method': 'turn/completed', 'params': {'turn': {'id': str(turn), 'status': 'completed'}}})
    elif mode == 'acp':
        if method == 'initialize':
            assert data['params']['protocolVersion'] == 1
            assert data['params']['clientCapabilities'] == {'elicitation': {'form': {}}}
            response(data['id'], {'protocolVersion': 1, 'agentCapabilities': {'loadSession': True}})
        elif method == 'session/new': response(data['id'], {'sessionId': session})
        elif method == 'session/prompt':
            assert data['params']['sessionId'] == session
            assert data['params']['prompt'][0]['type'] == 'text'
            pending = data['id']
            emit({'jsonrpc': '2.0', 'id': 'permission-1', 'method': 'session/request_permission', 'params': {
                'toolCall': {'title': 'Read selected source'}, 'options': [{'optionId': 'once', 'name': 'Allow once', 'kind': 'allow_once'}]}})
        elif data.get('id') == 'permission-1':
            assert data['result']['outcome'] == {'outcome': 'selected', 'optionId': 'once'}
            emit({'method': 'session/update', 'params': {'sessionId': session, 'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'approved answer'}}}})
            response(pending, {'stopReason': 'end_turn'})
    elif mode == 'claude':
        if data.get('type') == 'control_request':
            assert data['request']['subtype'] in ('initialize', 'get_context_usage')
            emit({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': data['request_id'], 'response': {}}})
        elif data.get('type') == 'user':
            assert data['message']['role'] == 'user'
            emit({'type': 'system', 'subtype': 'init', 'session_id': session})
            emit({'type': 'control_request', 'request_id': 'permission-c', 'request': {'subtype': 'can_use_tool', 'tool_name': 'Read', 'input': {'file_path': 'src/test.cpp'}}})
        elif data.get('type') == 'control_response':
            result = data['response']['response']
            assert result == {'behavior': 'allow', 'updatedInput': {'file_path': 'src/test.cpp'}}
            emit({'type': 'assistant', 'message': {'id': 'a', 'content': [{'type': 'text', 'text': 'approved answer'}]}})
            emit({'type': 'result', 'session_id': session, 'is_error': False})
