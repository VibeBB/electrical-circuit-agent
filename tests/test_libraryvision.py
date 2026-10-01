from pathlib import Path
from types import SimpleNamespace
from typing import cast

from PIL import Image, ImageDraw

from circuit.libraryvision import (
    _comparison_region,  # pyright: ignore[reportPrivateUsage]
    _scale_model_render,  # pyright: ignore[reportPrivateUsage]
)
from circuit.partspec import PartSpec


def test_model_render_is_rescaled_to_physical_board_extent(tmp_path: Path) -> None:
    source = tmp_path / "render.png"
    destination = tmp_path / "scaled.png"
    image = Image.new("RGB", (240, 180), "white")
    ImageDraw.Draw(image).rectangle((40, 30, 199, 149), fill=(40, 90, 70))
    image.save(source, format="PNG")

    _scale_model_render(
        source,
        destination,
        board_width_mm=16.0,
        board_height_mm=12.0,
        target_pixels_per_mm=20.0,
    )

    with Image.open(destination) as rendered:
        assert rendered.size == (320, 240)


def test_model_comparison_region_uses_the_package_pin1_drawing() -> None:
    pin1_reading = SimpleNamespace(page=4, bbox=(200.0, 150.0, 220.0, 170.0))
    spec = cast(
        PartSpec,
        SimpleNamespace(
            package=SimpleNamespace(
                pin1_reading=pin1_reading,
                body_length=SimpleNamespace(
                    reading=SimpleNamespace(page=4, bbox=(10.0, 10.0, 30.0, 20.0))
                ),
                body_width=SimpleNamespace(
                    reading=SimpleNamespace(page=4, bbox=(40.0, 10.0, 60.0, 20.0))
                ),
            )
        ),
    )

    page, bbox = _comparison_region(spec, "compare_model")

    assert page == 4
    assert bbox == (164.0, 114.0, 256.0, 206.0)
