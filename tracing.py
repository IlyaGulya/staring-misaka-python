"""OpenTelemetry tracing helpers for the spam detection bot."""

from opentelemetry import trace

tracer = trace.get_tracer("staring-misaka")
