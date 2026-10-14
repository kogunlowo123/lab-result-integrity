"""Service metrics exposed on /metrics in Prometheus text format (own registry)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class IntegrityMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.requests = Histogram(
            "lri_http_request_duration_seconds",
            "HTTP request latency",
            ["method", "route", "status"],
            registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
        )
        self.results_screened = Counter(
            "lri_results_screened_total",
            "Results screened, by disposition",
            ["disposition"],
            registry=self.registry,
        )
        self.findings = Counter(
            "lri_findings_total",
            "Integrity findings raised",
            ["kind", "severity"],
            registry=self.registry,
        )
        self.cases_opened = Counter(
            "lri_review_cases_opened_total",
            "Pathologist review cases opened",
            ["priority"],
            registry=self.registry,
        )
        self.review_decisions = Counter(
            "lri_review_decisions_total",
            "Pathologist decisions recorded",
            ["outcome"],
            registry=self.registry,
        )
        self.proposals = Counter(
            "lri_agent_proposals_total",
            "Agent disposition proposals recorded pending human approval",
            ["outcome"],
            registry=self.registry,
        )
        self.cases_overdue = Gauge(
            "lri_review_cases_overdue",
            "Undecided review cases past their SLA at the last sweep",
            registry=self.registry,
        )
        self.escalations = Counter(
            "lri_review_escalations_total",
            "Review cases escalated after missing their SLA",
            registry=self.registry,
        )
        self.pbrtqc_alarms = Counter(
            "lri_pbrtqc_alarms_total",
            "Patient-based QC alarms started",
            ["analyte"],
            registry=self.registry,
        )
        self.authz_decisions = Counter(
            "lri_authorization_decisions_total",
            "Authorization outcomes",
            ["action", "decision"],
            registry=self.registry,
        )
        self.narratives = Counter(
            "lri_evidence_narratives_total",
            "Evidence narrative drafting outcomes",
            ["status"],
            registry=self.registry,
        )
        self.mcp_calls = Counter(
            "lri_mcp_tool_calls_total",
            "MCP tool invocations",
            ["tool", "outcome"],
            registry=self.registry,
        )
        self.a2a_requests = Counter(
            "lri_a2a_requests_total",
            "A2A JSON-RPC requests",
            ["method", "outcome"],
            registry=self.registry,
        )
