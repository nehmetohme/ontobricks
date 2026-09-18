"""Regression tests for agents/tools/mapping.py's SQL deduplication guard.

Real-world bug: the auto-mapping agent's own prompt instructs it to alias
the id/label source column (e.g. "AS Label") *and* repeat it under its
original name for attribute mapping. When that original name is itself
"id"/"label" (case-insensitive) — e.g. a `gold_segment` table with an
actual column named `label` — this produces two columns Databricks
resolves as the same one, and the digital-twin build fails with
`AMBIGUOUS_REFERENCE` at Sync time. `tool_submit_entity_mapping` /
`tool_submit_relationship_mapping` must deduplicate before persisting the
mapping, regardless of what the LLM actually generated.
"""

import json

import pytest

from agents.tools.context import ToolContext
from agents.tools.mapping import (
    tool_submit_entity_mapping,
    tool_submit_relationship_mapping,
)


@pytest.fixture
def ctx():
    return ToolContext(host="https://test.databricks.com", token="fake-token")


@pytest.mark.unit
class TestEntityMappingLabelCollision:
    def test_label_column_named_label_is_deduplicated(self, ctx):
        """The exact production shape: label_column="Label" sourced from a
        physical column literally named "label", also listed as a
        standalone attribute — the classic self-collision."""
        sql = (
            "SELECT segment_id AS ID, label AS Label, segment_id, label, description "
            "FROM catalog.schema.gold_segment WHERE segment_id IS NOT NULL"
        )
        tool_submit_entity_mapping(
            ctx,
            class_uri="http://test.org/ontology#Segment",
            class_name="Segment",
            sql_query=sql,
            id_column="ID",
            label_column="Label",
            attribute_mappings={"hasLabel": "label", "hasDescription": "description"},
        )
        assert len(ctx.entity_mappings) == 1
        stored_sql = ctx.entity_mappings[0]["sql_query"]

        # The duplicate "label" column must be renamed, not left colliding
        # with "Label" under case-insensitive resolution.
        assert "label AS Label" in stored_sql
        assert ", label," not in stored_sql, f"unresolved duplicate in: {stored_sql}"

    def test_no_collision_when_columns_are_distinct(self, ctx):
        """Sanity check: distinct column names pass through unchanged."""
        sql = (
            "SELECT viewer_id AS ID, display_name AS Label, viewer_id, display_name, region "
            "FROM catalog.schema.gold_viewer WHERE viewer_id IS NOT NULL"
        )
        tool_submit_entity_mapping(
            ctx,
            class_uri="http://test.org/ontology#Viewer",
            class_name="Viewer",
            sql_query=sql,
            id_column="ID",
            label_column="Label",
            attribute_mappings={"hasRegion": "region"},
        )
        stored_sql = ctx.entity_mappings[0]["sql_query"]
        assert "viewer_id AS ID" in stored_sql
        assert "display_name AS Label" in stored_sql


@pytest.mark.unit
class TestRelationshipMappingDedup:
    def test_relationship_columns_are_deduplicated_too(self, ctx):
        sql = (
            "SELECT viewer_id AS source_id, segment_id AS target_id "
            "FROM catalog.schema.gold_viewer WHERE viewer_id IS NOT NULL AND segment_id IS NOT NULL"
        )
        tool_submit_relationship_mapping(
            ctx,
            property_uri="http://test.org/ontology#belongsTo",
            property_name="belongsTo",
            sql_query=sql,
            source_id_column="source_id",
            target_id_column="target_id",
            domain="http://test.org/ontology#Viewer",
            range_class="http://test.org/ontology#Segment",
        )
        assert len(ctx.relationships) == 1
        stored_sql = ctx.relationships[0]["sql_query"]
        assert "AS source_id" in stored_sql
        assert "AS target_id" in stored_sql


@pytest.mark.unit
class TestIncrementalEntityMapping:
    URI = "http://test.org/ontology#Customer"

    @pytest.fixture
    def partial_ctx(self):
        return ToolContext(
            host="https://test.databricks.com",
            token="fake-token",
            ontology={
                "entities": [
                    {
                        "uri": self.URI,
                        "name": "Customer",
                        "attributes": ["firstName", "lastName"],
                    }
                ],
                "relationships": [],
            },
            entity_mappings=[
                {
                    "ontology_class": self.URI,
                    "class_name": "Customer",
                    "sql_query": (
                        "SELECT customer_id AS ID, display_name AS Label, "
                        "first_name FROM catalog.schema.customers"
                    ),
                    "id_column": "ID",
                    "label_column": "Label",
                    "attribute_mappings": {"firstName": "first_name"},
                    "excluded_attributes": ["internalNote"],
                }
            ],
        )

    def test_extends_existing_mapping_when_projection_is_complete(self, partial_ctx):
        result = json.loads(
            tool_submit_entity_mapping(
                partial_ctx,
                class_uri=self.URI,
                class_name="Customer",
                sql_query=(
                    "SELECT customer_id AS ID, display_name AS Label, first_name, "
                    "last_name FROM catalog.schema.customers"
                ),
                id_column="ID",
                label_column="Label",
                attribute_mappings={"lastName": "last_name"},
            )
        )

        assert result["success"] is True
        assert partial_ctx.entity_mappings[0]["attribute_mappings"] == {
            "firstName": "first_name",
            "lastName": "last_name",
        }
        assert partial_ctx.entity_mappings[0]["excluded_attributes"] == [
            "internalNote"
        ]

    def test_rejects_sql_that_drops_existing_mapped_column(self, partial_ctx):
        original = dict(partial_ctx.entity_mappings[0])

        result = json.loads(
            tool_submit_entity_mapping(
                partial_ctx,
                class_uri=self.URI,
                class_name="Customer",
                sql_query=(
                    "SELECT customer_id AS ID, display_name AS Label, last_name "
                    "FROM catalog.schema.customers"
                ),
                id_column="ID",
                label_column="Label",
                attribute_mappings={"lastName": "last_name"},
            )
        )

        assert result["error"] == "sql_query drops mapped output columns"
        assert result["missing_columns"] == ["first_name"]
        assert partial_ctx.entity_mappings[0] == original
