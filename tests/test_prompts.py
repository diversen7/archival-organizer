import json

from archival_organizer.prompts import (
    PAGE_ANALYSIS_PROMPT,
    boundary_prompt,
    boundary_review_prompt,
    label_prompt,
)


def test_dynamic_prompts_include_unicode_evidence_as_json():
    evidence = [{"page": 2, "title": "Aarhus havn"}]

    assert json.dumps(evidence, ensure_ascii=False) in boundary_prompt(evidence)
    assert json.dumps(evidence, ensure_ascii=False) in boundary_review_prompt(evidence)
    assert json.dumps(evidence, ensure_ascii=False) in label_prompt(evidence)


def test_each_prompt_defines_the_output_language_policy():
    prompts = [
        PAGE_ANALYSIS_PROMPT,
        boundary_prompt([]),
        boundary_review_prompt([]),
        label_prompt([]),
    ]

    for prompt in prompts:
        assert "in Danish" in prompt
        assert "original language" in prompt
