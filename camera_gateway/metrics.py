"""Prometheus metrics. Labels are bounded enums only: never camera IDs, hosts, refs or tenants."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.http_requests = Counter(
            "camera_gateway_http_requests_total",
            "HTTP requests handled",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.http_latency = Histogram(
            "camera_gateway_http_request_duration_seconds",
            "HTTP request latency",
            ["method", "route"],
            registry=self.registry,
        )
        self.camera_ops = Counter(
            "camera_gateway_camera_operations_total",
            "Upstream camera operations by outcome",
            ["operation", "outcome"],
            registry=self.registry,
        )
        self.camera_op_latency = Histogram(
            "camera_gateway_camera_operation_duration_seconds",
            "Upstream camera operation latency",
            ["operation"],
            buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60),
            registry=self.registry,
        )
        self.cameras_by_status = Gauge(
            "camera_gateway_cameras",
            "Registered cameras by last observed health status",
            ["status"],
            registry=self.registry,
        )
        self.secret_resolutions = Counter(
            "camera_gateway_secret_resolutions_total",
            "Secret reference resolutions by outcome",
            ["outcome"],
            registry=self.registry,
        )
        self.preview_sessions = Gauge(
            "camera_gateway_mjpeg_preview_sessions",
            "Active operator MJPEG preview sessions",
            registry=self.registry,
        )
