import logging

from test_service.config import AppSettings


def setup_logging(settings: AppSettings):
    """Emit structured application diagnostics and revision identity without raw exceptions."""
    from pythonjsonlogger.json import JsonFormatter

    formatter = JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "level"},
        static_fields={"service": settings.otel_service_name},
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    logging.getLogger(__name__).info(
        "Service starting",
        extra={
            "deployed_revision": settings.deployed_revision,
        },
    )


def setup_telemetry(app, settings: AppSettings):
    """Install redacted HTTP tracing; setup failures log a fixed warning without exception details."""
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": settings.otel_service_name,
                "deployment.environment": "dev",
            }
        )
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_endpoint,
            insecure=True,
            timeout=5,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        instrument_http(app, provider)
    except Exception:
        logging.getLogger(__name__).warning("OpenTelemetry setup failed — tracing disabled")


def instrument_http(app, provider) -> None:
    """Retain request traces while redacting raw URL fields and all captured header values.

    Request hooks run after initial attribute collection. Route templates remain
    useful for diagnosis without exporting patient path values or query secrets.
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    def safe_request(span, scope):
        """Replace user-controlled location fields with a fixed marker before export."""
        if span and span.is_recording():
            for name in (
                "http.target",
                "http.url",
                "url.full",
                "url.path",
                "url.query",
                "http.user_agent",
                "user_agent.original",
            ):
                span.set_attribute(name, "[REDACTED]")

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        server_request_hook=safe_request,
        http_capture_headers_sanitize_fields=[".*"],
    )
