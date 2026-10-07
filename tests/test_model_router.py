"""
Tests for core.model_router -- the Hybrid Model Routing layer.
"""
from unittest.mock import patch

from core.model_router import route_llm_call, _decide_backend, TaskType
from core.config import settings


def test_auto_mode_routes_explain_normal_priority_to_local():
    settings.routing_mode = "auto"
    assert _decide_backend(TaskType.EXPLAIN, "normal") == "local"


def test_auto_mode_escalates_high_priority_explain_to_cloud():
    settings.routing_mode = "auto"
    assert _decide_backend(TaskType.EXPLAIN, "high") == "cloud"


def test_auto_mode_always_routes_patch_generation_to_cloud():
    settings.routing_mode = "auto"
    assert _decide_backend(TaskType.GENERATE_PATCH, "normal") == "cloud"
    assert _decide_backend(TaskType.GENERATE_PATCH, "high") == "cloud"


def test_local_mode_forces_local_regardless_of_task():
    settings.routing_mode = "local"
    assert _decide_backend(TaskType.GENERATE_PATCH, "high") == "local"
    settings.routing_mode = "auto"


def test_cloud_mode_forces_cloud_regardless_of_task():
    settings.routing_mode = "cloud"
    assert _decide_backend(TaskType.EXPLAIN, "normal") == "cloud"
    settings.routing_mode = "auto"


def test_route_llm_call_uses_chosen_backend_on_success():
    settings.routing_mode = "auto"
    with patch("core.model_router.call_local_llm_json", return_value={"ok": True}) as local_mock, \
         patch("core.model_router.call_cloud_llm_json", return_value={"ok": True}) as cloud_mock:
        response, backend = route_llm_call(TaskType.EXPLAIN, "sys", "user", priority="normal")
    assert backend == "local"
    assert local_mock.called
    assert not cloud_mock.called


def test_route_llm_call_falls_back_when_primary_fails():
    settings.routing_mode = "auto"
    with patch("core.model_router.call_local_llm_json", side_effect=Exception("down")), \
         patch("core.model_router.call_cloud_llm_json", return_value={"ok": True, "source": "cloud"}) as cloud_mock:
        response, backend = route_llm_call(TaskType.EXPLAIN, "sys", "user", priority="normal")
    assert backend == "cloud"
    assert cloud_mock.called
    assert response["source"] == "cloud"


def test_route_llm_call_raises_when_both_backends_fail():
    settings.routing_mode = "auto"
    with patch("core.model_router.call_local_llm_json", side_effect=Exception("local down")), \
         patch("core.model_router.call_cloud_llm_json", side_effect=Exception("cloud down")):
        try:
            route_llm_call(TaskType.EXPLAIN, "sys", "user", priority="normal")
            assert False, "expected an exception to be raised"
        except Exception as e:
            assert "Both local and cloud" in str(e)