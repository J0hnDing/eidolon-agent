from __future__ import annotations

import pytest

from app.services.assistant_assessment_service import AssistantAssessmentService
from app.services.atlas_provider import UrllibAtlasProviderAdapter
from app.services.github_provider import IntegrationProviderError


def test_cleanup_reads_current_derived_progress_without_legacy_progression(monkeypatch):
    def request(self, path, payload, **kwargs):
        assert path == "/api/records?category=goal"
        assert kwargs["method"] == "GET"
        return [
            {"id": goal_id, "category": "goal", "title": goal_id, "data": {}, "progress": progress}
            for goal_id, progress in (("unassessed", None), ("partial", 99.5), ("done", 100.0))
        ]

    monkeypatch.setattr(UrllibAtlasProviderAdapter, "_request", request)
    assert AssistantAssessmentService._inactive_goals({
        "goal:unassessed", "goal:partial", "goal:done", "subgoal:done", "subgoal:removed", "goal:missing",
    }) == {"goal:done", "subgoal:done", "subgoal:removed", "goal:missing"}


@pytest.mark.parametrize("progress", [True, "100", -1, 101, float("nan"), float("inf")])
def test_cleanup_rejects_invalid_progress_instead_of_deleting_from_incomplete_source(monkeypatch, progress):
    monkeypatch.setattr(UrllibAtlasProviderAdapter, "_request", lambda *args, **kwargs: [
        {"id": "goal", "category": "goal", "title": "Goal", "data": {}, "progress": progress},
    ])
    with pytest.raises(IntegrationProviderError, match="invalid Goal progress"):
        AssistantAssessmentService._inactive_goals({"goal:goal", "goal:missing"})
