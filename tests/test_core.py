from pathlib import Path

from archival_organizer.core import (
    discover_collections,
    group_pages,
    natural_key,
    suspicious_split_reasons,
)


def test_natural_key_keeps_scan_order():
    names = [Path("scan_10.png"), Path("scan_2.png"), Path("scan_1.png")]
    assert [p.name for p in sorted(names, key=natural_key)] == ["scan_1.png", "scan_2.png", "scan_10.png"]


def test_discover_collections_finds_nested_image_directories(tmp_path: Path):
    (tmp_path / "box 2" / "folder 10").mkdir(parents=True)
    (tmp_path / "box 2" / "folder 2").mkdir()
    (tmp_path / "empty").mkdir()
    (tmp_path / "root.jpg").touch()
    (tmp_path / "box 2" / "folder 10" / "page.tif").touch()
    (tmp_path / "box 2" / "folder 2" / "page.png").touch()
    (tmp_path / "empty" / "notes.txt").touch()

    collections = discover_collections(tmp_path)

    assert [path.relative_to(tmp_path).as_posix() for path in collections] == [
        ".", "box 2/folder 2", "box 2/folder 10"
    ]


def test_group_pages_uses_right_hand_boundaries():
    pages = [{"page": n, "file": f"{n}.png"} for n in range(1, 6)]
    boundaries = {
        2: {"starts_new_document": False},
        3: {"starts_new_document": True},
        4: {"starts_new_document": False},
        5: {"starts_new_document": True},
    }
    groups = group_pages(pages, boundaries)
    assert [[p["page"] for p in group] for group in groups] == [[1, 2], [3, 4], [5]]


def test_suspicious_split_detects_sentence_seam_and_orphan_middle_page():
    pages = [
        {"page": 1, "ends_mid_sentence": True, "page_role": "first"},
        {
            "page": 2,
            "starts_mid_sentence": True,
            "ends_mid_sentence": True,
            "page_role": "middle",
            "printed_page_number": "2",
        },
    ]
    boundaries = {2: {"starts_new_document": True, "confidence": 0.99}}

    reasons = suspicious_split_reasons(pages, boundaries)

    assert len(reasons[2]) == 3
    assert "left ends mid-sentence" in reasons[2][0]


def test_suspicious_split_ignores_ordinary_new_document():
    pages = [
        {"page": 1, "ends_mid_sentence": False, "page_role": "last"},
        {
            "page": 2,
            "starts_mid_sentence": False,
            "ends_mid_sentence": False,
            "page_role": "first",
            "printed_page_number": "",
        },
    ]
    boundaries = {2: {"starts_new_document": True, "confidence": 0.99}}

    assert suspicious_split_reasons(pages, boundaries) == {}


def test_suspicious_split_reviews_sentence_seam_despite_discontinuous_pagination():
    pages = [
        {
            "page": 1,
            "ends_mid_sentence": True,
            "page_role": "first",
            "printed_page_number": "11",
        },
        {
            "page": 2,
            "starts_mid_sentence": True,
            "ends_mid_sentence": True,
            "page_role": "middle",
            "printed_page_number": "3",
        },
    ]
    boundaries = {2: {"starts_new_document": True, "confidence": 0.98}}

    reasons = suspicious_split_reasons(pages, boundaries)[2]

    assert "left ends mid-sentence" in reasons[0]
    assert "pagination conflicts" in reasons[1]


def test_suspicious_split_detects_misread_page_number_between_neighbors():
    pages = [
        {"page": 1, "printed_page_number": "16", "page_role": "middle"},
        {"page": 2, "printed_page_number": "47", "page_role": "middle"},
        {"page": 3, "printed_page_number": "18", "page_role": "middle"},
    ]
    boundaries = {
        2: {"starts_new_document": True, "confidence": 0.9},
        3: {"starts_new_document": True, "confidence": 0.9},
    }

    reasons = suspicious_split_reasons(pages, boundaries)

    assert set(reasons) == {2, 3}
    assert "may be a misread 17" in reasons[2][-1]
    assert "may be a misread 17" in reasons[3][-1]


def test_suspicious_split_detects_colon_followed_by_numbered_section():
    pages = [
        {
            "page": 1,
            "printed_page_number": "",
            "page_role": "first",
            "ending_text": "følgende kommentarer og konklusion:",
            "transcription": "Introduction ending with a colon:",
        },
        {
            "page": 2,
            "printed_page_number": "2",
            "page_role": "middle",
            "transcription": "1. Balancen mellem det lokale civilforsvar og fjernhjælp.",
        },
    ]
    boundaries = {2: {"starts_new_document": True, "confidence": 0.99}}

    reasons = suspicious_split_reasons(pages, boundaries)[2]

    assert "colon introducing the numbered section" in reasons[0]
    assert "internal page 2" in reasons[1]


def test_suspicious_split_detects_hierarchical_chapter_progression():
    pages = [
        {
            "page": 1,
            "printed_page_number": "2.3",
            "organization": "Civilforsvaret for Stor-Randers",
            "header_text": (
                "CIVILFORSVARET FOR STOR-RANDERS | FREDSMÆSSIGT KATASTROFEBEREDSKAB | "
                "OPRETTELSE AF OPLYSNINGSPOST"
            ),
        },
        {
            "page": 2,
            "printed_page_number": "3.0",
            "organization": "Civilforsvaret for Stor-Randers",
            "header_text": (
                "CIVILFORSVARET FOR STOR-RANDERS | FREDSSMÆSSIGT KATASTROFEBEREDSKAB | "
                "ORGANISATIONSPLAN"
            ),
        },
    ]
    boundaries = {2: {"starts_new_document": True, "confidence": 0.9}}

    reasons = suspicious_split_reasons(pages, boundaries)[2]

    assert reasons == [
        "hierarchical numbering progresses from 2.3 to 3.0 under the same document masthead"
    ]


def test_hierarchical_progression_requires_a_stable_masthead():
    pages = [
        {
            "page": 1,
            "printed_page_number": "2.3",
            "organization": "First organization",
            "header_text": "FIRST | MANUAL | CHAPTER",
        },
        {
            "page": 2,
            "printed_page_number": "3.0",
            "organization": "Second organization",
            "header_text": "SECOND | MANUAL | CHAPTER",
        },
    ]
    boundaries = {2: {"starts_new_document": True, "confidence": 0.9}}

    assert suspicious_split_reasons(pages, boundaries) == {}
