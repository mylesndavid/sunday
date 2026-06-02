"""argus — tiny Python SDK to send agent traces to Argus.

    from argus import Argus
    argus = Argus(url="http://localhost:4317", service="my-agent")
    t = argus.trace("handle_request", session="sess-1", user="u-42")
    llm = t.span("chat", kind="llm", model="gpt-4o", input=prompt)
    llm.end(output=answer, input_tokens=800, output_tokens=120)
    tool = t.span("search", kind="tool", tool="web_search")
    tool.end(output=results)
    t.end()
"""
import json, os, time, secrets, urllib.request, functools, asyncio, contextvars

_current = contextvars.ContextVar("argus_trace", default=None)  # current trace (auto-nesting)


def _short(x, n=600):
    try:
        return x if isinstance(x, str) else json.dumps(x)[:n]
    except Exception:
        return str(x)[:n]


def _rid(n=16):
    return secrets.token_hex(n // 2)


class _Span:
    def __init__(self, trace, name, kind="other", model=None, tool=None, input=None, parent=None, attributes=None):
        self.trace = trace
        self.d = {
            "span_id": _rid(8), "trace_id": trace.trace_id, "parent_id": parent or trace.root_id,
            "name": name, "kind": kind, "service": trace.service, "model": model, "tool_name": tool,
            "start_ms": int(time.time() * 1000), "session_id": trace.session, "user_id": trace.user,
            "input": input, "attributes": attributes or {},
        }

    def end(self, output=None, input_tokens=0, output_tokens=0, error=None):
        self.d.update(end_ms=int(time.time() * 1000), output=output, input_tokens=input_tokens,
                      output_tokens=output_tokens, status="error" if error else "ok", error=error)
        self.trace.spans.append(self.d)
        return self


class _Trace:
    def __init__(self, argus, name, session=None, user=None):
        self.argus, self.trace_id, self.root_id = argus, _rid(16), _rid(8)
        self.service, self.session, self.user = argus.service, session, user
        self.spans = [{"span_id": self.root_id, "trace_id": self.trace_id, "parent_id": None,
                       "name": name, "kind": "agent", "service": self.service,
                       "start_ms": int(time.time() * 1000), "session_id": session, "user_id": user}]

    def span(self, name, **kw):
        return _Span(self, name, **kw)

    def end(self, error=None):
        self.spans[0].update(end_ms=int(time.time() * 1000), status="error" if error else "ok", error=error)
        self.argus.send(self.spans)


class Argus:
    def __init__(self, url="http://localhost:4317", service="agent"):
        self.url, self.service = url.rstrip("/"), service

    def trace(self, name, session=None, user=None):
        return _Trace(self, name, session=session, user=user)

    def send(self, spans):
        try:
            req = urllib.request.Request(self.url + "/api/v1/traces",
                                         data=json.dumps({"spans": spans}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass  # never crash the host app

    # ---- auto-instrumentation wrappers ----

    def traced(self, name, session=None, user=None):
        """Decorator: wrap a function so the whole call becomes a trace.
        Anything traced/tool/wrapped inside it auto-nests.
            @argus.traced("handle_request")
            async def handle(req): ...
        """
        def deco(fn):
            def _start():
                t = self.trace(name, session=session, user=user)
                return t, _current.set(t)
            if asyncio.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def aw(*a, **k):
                    t, tok = _start()
                    try:
                        r = await fn(*a, **k); t.end(); return r
                    except Exception as e:
                        t.end(error=str(e)); raise
                    finally:
                        _current.reset(tok)
                return aw

            @functools.wraps(fn)
            def sw(*a, **k):
                t, tok = _start()
                try:
                    r = fn(*a, **k); t.end(); return r
                except Exception as e:
                    t.end(error=str(e)); raise
                finally:
                    _current.reset(tok)
            return sw
        return deco

    def tool(self, name):
        """Decorator: wrap a tool/function so every call becomes a tool span."""
        def deco(fn):
            def _span(args):
                t = _current.get() or self.trace(name)
                return t, _current.get() is None, t.span(name, kind="tool", tool=name, input=_short(args))
            if asyncio.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def aw(*a, **k):
                    t, standalone, s = _span(a)
                    try:
                        r = await fn(*a, **k); s.end(output=_short(r));  t.end() if standalone else None; return r
                    except Exception as e:
                        s.end(error=str(e)); t.end(error=str(e)) if standalone else None; raise
                return aw

            @functools.wraps(fn)
            def sw(*a, **k):
                t, standalone, s = _span(a)
                try:
                    r = fn(*a, **k); s.end(output=_short(r)); t.end() if standalone else None; return r
                except Exception as e:
                    s.end(error=str(e)); t.end(error=str(e)) if standalone else None; raise
            return sw
        return deco

    def wrap_openai(self, client):
        """Auto-instrument an OpenAI client: every chat.completions.create becomes an LLM span."""
        orig = client.chat.completions.create

        def create(*a, **k):
            t = _current.get() or self.trace("chat " + k.get("model", ""))
            standalone = _current.get() is None
            s = t.span("chat " + k.get("model", ""), kind="llm", model=k.get("model"), input=_short(k.get("messages")))
            try:
                res = orig(*a, **k)
                u = getattr(res, "usage", None)
                out = res.choices[0].message.content if getattr(res, "choices", None) else ""
                s.end(output=out, input_tokens=getattr(u, "prompt_tokens", 0) if u else 0, output_tokens=getattr(u, "completion_tokens", 0) if u else 0)
                if standalone: t.end()
                return res
            except Exception as e:
                s.end(error=str(e))
                if standalone: t.end(error=str(e))
                raise
        client.chat.completions.create = create
        return client
