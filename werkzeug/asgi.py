from asyncio import get_event_loop, iscoroutine, run_coroutine_threadsafe
from io import RawIOBase
from collections.abc import AsyncIterable

from werkzeug._compat import text_type
from werkzeug.wrappers import Request, Response


class SynchronizedInputStream(RawIOBase):
    """A synchronous stream constructed from an asynchronous event source.

    To be avoided; this should not be merged."""
    def __init__(self, receiver, loop):
        self.receiver = receiver
        self.loop = loop
        self.bufp = 0
        self.buffer = b''
        self.exhausted = False
        self.ended = False

    async def _read(self):
        if self.ended:
            return b''
        event = await self.receiver()
        if event['type'] == 'http.disconnect':
            self.ended = True
            return b''
        if event['type'] == 'http.request':
            self.ended = not event.get('more_body', False)
            return event.get('body', b'')

    def read(self, n=-1):
        if n == -1:
            rv = b''
            while True:
                v = self.read(8192)
                if len(v) == 0:
                    break
                rv += v
            return rv
        if n == 0 or self.exhausted:
            return b''
        rv = self.buffer[self.bufp:self.bufp + n]
        if len(rv) == n:
            self.bufp += n
            return rv
        while True:
            fut = run_coroutine_threadsafe(self._read(), self.loop)
            self.buffer = fut.result()
            if len(self.buffer) == 0:
                self.exhausted = True
                break
            self.bufp = n - len(rv)
            rv += self.buffer[:n - len(rv)]
            if len(rv) == n:
                break
        return rv


def asgi_scope_to_environ(scope):
        environ = {
            'REQUEST_METHOD': scope['method'],
            'SCRIPT_NAME': scope.get('root_path', ''),
            'PATH_INFO': scope.get('root_path', '') + scope['path'],
            'QUERY_STRING': scope.get('query_string').decode('iso-8859-1'),
            'SERVER_NAME': scope.get('server', [None, None])[0],
            'SERVER_PORT': scope.get('server', [None, None])[1],
            'REMOTE_HOST': scope.get('client', [None, None])[0],
            'REMOTE_ADDR': scope.get('client', [None, None])[0],
            'REMOTE_PORT': scope.get('client', [None, None])[1],
            'wsgi.url_scheme': scope.get('scheme', 'http'),
        }
        for k, v in scope.get('headers', []):
            wsgi_key = k.decode('iso-8859-1').replace('-', '_').upper()
            if wsgi_key not in {'CONTENT_TYPE', 'CONTENT_LENGTH'}:
                wsgi_key = 'HTTP_' + wsgi_key
            if wsgi_key not in environ:
                environ[wsgi_key] = v.decode('iso-8859-1')
            else:
                environ[wsgi_key] += '; ' + v.decode('iso-8859-1')
        return environ


class ASGIRequest(Request):
    asgi = True

    def __init__(self, scope, receiver, sender):
        self.scope = scope
        self.receiver = receiver
        self.sender = sender
        self.environ = asgi_scope_to_environ(scope)
        self.environ['wsgi.input'] = SynchronizedInputStream(receiver, get_event_loop())
        super().__init__(self.environ)

    async def _load_form_data(self):
        await get_event_loop().run_in_executor(None, super()._load_form_data)

    form = None

    async def get_form(self):
        await self._load_form_data()
        return self.form

    values = None

    async def get_values(self):
        await self._load_form_data()


class ASGIResponse(Response):
    async def _asyncify(self, iterable):
        for item in iterable:
            yield item

    async def aiter_encoded(self, iterable):
        async for item in iterable:
            if isinstance(item, text_type):
                yield item.encode(self.charset)
            else:
                yield item

    def get_app_iter(self, environ):
        resp = self.response
        status = self.status_code
        if environ['REQUEST_METHOD'] == 'HEAD' or \
           100 <= status < 200 or status in (204, 304):
            iterable = ()
        elif self.direct_passthrough:
            if __debug__:
                _warn_if_string(self.response)
            return self.response
        else:
            iterable = self.response
        if not isinstance(iterable, AsyncIterable):
            coro = self._asyncify(iterable)
        else:
            coro = iterable
        return self.aiter_encoded(coro)

    def __call__(self, scope):
        environ = asgi_scope_to_environ(scope)
        app_iter, status, headers = self.get_wsgi_response(environ)
        async def response(receiver, sender):
            await sender({
                'type': 'http.response.start',
                'status': self.status_code,
                'headers': headers
            })
            try:
                l = len(app_iter)
            except Exception:
                l = None
            i = 0
            async for item in app_iter:
                event = {
                    'type': 'http.response.body',
                    'body': item
                }
                if l is None or i < l:
                    event['more_body'] = True
                await sender(event)
                i += 1
            if l is None:
                await sender({
                    'type': 'http.response.body'
                })
        return response
