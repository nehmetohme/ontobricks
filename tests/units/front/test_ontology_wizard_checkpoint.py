"""Ontology wizard checkpoint recovery frontend contract."""

from pathlib import Path


def test_wizard_recovers_server_checkpoint_after_interrupted_or_failed_task():
    source = Path(
        "src/front/static/ontology/js/ontology-wizard.js"
    ).read_text(encoding="utf-8")

    assert "/ontology/wizard/checkpoint" in source
    assert "recovered_from_checkpoint" in source
    assert "Recovered the latest valid ontology" in source
