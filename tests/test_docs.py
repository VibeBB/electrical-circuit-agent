import re
from pathlib import Path


def test_adr_number_prefixes_are_unique() -> None:
    adr_dir = Path(__file__).parents[1] / "docs" / "adr"
    paths = sorted(adr_dir.glob("ADR-*.md"))
    matches = [re.match(r"ADR-(\d{4})-", path.name) for path in paths]

    assert all(match is not None for match in matches)
    numbers = [match.group(1) for match in matches if match is not None]
    assert len(numbers) == len(set(numbers))
