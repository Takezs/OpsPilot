import json

from opspilot.evaluation.report import build_report_artifacts


def test_report_is_deterministic_and_includes_metrics_faults_and_failures() -> None:
    rows = [
        {
            "case_id": "case-a",
            "repetition": 1,
            "latency_ms": 100,
            "scores": {"recall_at_5": 1.0, "mrr": 1.0, "task_success": True},
            "actual": {
                "fault_point": "before_execute",
                "fault_consumed": True,
                "recovered": True,
            },
            "error": None,
        },
        {
            "case_id": "case-a",
            "repetition": 2,
            "latency_ms": 300,
            "scores": {"recall_at_5": 0.0, "mrr": 0.5, "task_success": False},
            "actual": {
                "fault_point": "before_execute",
                "fault_consumed": True,
                "recovered": False,
                "duplicate_side_effect": False,
                "lost_operation": True,
                "recovery_ms": 250,
            },
            "error": "safe failure",
        },
    ]

    artifacts = build_report_artifacts("run-1", {"top_k": 5}, rows)
    result = json.loads(artifacts.json_bytes)

    assert result["metrics"]["recall_at_5"] == {"mean": 0.5, "stddev": 0.5}
    assert result["metrics"]["task_success_rate"] == 0.5
    assert result["metrics"]["p95_latency_ms"] == 300
    assert result["fault_matrix"]["before_execute"] == {
        "attempted": 2,
        "consumed": 2,
        "recovery_rate": 0.5,
        "duplicate_side_effect_rate": 0.0,
        "lost_operation_rate": 0.5,
        "mean_recovery_ms": 250.0,
    }
    assert result["failures"][0]["case_id"] == "case-a"
    assert result["failures"][0]["scores"]["task_success"] is False
    assert b"metric,value" in artifacts.csv_bytes
    assert b"OpsPilot Evaluation" in artifacts.html_bytes
