from yuki.cognition.error_context import ModelErrorContext, classify_model_error


def test_model_error_context_records_recent_incidents_with_correlation_ids():
    now = [100.0]
    context = ModelErrorContext(max_incidents=2, clock=lambda: now[0])

    first = context.record("vlm", "timeout waiting for result", correlation_id="same")
    now[0] = 101.0
    second = context.record("stt", "plain failure")
    context.record("local_chat", "later failure")

    incidents = context.recent_incidents()

    assert first["kind"] == "timeout"
    assert first["correlation_id"] == "same"
    assert len(second["correlation_id"]) > 0
    assert [incident["model"] for incident in incidents] == ["stt", "local_chat"]
    assert incidents[-1]["ts"] == 101.0


def test_classify_model_error_marks_oom():
    assert classify_model_error("CUDA out of memory while allocating") == "gpu_oom"
