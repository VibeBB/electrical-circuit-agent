from scripts.verify_docs import check_adr_index, check_links


def test_documentation_links_and_adr_index() -> None:
    assert check_links() == []
    assert check_adr_index() == []
