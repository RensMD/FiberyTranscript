from integrations.context_builder import build_keyterms_prompt
from integrations.fibery_client import EntityContext


def test_build_keyterms_prompt_curates_full_names_and_dedupes_case_insensitively():
    context = EntityContext(
        entity_name="Quarterly Review with Acme",
        assignee_names=["  Alice   Johnson  "],
        people_names=["alice johnson", "Bob Stone"],
        operator_names=["Carol Smith"],
        organization_names=["Acme Holdings"],
    )

    result = build_keyterms_prompt(context)

    assert result.terms == [
        "Alice Johnson",
        "Bob Stone",
        "Carol Smith",
        "Acme Holdings",
    ]
    assert result.total_words == 8
    assert result.skipped_reasons == {"duplicate": 1}
    assert "Alice" not in result.terms
    assert "Quarterly" not in result.terms


def test_build_keyterms_prompt_filters_invalid_candidates():
    context = EntityContext(
        assignee_names=["Amy", "   "],
        people_names=["One Two Three Four Five Six Seven"],
        organization_names=["X" * 51],
    )

    result = build_keyterms_prompt(context)

    assert result.terms == []
    assert result.total_words == 0
    assert result.skipped_reasons == {
        "unsupported_length": 2,
        "empty": 1,
        "too_many_words": 1,
    }


def test_build_keyterms_prompt_enforces_word_budget_with_stable_priority():
    context = EntityContext(
        assignee_names=[
            f"Atlas{i} Baker{i} Carter{i} Dune{i}"
            for i in range(12)
        ],
        operator_names=["Vector Forge"],
        organization_names=["Nimbus Labs"],
    )

    result = build_keyterms_prompt(context)

    assert result.terms == [
        *[f"Atlas{i} Baker{i} Carter{i} Dune{i}" for i in range(12)],
        "Vector Forge",
    ]
    assert result.total_words == 50
    assert "Nimbus Labs" not in result.terms
    assert result.skipped_reasons == {"word_budget": 1}
