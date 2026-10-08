from archival_organizer.review import effective_sections


def test_merged_section_keeps_first_title_and_is_marked_adjusted():
    pages = {
        1: {"page_id": 10, "file": "page-1.png"},
        2: {"page_id": 20, "file": "page-2.png"},
    }
    original_sections = [
        {
            "section": 1, "start_page": 1, "end_page": 1, "label": "First title",
            "document_type": "letter", "summary": "First summary", "date": "",
            "places": [], "subjects": [], "label_confidence": 0.9,
            "lowest_boundary_confidence": 1.0,
        },
        {
            "section": 2, "start_page": 2, "end_page": 2, "label": "Second title",
            "document_type": "letter", "summary": "Second summary", "date": "",
            "places": [], "subjects": [], "label_confidence": 0.8,
            "lowest_boundary_confidence": 1.0,
        },
    ]
    boundaries = {
        2: {"starts_new_document": False, "confidence": 1.0, "review_source": "human"}
    }

    sections = effective_sections(original_sections, pages, boundaries)

    assert len(sections) == 1
    assert sections[0]["label"] == "First title"
    assert "Second title" not in sections[0]["label"]
    assert sections[0]["review_adjusted"] is True
    assert sections[0]["label_needs_review"] is True
