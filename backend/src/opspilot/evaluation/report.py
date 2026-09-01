"""Deterministic JSON, CSV and HTML artifacts from persisted evaluation facts."""

import csv
import html
import io
import json
import math
import statistics
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReportArtifacts:
    json_bytes: bytes
    csv_bytes: bytes
    html_bytes: bytes


def _mean_std(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "stddev": 0.0}
    return {"mean": statistics.fmean(values), "stddev": statistics.pstdev(values)}


def _rate(values: list[bool]) -> float:
    return statistics.fmean(int(value) for value in values) if values else 0.0


def build_report_artifacts(
    run_id: str, configuration: dict[str, object], rows: list[dict[str, Any]]
) -> ReportArtifacts:
    score_names = (
        "recall_at_5",
        "precision_at_5",
        "mrr",
        "ndcg_at_5",
        "citation_precision",
        "citation_recall",
        "tool_precision",
        "tool_recall",
        "tool_f1",
    )
    metrics: dict[str, object] = {
        name: _mean_std([float(row["scores"][name]) for row in rows if name in row["scores"]])
        for name in score_names
    }
    metrics["task_success_rate"] = _rate(
        [bool(row["scores"].get("task_success", False)) for row in rows]
    )
    metrics["citation_correctness_rate"] = _rate(
        [
            float(row["scores"].get("citation_precision", 0.0)) == 1.0
            and float(row["scores"].get("citation_recall", 0.0)) == 1.0
            for row in rows
        ]
    )
    latencies = sorted(int(row["latency_ms"]) for row in rows)
    metrics["p95_latency_ms"] = (
        latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)] if latencies else 0
    )
    metrics["unapproved_execution_rate"] = _rate(
        [bool(row["actual"].get("unapproved_execution", False)) for row in rows]
    )
    metrics["duplicate_side_effect_rate"] = _rate(
        [bool(row["actual"].get("duplicate_side_effect", False)) for row in rows]
    )
    fault_matrix: dict[str, object] = {}
    fault_points = sorted(
        {str(row["actual"]["fault_point"]) for row in rows if row["actual"].get("fault_point")}
    )
    for point in fault_points:
        samples = [row for row in rows if row["actual"].get("fault_point") == point]
        recovery_times = [
            float(row["actual"]["recovery_ms"])
            for row in samples
            if row["actual"].get("recovery_ms") is not None
        ]
        fault_matrix[point] = {
            "runs": len(samples),
            "recovery_rate": _rate(
                [bool(row["actual"].get("recovered", False)) for row in samples]
            ),
            "duplicate_side_effect_rate": _rate(
                [bool(row["actual"].get("duplicate_side_effect", False)) for row in samples]
            ),
            "lost_operation_rate": _rate(
                [bool(row["actual"].get("lost_operation", False)) for row in samples]
            ),
            "mean_recovery_ms": statistics.fmean(recovery_times) if recovery_times else 0.0,
        }
    failures = [
        {
            "case_id": row["case_id"],
            "repetition": row["repetition"],
            "error": row["error"],
        }
        for row in rows
        if row.get("error") or not bool(row["scores"].get("task_success", True))
    ]
    payload = {
        "run_id": run_id,
        "configuration": configuration,
        "metrics": metrics,
        "fault_matrix": fault_matrix,
        "failures": failures,
    }
    json_bytes = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    csv_stream = io.StringIO(newline="")
    writer = csv.writer(csv_stream, lineterminator="\n")
    writer.writerow(["metric", "value"])
    for name, value in sorted(metrics.items()):
        writer.writerow([name, json.dumps(value, sort_keys=True, separators=(",", ":"))])
    csv_bytes = csv_stream.getvalue().encode("utf-8")
    escaped = html.escape(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    html_bytes = (
        '<!doctype html><html><head><meta charset="utf-8"><title>OpsPilot Evaluation'
        "</title></head><body><h1>OpsPilot Evaluation</h1><pre>" + escaped + "</pre></body></html>"
    ).encode("utf-8")
    return ReportArtifacts(json_bytes=json_bytes, csv_bytes=csv_bytes, html_bytes=html_bytes)
