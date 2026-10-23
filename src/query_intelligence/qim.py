"""Initial query intent mapping from question text to structured intent."""

from __future__ import annotations

import re

from src.evidence.schema import load_schema_snapshot
from src.query_intelligence.models import QueryFilter, QueryIntent, QueryRequest


UNSUPPORTED_OPERATIONS = frozenset(
    {"insert", "update", "delete", "create", "drop", "alter", "truncate"}
)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", text.casefold())


def _contains_phrase(tokens: list[str], phrase: list[str]) -> bool:
    if not phrase or len(phrase) > len(tokens):
        return False

    return any(
        tokens[index : index + len(phrase)] == phrase
        for index in range(len(tokens) - len(phrase) + 1)
    )


def _is_count_request(question: str) -> bool:
    tokens = _tokens(question)
    return (
        "count" in tokens
        or _contains_phrase(tokens, ["how", "many"])
        or _contains_phrase(tokens, ["number", "of"])
    )


def _table_names_in_question(question: str) -> tuple[str, ...]:
    """Find table names; prefer longer names when table phrases overlap."""
    question_tokens = _tokens(question)
    snapshot = load_schema_snapshot()
    candidates: list[tuple[int, int, str]] = []

    for table in snapshot["tables"]:
        table_name = table["table_name"]
        table_tokens = table_name.split("_")
        phrase_variants = [table_tokens]

        if table_name == "order_items":
            phrase_variants.append(["order", "line", "items"])
            phrase_variants.append(["order", "line", "item"])

        # Natural questions often use the distinctive table noun without its
        # technical prefix, e.g. "reviews" for ``order_reviews``. This is safe
        # only when the final noun identifies one schema table.
        if len(table_tokens) > 1:
            phrase_variants.append([table_tokens[-1]])

        # Recognize simple singular forms, e.g. "order item".
        singular_variants = []
        if table_tokens[-1].endswith("s"):
            singular_variants.append(table_tokens[-1][:-1])
            phrase_variants.append(
                [*table_tokens[:-1], table_tokens[-1][:-1]]
            )

        if len(table_tokens) > 1:
            singular_variants.append(
                table_tokens[-1][:-1]
                if table_tokens[-1].endswith("s")
                else table_tokens[-1]
            )
            phrase_variants.extend([[word] for word in singular_variants])

        # Olist's physical lookup table includes "name" in its identifier;
        # users commonly say "category translation" instead.
        if "name" in table_tokens:
            phrase_variants.append(
                [token for token in table_tokens if token != "name"]
            )

        # Avoid duplicate variants before matching.
        phrase_variants = list(dict.fromkeys(tuple(phrase) for phrase in phrase_variants))

        for phrase in phrase_variants:
            for start in range(len(question_tokens) - len(phrase) + 1):
                if question_tokens[start : start + len(phrase)] == list(phrase):
                    candidates.append(
                        (start, start + len(phrase), table_name)
                    )

    # For example, "order items" should match order_items, not orders too.
    candidates.sort(
        key=lambda match: (match[0], -(match[1] - match[0]))
    )

    matches: list[tuple[int, int, str]] = []
    occupied_tokens: set[int] = set()

    for start, end, table_name in candidates:
        token_positions = set(range(start, end))
        if token_positions & occupied_tokens:
            continue

        matches.append((start, end, table_name))
        occupied_tokens.update(token_positions)

    matches.sort(key=lambda match: match[0])
    return tuple(
        dict.fromkeys(
            f"{snapshot['schema']}.{table_name}"
            for _, _, table_name in matches
        )
    )


def _classify_operation(question: str) -> str:
    question_tokens = _tokens(question)
    tokens = set(question_tokens)

    for operation in UNSUPPORTED_OPERATIONS:
        if operation in tokens:
            return operation

    if (
        "trend" in tokens
        or _contains_phrase(question_tokens, ["over", "time"])
    ):
        return "trend"

    if {"highest", "lowest", "top", "rank"} & tokens:
        return "rank"

    if "compare" in tokens or "versus" in tokens or "vs" in tokens:
        return "compare"

    if (
        "total" in tokens
        or "sum" in tokens
        or "average" in tokens
        or _is_count_request(question)
    ):
        return "aggregate"

    if "where" in tokens or "filter" in tokens:
        return "filter"

    return "select"


def _columns_mentioned(
    question: str,
    entities: tuple[str, ...],
) -> tuple[str, ...]:
    """Resolve unambiguous schema-column phrases within mentioned tables."""
    tokens = _tokens(question)
    snapshot = load_schema_snapshot()
    matches: list[tuple[int, int, str]] = []

    for entity in entities:
        table_name = entity.rsplit(".", maxsplit=1)[-1]
        table = next(
            (item for item in snapshot["tables"] if item["table_name"] == table_name),
            None,
        )
        if table is None:
            continue

        for column in table["columns"]:
            column_name = column["column_name"]
            column_tokens = column_name.split("_")
            phrases = [column_tokens]

            # In a table-specific context, users commonly shorten
            # customer_state/seller_state to just "state".
            if len(column_tokens) > 1:
                phrases.append([column_tokens[-1]])

            for phrase in phrases:
                for start in range(len(tokens) - len(phrase) + 1):
                    if tokens[start : start + len(phrase)] == phrase:
                        target = f"{entity}.{column_name}"
                        matches.append((start, len(phrase), target))

    # Prefer the longest, most specific field phrase at a given position.
    matches.sort(key=lambda item: (item[0], -item[1], item[2]))
    selected: list[str] = []
    occupied: set[int] = set()
    for start, phrase_length, target in matches:
        positions = set(range(start, start + phrase_length))
        if positions & occupied:
            continue
        selected.append(target)
        occupied.update(positions)

    return tuple(dict.fromkeys(selected))


def _strict_join_dimension(
    question: str,
    entities: tuple[str, ...],
) -> tuple[str, ...]:
    """Resolve an explicit per-entity grouping using verified join keys only."""
    tokens = _tokens(question)
    group_index = next(
        (index for index, token in enumerate(tokens[:-1]) if token in {"each", "per"}),
        None,
    )
    if group_index is None:
        return ()

    snapshot = load_schema_snapshot()
    schema_name = snapshot["schema"]
    entity_tables = {
        entity.rsplit(".", maxsplit=1)[-1]
        for entity in entities
    }

    grouped_table = None
    for table in snapshot["tables"]:
        table_name = table["table_name"]
        table_tokens = table_name.split("_")
        noun_forms = {table_tokens[-1]}
        if table_tokens[-1].endswith("s"):
            noun_forms.add(table_tokens[-1][:-1])
        if (
            table_name in entity_tables
            and tokens[group_index + 1] in noun_forms
        ):
            grouped_table = table_name
            break

    if grouped_table is None:
        return ()

    for relation in snapshot["strict_relationships"]:
        child = relation["child_table"]
        parent = relation["parent_table"]
        if child not in entity_tables or parent not in entity_tables:
            continue
        if grouped_table == child:
            columns = relation["child_columns"]
        elif grouped_table == parent:
            columns = relation["parent_columns"]
        else:
            continue

        return tuple(
            f"{schema_name}.{grouped_table}.{column}"
            for column in columns
        )

    return ()


def build_query_intent(request: QueryRequest) -> QueryIntent:
    """Create an initial intent; unresolved details remain explicit."""
    entities = _table_names_in_question(request.question)
    operation = _classify_operation(request.question)
    question_tokens = set(_tokens(request.question))
    mentioned_columns = _columns_mentioned(request.question, entities)

    measures: tuple[str, ...] = ()
    aggregation: str | None = None
    dimensions: tuple[str, ...] = ()
    filters: tuple[QueryFilter, ...] = ()
    unresolved_terms: list[str] = []

    if _is_count_request(request.question):
        measures = ("count",)
        aggregation = "count"

    requested_aggregations = set()
    if {"average", "avg"} & question_tokens:
        requested_aggregations.add("avg")
    if {"sum", "total"} & question_tokens:
        requested_aggregations.add("sum")

    numeric_aggregation = bool(requested_aggregations)
    if numeric_aggregation:
        if len(requested_aggregations) > 1:
            unresolved_terms.append("aggregation")
        elif mentioned_columns:
            measures = mentioned_columns
            aggregation = next(iter(requested_aggregations))
        else:
            unresolved_terms.append("measure")

    grouping_requested = (
        "each" in question_tokens
        or "by" in question_tokens
        or "per" in question_tokens
    )
    if grouping_requested:
        dimensions = tuple(
            target for target in mentioned_columns if target not in measures
        )
        if not dimensions:
            dimensions = _strict_join_dimension(request.question, entities)

    if "delivered" in question_tokens:
        status_target = next(
            (
                target
                for target in mentioned_columns
                if target.endswith(".order_status")
            ),
            None,
        )
        if status_target is not None:
            filters = (
                QueryFilter(field=status_target, operator="=", value="delivered"),
            )

    if operation == "aggregate" and not measures:
        unresolved_terms.append("measure")

    if not entities:
        unresolved_terms.append("entity")

    return QueryIntent(
        request_id=request.request_id,
        database_id=request.database_id,
        operation=operation,
        entities=entities,
        measures=measures,
        aggregation=aggregation,
        dimensions=dimensions,
        filters=filters,
        unresolved_terms=tuple(unresolved_terms),
    )
