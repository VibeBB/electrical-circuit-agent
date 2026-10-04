from typing import Literal

from circuit.partspec import (
    CellRef,
    ConnectorBoardEdge,
    ConnectorContactRow,
    ConnectorKeepout,
    ConnectorMatingEnvelope,
    ConnectorMechanicalFeature,
    ConnectorNumbering,
    ConnectorSpec,
    DatasheetRef,
    Dimension,
    OrderableVariant,
    PackageSpec,
    PartSpec,
    PinSpec,
    PinTable,
    Reading,
)


def reading(label: str) -> Reading:
    return Reading(
        page=1,
        bbox=(0.0, 0.0, 1.0, 1.0),
        vision=label,
        vision_record="synthetic-connector-vision.json",
    )


def dimension(value: float) -> Dimension:
    return Dimension(nom=value, reading=reading(str(value)))


def _row(
    numbers: list[str],
    *,
    x0: float,
    y: float,
    pitch: float,
    width: float,
    height: float,
    drill: float | None = None,
    stagger: float | None = None,
) -> ConnectorContactRow:
    return ConnectorContactRow(
        numbers=numbers,
        x0=dimension(x0),
        y=dimension(y),
        pitch=dimension(pitch),
        stagger=dimension(stagger) if stagger is not None else None,
        drill=dimension(drill) if drill is not None else None,
        pad_width=dimension(width),
        pad_height=dimension(height),
    )


def connector_spec(kind: str) -> PartSpec:
    contacts: list[ConnectorContactRow]
    mechanical: list[ConnectorMechanicalFeature] = []
    numbering_view: Literal["mating_face", "top", "pcb_side"] = "top"
    mating_mirror = False
    keepout: ConnectorKeepout | None = None
    mount: Literal["smd", "tht", "mixed"] = "smd"
    orientation: Literal["vertical", "right_angle", "edge_mount"] = "vertical"
    mating_axis: Literal["+x", "-x", "+y", "-y", "+z"] = "+z"
    edge: ConnectorBoardEdge | None = None

    if kind == "header_2x5":
        mount = "tht"
        contacts = [
            _row(
                [str(number) for number in range(1, 6)],
                x0=-2.54,
                y=-0.635,
                pitch=1.27,
                width=1.6,
                height=1.6,
                drill=0.8,
            ),
            _row(
                [str(number) for number in range(6, 11)],
                x0=-2.54,
                y=0.635,
                pitch=1.27,
                width=1.6,
                height=1.6,
                drill=0.8,
            ),
        ]
    elif kind == "jst_ph_tht":
        mount = "tht"
        contacts = [
            _row(
                ["1", "2"],
                x0=-1.0,
                y=0.0,
                pitch=2.0,
                width=1.6,
                height=1.6,
                drill=0.8,
            )
        ]
        mechanical.append(
            ConnectorMechanicalFeature(
                kind="locating",
                x=dimension(-3.0),
                y=dimension(0.0),
                drill=dimension(1.0),
                pad_width=dimension(2.0),
                pad_height=dimension(2.0),
                plated=False,
                plating_reading=reading("Unplated locating hole"),
            )
        )
    elif kind in {"jst_ph_right_angle", "datamate"}:
        mount = "smd"
        orientation = "right_angle"
        mating_axis = "+x"
        edge = ConnectorBoardEdge(
            side="+x",
            offset=dimension(0.0),
            source_note="Synthetic connector board-edge datum",
        )
        contacts = [
            _row(
                ["1", "2", "3", "4"],
                x0=-1.5,
                y=-1.0,
                pitch=1.0,
                width=0.7,
                height=1.4,
            )
        ]
        if kind == "jst_ph_right_angle":
            mechanical.extend(
                [
                    ConnectorMechanicalFeature(
                        kind="mounting",
                        x=dimension(-3.0),
                        y=dimension(-1.0),
                        pad_width=dimension(1.4),
                        pad_height=dimension(1.8),
                    ),
                    ConnectorMechanicalFeature(
                        kind="mounting",
                        x=dimension(3.0),
                        y=dimension(-1.0),
                        pad_width=dimension(1.4),
                        pad_height=dimension(1.8),
                    ),
                ]
            )
    elif kind == "usb_c":
        mount = "mixed"
        contacts = [
            _row(
                [str(number) for number in range(1, 9)],
                x0=-1.75,
                y=-1.0,
                pitch=0.5,
                width=0.25,
                height=1.2,
                stagger=0.25,
            ),
            _row(
                [str(number) for number in range(9, 17)],
                x0=-1.75,
                y=1.0,
                pitch=0.5,
                width=0.25,
                height=1.2,
                stagger=0.25,
            ),
        ]
        mechanical.extend(
            [
                ConnectorMechanicalFeature(
                    kind="shield",
                    number="SH1",
                    x=dimension(-3.0),
                    y=dimension(-1.5),
                    drill=dimension(0.8),
                    pad_width=dimension(1.6),
                    pad_height=dimension(1.6),
                    plated=True,
                    plating_reading=reading("Plated shell tab"),
                ),
                ConnectorMechanicalFeature(
                    kind="shield",
                    number="SH2",
                    x=dimension(3.0),
                    y=dimension(1.5),
                    drill=dimension(0.8),
                    pad_width=dimension(1.6),
                    pad_height=dimension(1.6),
                    plated=True,
                    plating_reading=reading("Plated shell tab"),
                ),
                ConnectorMechanicalFeature(
                    kind="locating",
                    x=dimension(-2.5),
                    y=dimension(0.0),
                    drill=dimension(0.8),
                    pad_width=dimension(1.6),
                    pad_height=dimension(1.6),
                    plated=False,
                    plating_reading=reading("Unplated locating hole"),
                ),
            ]
        )
    elif kind == "fpc_10":
        contacts = [
            _row(
                [str(number) for number in range(1, 11)],
                x0=-2.25,
                y=0.0,
                pitch=0.5,
                width=0.3,
                height=1.2,
            )
        ]
        mechanical.extend(
            [
                ConnectorMechanicalFeature(
                    kind="retention",
                    x=dimension(-3.0),
                    y=dimension(0.0),
                    pad_width=dimension(1.0),
                    pad_height=dimension(1.2),
                ),
                ConnectorMechanicalFeature(
                    kind="retention",
                    x=dimension(3.0),
                    y=dimension(0.0),
                    pad_width=dimension(1.0),
                    pad_height=dimension(1.2),
                ),
            ]
        )
    elif kind == "sma_edge":
        orientation = "edge_mount"
        mating_axis = "+x"
        edge = ConnectorBoardEdge(
            side="+x",
            offset=dimension(0.0),
            source_note="Synthetic coax board-edge datum",
        )
        contacts = [
            _row(
                ["1"],
                x0=-0.5,
                y=0.0,
                pitch=1.0,
                width=1.2,
                height=1.2,
            )
        ]
        mechanical = [
            ConnectorMechanicalFeature(
                kind="shield",
                x=dimension(x),
                y=dimension(y),
                pad_width=dimension(0.8),
                pad_height=dimension(0.8),
            )
            for x, y in ((-2.0, -1.5), (-2.0, 1.5), (2.0, -1.5), (2.0, 1.5))
        ]
        keepout = ConnectorKeepout(
            x0=dimension(-3.0),
            y0=dimension(-2.5),
            x1=dimension(3.0),
            y1=dimension(2.5),
            reading=reading("Coax shield copper keepout"),
        )
    elif kind == "gecko":
        mount = "tht"
        contacts = [
            _row(
                ["1", "2", "3", "4"],
                x0=-1.5,
                y=0.0,
                pitch=1.0,
                width=1.0,
                height=1.0,
                drill=0.5,
            )
        ]
        mechanical = [
            ConnectorMechanicalFeature(
                kind="mounting",
                x=dimension(x),
                y=dimension(y),
                drill=dimension(2.2),
                pad_width=dimension(3.0),
                pad_height=dimension(3.0),
            )
            for x, y in ((-3.0, -2.0), (3.0, 2.0))
        ]
    elif kind == "kona_mirrored":
        mount = "tht"
        numbering_view = "mating_face"
        mating_mirror = True
        contacts = [
            _row(
                ["1", "2", "3", "4"],
                x0=-1.5,
                y=0.0,
                pitch=1.0,
                width=1.2,
                height=1.2,
                drill=0.6,
            )
        ]
    else:
        raise ValueError(f"unknown connector fixture: {kind}")

    numbers = [number for row in contacts for number in row.numbers]
    if kind in {"header_2x5", "jst_ph_tht", "gecko", "kona_mirrored"}:
        mount = "tht"
    numbering = ConnectorNumbering(
        manufacturer_to_kicad={number: number for number in numbers},
        view=numbering_view,
        reading=reading(f"{kind} numbering"),
        mating_mirror=mating_mirror,
    )
    mating_face = dimension(2.0)
    envelope = ConnectorMatingEnvelope(
        mating_mpn=f"{kind}-mate",
        reading=reading(f"{kind} mating interface"),
        box=(1.5, -1.0, 2.5, 1.0),
        z_min=0.0,
        z_max=5.0,
        travel=dimension(3.0),
        access_margin_mm=0.5,
    )
    connector = ConnectorSpec(
        mount=mount,
        orientation=orientation,
        gender="female",
        mating_axis=mating_axis,
        mating_face=mating_face,
        board_edge=edge,
        mating_envelope=envelope,
        contacts=contacts,
        mechanical=mechanical,
        numbering=numbering,
        copper_keepout=keepout,
    )
    pin_numbers = [connector.numbering.manufacturer_to_kicad[number] for number in numbers]
    return PartSpec(
        artifact_kind="circuit_part_spec",
        mpn=f"SYNTH-{kind.upper()}",
        manufacturer="Synthetic",
        datasheet=DatasheetRef(
            path="synthetic-connectors.pdf",
            sha256="a" * 64,
            revision="A",
            extraction_path="synthetic-connectors.extraction.json",
        ),
        package=PackageSpec(
            family="connector",
            drawing_id=kind,
            pin_count=len(pin_numbers),
            pitch=dimension(1.0),
            body_length=dimension(8.0),
            body_width=dimension(4.0),
            height=dimension(5.0),
            drawing_view="top",
            pin1_corner="top_left",
            pin1_reading=reading(f"{kind} pin 1"),
        ),
        connector=connector,
        pins=[
            PinSpec(
                number=number,
                name=f"PIN{number}",
                electrical_type="passive",
                reading=reading(f"{kind} pin {number}"),
            )
            for number in pin_numbers
        ],
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
        orderable=[
            OrderableVariant(
                mpn=f"SYNTH-{kind.upper()}",
                package_designator=kind,
                pin_count=len(pin_numbers),
                row=CellRef(table=0, row=1, col=0),
                reading=reading(f"{kind} orderable"),
            )
        ],
    )
