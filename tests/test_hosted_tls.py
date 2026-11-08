import asyncio
from contextlib import contextmanager
import socket
import os
import sys
import ssl
import struct
import json
from threading import Event, Thread
from unittest.mock import Mock
import uuid

import pytest

from boomer_switch.home_runner import WebsiteTransport
from boomer_switch.website_grants import PermissionUnavailable


def fixture_module():
    pytest.importorskip('playwright.sync_api', reason='The hosted fixture requires optional demo dependencies.')
    pytest.importorskip('PIL.Image', reason='The hosted fixture requires optional demo dependencies.')
    pytest.importorskip('zxingcpp', reason='The hosted fixture requires optional demo dependencies.')
    from demos import hosted_website_acceptance
    return hosted_website_acceptance


@contextmanager
def tls_fixture(tmp_path, app, server_class=None):
    demo = fixture_module()
    cert,key = demo.make_certificate(tmp_path)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert,key)
    server = demo.make_server(demo.LOOPBACK,0,app,server_class=server_class or demo.FixtureServer,handler_class=demo.QuietHandler)
    server.enable_tls(tls)
    worker = Thread(target=server.serve_forever,daemon=True)
    worker.start()
    try:
        yield server,ssl.create_default_context(cafile=str(cert))
    finally:
        server.shutdown()
        worker.join(timeout=3)
        server.server_close()
        assert not worker.is_alive()


def test_hosted_fixture_completes_tls_close_notify_after_complete_response(tmp_path):
    def app(environ,start):
        start('200 OK',[('Content-Length','5')])
        return [b'ready']
    with tls_fixture(tmp_path,app) as (server,context):
        with socket.create_connection(('localhost',server.server_port),timeout=2) as tcp:
            with context.wrap_socket(tcp,server_hostname='localhost') as connection:
                connection.sendall(b'GET / HTTP/1.0\r\nHost: localhost\r\n\r\n')
                received=b''
                while b'\r\n\r\n' not in received or len(received.split(b'\r\n\r\n',1)[1])<5:
                    part=connection.recv(4096)
                    assert part, 'The synthetic response ended before its body.'
                    received+=part
                assert received.split(b'\r\n\r\n',1)[1]==b'ready'
                raw=connection.unwrap()
                raw.close()


def test_tls_close_failure_has_one_scoped_diagnostic_and_closes_the_socket(capsys):
    demo=fixture_module()
    request=Mock(spec=ssl.SSLSocket)
    request.version.return_value='TLSv1.3'
    request.unwrap.side_effect=OSError('Synthetic private diagnostic detail')
    server=object.__new__(demo.FixtureServer)
    server.shutdown_request(request)
    request.settimeout.assert_called_once_with(1)
    request.close.assert_called_once()
    assert capsys.readouterr().out=='CHECK: fixture TLS close error (OSError)\n'


def test_unexpected_close_failure_still_propagates_and_releases_the_socket():
    demo=fixture_module()
    request=Mock(spec=ssl.SSLSocket)
    request.version.return_value='TLSv1.3'
    request.unwrap.side_effect=RuntimeError('Synthetic unexpected close failure')
    server=object.__new__(demo.FixtureServer)
    with pytest.raises(RuntimeError,match='Synthetic unexpected close failure'):
        server.shutdown_request(request)
    request.close.assert_called_once()


@pytest.mark.parametrize('peer_close', ['reset','withheld'])
def test_real_peer_close_failure_is_bounded_and_reported_once(tmp_path,capsys,peer_close):
    demo=fixture_module()
    closed=Event()
    diagnostics=[]
    class Observed(demo.FixtureServer):
        def handle_error(self, request, address, *, phase='request'):
            diagnostics.append((phase,type(sys.exception()).__name__))
            super().handle_error(request,address,phase=phase)
        def close_request(self,request):
            super().close_request(request)
            closed.set()
    def app(environ,start):
        start('200 OK',[('Content-Length','5')]); return [b'ready']
    with tls_fixture(tmp_path,app,Observed) as (server,context):
        connection=context.wrap_socket(socket.create_connection(('localhost',server.server_port),timeout=2),server_hostname='localhost')
        try:
            connection.sendall(b'GET / HTTP/1.0\r\nHost: localhost\r\n\r\n')
            received=b''
            while b'\r\n\r\n' not in received or len(received.split(b'\r\n\r\n',1)[1])<5:
                part=connection.recv(4096)
                assert part
                received+=part
            if peer_close=='reset':
                connection.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('HH' if os.name=='nt' else 'ii',1,0))
                connection.close()
            assert closed.wait(2), 'The peer close exceeded the fixture deadline.'
            assert len(diagnostics)==1 and diagnostics[0][0]=='TLS close'
            assert diagnostics[0][1] in ('ConnectionResetError','ConnectionAbortedError','SSLEOFError','SSLError','TimeoutError')
        finally:
            connection.close()
    captured=capsys.readouterr()
    assert captured.err==''
    assert captured.out.count('CHECK: fixture TLS close error (')==1


def test_short_lived_transport_success_has_no_unhandled_loop_diagnostics(tmp_path,capsys):
    events=[]
    def app(environ,start):
        body=json.loads(environ['wsgi.input'].read(int(environ['CONTENT_LENGTH'])))
        payload=json.dumps({'request_id':body['request_id']}).encode()
        start('200 OK',[('Content-Length',str(len(payload)))])
        return [payload]
    with tls_fixture(tmp_path,app) as (server,context):
        async def scenario():
            loop=asyncio.get_running_loop()
            def observe(loop,details):
                exception=details.get('exception')
                events.append((type(exception).__name__,getattr(exception,'winerror',None)))
                loop.default_exception_handler(details)
            loop.set_exception_handler(observe)
            transport=WebsiteTransport(f'https://localhost:{server.server_port}','synthetic-key-'+'k'*32)
            transport.ssl=context
            for _ in range(2):
                request_id=str(uuid.uuid4())
                assert await transport.post('poll',{'request_id':request_id})=={'request_id':request_id}
        for _ in range(10):
            asyncio.run(scenario())
    assert events==[]
    captured=capsys.readouterr()
    assert captured.out=='' and captured.err==''


def test_async_client_keeps_real_action_failure_visible(tmp_path, capsys):
    def app(environ,start):
        raise RuntimeError('Synthetic action failure')
    with tls_fixture(tmp_path,app) as (server,context):
        async def scenario():
            transport=WebsiteTransport(f'https://localhost:{server.server_port}','synthetic-key-'+'k'*32)
            transport.ssl=context
            with pytest.raises(PermissionUnavailable):
                await transport.post('poll',{'request_id':str(uuid.uuid4())})
        asyncio.run(scenario())
    assert 'Synthetic action failure' in capsys.readouterr().err
