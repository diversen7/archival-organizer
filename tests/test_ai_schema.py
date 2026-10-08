from archival_organizer.ai import PAGE_SCHEMA, _review_page_evidence
from archival_organizer.cli import DEFAULT_MODEL


def test_page_schema_requires_every_declared_field():
    schema = PAGE_SCHEMA["schema"]
    assert set(schema["required"]) == set(schema["properties"])


def test_page_schema_contains_boundary_evidence_and_transcription():
    fields = PAGE_SCHEMA["schema"]["properties"]
    expected = {
        "transcription",
        "page_role",
        "header_text",
        "footer_text",
        "starts_mid_sentence",
        "ends_mid_sentence",
        "layout_signature",
        "reference_number",
    }
    assert expected <= fields.keys()


def test_default_model_is_luna():
    assert DEFAULT_MODEL == "gpt-6-luna"


def test_boundary_review_uses_the_text_nearest_the_seam():
    page = {
        "page": 4,
        "transcription": "start " + "x" * 1300 + " sentence ending",
        "visual_description": "Faint and smudged page number.",
    }

    evidence = _review_page_evidence(page, trailing=True)
    trailing = evidence["transcription_excerpt"]
    leading = _review_page_evidence(page, trailing=False)["transcription_excerpt"]

    assert evidence["visual_description"] == "Faint and smudged page number."
    assert trailing.endswith("sentence ending")
    assert not trailing.startswith("start")
    assert leading.startswith("start")
    assert not leading.endswith("sentence ending")
