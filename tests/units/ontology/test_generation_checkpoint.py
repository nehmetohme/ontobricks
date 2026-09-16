"""Server-side ontology generation checkpoint persistence tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from back.objects.ontology import Ontology

_SESSION_ID = "0123456789abcdef0123456789abcdef"
_TURTLE = (
    "@prefix : <http://example.org/ecommerce#> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    ":Order a owl:Class .\n"
)


@pytest.mark.unit
def test_generation_checkpoint_persists_one_bounded_session_value(tmp_path: Path):
    session_path = tmp_path / _SESSION_ID
    session_path.write_text(json.dumps({"domain_data": {"domain": {}}}))
    session_ref = {"domain_data": {"domain": {}}}
    settings = MagicMock(session_dir=str(tmp_path))

    with patch("back.objects.ontology.Ontology.get_settings", return_value=settings):
        saved = Ontology.save_generation_checkpoint_to_session(
            _SESSION_ID,
            session_ref,
            content=_TURTLE,
            class_count=1,
            iteration=2,
            task_id="task-123",
        )

    assert saved is True
    checkpoint = json.loads(session_path.read_text())["ontology_generation_checkpoint"]
    assert checkpoint == session_ref["ontology_generation_checkpoint"]
    assert checkpoint["content"] == _TURTLE
    assert checkpoint["class_count"] == 1
    assert checkpoint["iteration"] == 2
    assert checkpoint["task_id"] == "task-123"


@pytest.mark.unit
def test_generation_checkpoint_rejects_traversal_session_id(tmp_path: Path):
    settings = MagicMock(session_dir=str(tmp_path))

    with patch("back.objects.ontology.Ontology.get_settings", return_value=settings):
        saved = Ontology.save_generation_checkpoint_to_session(
            "../evil",
            {},
            content=_TURTLE,
            class_count=1,
            iteration=1,
            task_id="task-123",
        )

    assert saved is False
    assert list(tmp_path.iterdir()) == []
