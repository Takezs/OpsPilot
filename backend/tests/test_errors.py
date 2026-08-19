from opspilot.api.errors import ApiError, ErrorResponse


def test_error_response_has_stable_machine_code_and_request_id() -> None:
    response = ErrorResponse(
        error=ApiError(code="VALIDATION_ERROR", message="Invalid request"),
        request_id="req-123",
    )

    assert response.model_dump() == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "Invalid request",
            "details": None,
        },
        "request_id": "req-123",
    }
