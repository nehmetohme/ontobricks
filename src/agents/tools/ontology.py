"""
Shared ontology tools used by the auto-mapping and auto-icon-assign agents.

Provides a tool to retrieve ontology entities and relationships from the ToolContext.
For incremental runs, already mapped and user-excluded attributes are omitted.
"""

import json
from typing import Callable, Dict, List

from back.core.logging import get_logger
from agents.tools.context import ToolContext

logger = get_logger(__name__)

# =====================================================
# Tool implementation
# =====================================================


def tool_get_ontology(ctx: ToolContext, **_kwargs) -> str:
    """Return ontology items and every attribute still needing a mapping."""
    logger.info("tool_get_ontology: retrieving ontology data")
    ontology = ctx.ontology or {}
    entities = ontology.get("entities", [])
    relationships = ontology.get("relationships", [])

    mapping_by_uri = {
        mapping.get("ontology_class") or mapping.get("class_uri", ""): mapping
        for mapping in (ctx.entity_mappings or [])
    }

    pending_entities: List[dict] = []
    for entity in entities:
        uri = entity.get("uri", "")
        existing = mapping_by_uri.get(uri, {})
        mapped_attributes = set((existing.get("attribute_mappings") or {}).keys())
        excluded_attributes = set(existing.get("excluded_attributes") or [])
        attributes = entity.get("attributes", []) or []
        pending_attributes = [
            attribute
            for attribute in attributes
            if attribute not in mapped_attributes and attribute not in excluded_attributes
        ]

        pending_entity = {**entity, "attributes": pending_attributes}
        if existing:
            pending_entity["existing_mapping"] = {
                key: existing.get(key)
                for key in (
                    "sql_query",
                    "id_column",
                    "label_column",
                    "attribute_mappings",
                    "unmapped_attributes",
                    "excluded_attributes",
                )
                if existing.get(key) not in (None, [], {})
            }
        pending_entities.append(pending_entity)
        logger.debug(
            "tool_get_ontology: entity '%s' — pending=%d, mapped=%d, excluded=%d",
            entity.get("name", "?"),
            len(pending_attributes),
            len(mapped_attributes),
            len(excluded_attributes),
        )

    logger.info(
        "tool_get_ontology: returning %d entities, %d relationships",
        len(pending_entities),
        len(relationships),
    )
    return json.dumps(
        {
            "entities": pending_entities,
            "relationships": relationships,
            "entity_count": len(pending_entities),
            "relationship_count": len(relationships),
        }
    )


# =====================================================
# OpenAI function-calling definition
# =====================================================

ONTOLOGY_TOOL_DEFINITIONS: List[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_ontology",
            "description": (
                "Get the ontology entities and relationships that need SQL mappings. Entity "
                "attributes include every pending, non-excluded attribute. Partially mapped "
                "entities include existing_mapping; extend its SQL projection and retain all "
                "existing output columns when submitting the updated mapping."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

ONTOLOGY_TOOL_HANDLERS: Dict[str, Callable] = {
    "get_ontology": tool_get_ontology,
}
