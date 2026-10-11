"""OpenTelemetry spans with a restricted, durable local exporter and W3C propagation."""

import logging
from contextlib import contextmanager
from functools import wraps

from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from psycopg.types.json import Jsonb

from commerce.db import connect_sync

logger = logging.getLogger(__name__)
ALLOWED = {
    "task_id",
    "task_type",
    "application_id",
    "tool",
    "agent",
    "http.route",
    "http.method",
    "http.status_code",
    "transport",
    "event_id",
    "generation",
    "redis.available",
}


class DatabaseExporter(SpanExporter):
    def export(self, spans):
        try:
            with connect_sync() as conn:
                for item in spans:
                    conn.execute(
                        "INSERT INTO commerce.trace_spans VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (
                            format(item.context.trace_id, "032x"),
                            format(item.context.span_id, "016x"),
                            format(item.parent.span_id, "016x") if item.parent else None,
                            item.name,
                            item.start_time,
                            item.end_time,
                            item.status.status_code.name,
                            Jsonb({k: v for k, v in item.attributes.items() if k in ALLOWED}),
                        ),
                    )
            return SpanExportResult.SUCCESS
        except Exception as exc:
            logger.warning("Trace export unavailable (%s)", type(exc).__name__)
            return SpanExportResult.FAILURE


provider = TracerProvider(resource=Resource.create({"service.name": "commerce-after-sales"}))
provider.add_span_processor(SimpleSpanProcessor(DatabaseExporter()))
tracer = provider.get_tracer("commerce.v3.1")


def configure_export():
    from commerce.config import CommerceSettings

    endpoint = CommerceSettings().OTEL_EXPORTER_OTLP_ENDPOINT
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces"))
        )


def carrier():
    result = {}
    inject(result)
    return result.get("traceparent", "")


@contextmanager
def span(name, *, parent=None, **attributes):
    context = extract({"traceparent": parent}) if parent else None
    with tracer.start_as_current_span(
        name,
        context=context,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except BaseException:
            current.set_status(trace.Status(trace.StatusCode.ERROR))
            raise


def instrument(name, **attributes):
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            with span(name, **attributes):
                return await function(*args, **kwargs)

        return wrapped

    return decorate


class TraceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not (
            scope["path"].startswith("/api/v1/")
            or scope["path"].startswith("/internal/commerce-mcp")
        ):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        # Browser-provided trace IDs cannot join another customer's internal trace.
        parent = (
            headers.get(b"traceparent", b"").decode()
            if scope["path"].startswith("/internal/")
            else None
        )
        with span(
            "api.request" if parent is None else "mcp.server",
            parent=parent,
            **{"http.method": scope["method"]},
        ):

            async def traced_send(message):
                if message["type"] == "http.response.start":
                    trace.get_current_span().set_attribute("http.status_code", message["status"])
                    route = scope.get("route")
                    trace.get_current_span().set_attribute(
                        "http.route", getattr(route, "path", "/internal/commerce-mcp")
                    )
                await send(message)

            await self.app(scope, receive, traced_send)
