"""Process entry point: ``python -m lab_result_integrity``."""

from __future__ import annotations

import os

import uvicorn
from agentic_core.observability import configure_logging, configure_telemetry
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from lab_result_integrity import __version__
from lab_result_integrity.api.app import create_app
from lab_result_integrity.config import Settings
from lab_result_integrity.runtime.container import build_container


def main() -> None:
    settings = Settings()  # populated from LRI_* environment variables
    configure_logging(
        service="lab-result-integrity", environment=settings.environment, level=settings.log_level
    )
    exporter = None
    if settings.otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter(endpoint=settings.otlp_endpoint)
    configure_telemetry(
        service_name="lab-result-integrity",
        service_version=__version__,
        environment=settings.environment,
        span_exporter=exporter,
    )
    app = create_app(lambda: build_container(settings), public_host=settings.public_host)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
    uvicorn.run(
        app,
        host=os.environ.get("LRI_HOST", "127.0.0.1"),
        port=int(os.environ.get("LRI_PORT", "8080")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("LRI_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
