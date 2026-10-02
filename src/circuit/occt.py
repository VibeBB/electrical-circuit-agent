# pyright: basic, reportAttributeAccessIssue=false
"""Narrow Open CASCADE boundary for STEP model operations."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Sphere
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.Interface import Interface_Static
from OCP.Quantity import Quantity_Color
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Reader, STEPControl_Writer
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID, TopAbs_VERTEX
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Shape
from OCP.XCAFDoc import (
    XCAFDoc_ColorSurf,
    XCAFDoc_DocumentTool,
)


class OcctError(ValueError):
    """Raised when Open CASCADE cannot safely process a model."""


@dataclass(frozen=True)
class Shape:
    native: TopoDS_Shape
    units: str = "mm"


@dataclass(frozen=True)
class Bounds:
    x_min: float
    y_min: float
    z_min: float
    x_max: float
    y_max: float
    z_max: float

    @property
    def xyz(self) -> tuple[float, float, float, float, float, float]:
        return (self.x_min, self.y_min, self.z_min, self.x_max, self.y_max, self.z_max)


@dataclass(frozen=True)
class SolidFacts:
    volume: float
    bbox: Bounds
    closed_shell: bool


@dataclass(frozen=True)
class ShapeFacts:
    solid_count: int
    solids: tuple[SolidFacts, ...]
    valid: bool
    units: str


@dataclass(frozen=True)
class SlabRegion:
    source_solid: int
    bbox_xy: tuple[float, float, float, float]
    area: float


@dataclass(frozen=True)
class MarkerEvidence:
    quadrant: str
    face_kind: str
    centroid: tuple[float, float, float]


def _explore(shape: TopoDS_Shape, kind: object) -> list[TopoDS_Shape]:
    result: list[TopoDS_Shape] = []
    explorer = TopExp_Explorer(shape, kind)
    while explorer.More():
        result.append(explorer.Current())
        explorer.Next()
    return result


def _bounds(shape: TopoDS_Shape) -> Bounds:
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        raise OcctError("shape has empty bounds")
    minimum = box.CornerMin()
    maximum = box.CornerMax()
    return Bounds(minimum.X(), minimum.Y(), minimum.Z(), maximum.X(), maximum.Y(), maximum.Z())


def _step_units(path: Path) -> str:
    text = path.read_text(encoding="ascii", errors="replace").upper()
    compact = re.sub(r"\s+", "", text)
    if "SI_UNIT(.MILLI.,.METRE.)" in compact or "MILLIMETRE" in compact:
        return "mm"
    if "SI_UNIT($,.METRE.)" in compact or "SI_UNIT(.METRE.)" in compact:
        return "m"
    if "INCH" in compact:
        return "in"
    return "unknown"


def read_step(path: Path) -> Shape:
    reader = STEPControl_Reader()
    try:
        status = reader.ReadFile(str(path))
        if status != IFSelect_RetDone:
            raise OcctError(f"STEP read status is not RetDone: {status}")
        if reader.NbRootsForTransfer() < 1:
            raise OcctError("STEP file has no transfer roots")
        reader.SetSystemLengthUnit(1.0)
        if reader.TransferRoots() < 1:
            raise OcctError("STEP transfer produced no roots")
        result = reader.OneShape()
        if result.IsNull() or not any(
            _explore(result, kind)
            for kind in (TopAbs_SOLID, TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX)
        ):
            raise OcctError("STEP file contains an empty shape")
        return Shape(result, _step_units(path))
    except OcctError:
        raise
    except Exception as exc:
        raise OcctError(f"STEP read failed: {exc}") from exc


def write_step(shape: Shape, path: Path, *, product_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for key, value in (
        ("write.step.product.name", product_name),
        ("write.step.product", product_name),
        ("write.step.assembly", "OFF"),
        ("write.step.schema", "AP214"),
        ("write.step.header.author", "circuit-agent"),
        ("write.step.header.company", "circuit-agent"),
        ("write.step.header.preprocessor_version", "Open CASCADE"),
        ("write.step.header.originating_system", "circuit-agent"),
    ):
        Interface_Static.SetCVal_s(key, value)
    try:
        writer = STEPControl_Writer()
        if writer.Transfer(shape.native, STEPControl_AsIs) != IFSelect_RetDone:
            raise OcctError("STEP transfer failed")
        if writer.Write(str(path)) != IFSelect_RetDone:
            raise OcctError("STEP write failed")
        content = path.read_text(encoding="ascii", errors="replace")
        content = re.sub(
            r"(FILE_NAME\s*\(\s*'[^']*'\s*,\s*)'[^']*'",
            r"\1'1970-01-01T00:00:00'",
            content,
            count=1,
        )
        escaped_name = product_name.replace("'", "''")
        product = re.compile(
            r"PRODUCT\(\s*'(?:''|[^'])*'\s*,\s*'(?:''|[^'])*'\s*,\s*"
            r"'(?P<identifier>(?:''|[^'])*)'\s*,\s*\((?P<refs>[^()]*)\)\s*\)",
            re.DOTALL,
        )
        product_index = 0

        def normalize_product(match: re.Match[str]) -> str:
            nonlocal product_index
            product_index += 1
            name = (
                escaped_name if product_index == 1 else f"{escaped_name} solid {product_index - 1}"
            )
            refs = ",".join(item.strip() for item in match.group("refs").split(",") if item.strip())
            identifier = match.group("identifier").strip()
            return f"PRODUCT('{name}','{name}','{identifier}',({refs}))"

        content = product.sub(normalize_product, content)
        occurrence = re.compile(r"(NEXT_ASSEMBLY_USAGE_OCCURRENCE\()'[^']*'")
        occurrence_index = 0

        def normalize_occurrence(match: re.Match[str]) -> str:
            nonlocal occurrence_index
            occurrence_index += 1
            return f"{match.group(1)}'{occurrence_index}'"

        content = occurrence.sub(normalize_occurrence, content)
        path.write_text(content, encoding="ascii")
    except OcctError:
        raise
    except Exception as exc:
        raise OcctError(f"STEP write failed: {exc}") from exc


def inspect(shape: Shape) -> ShapeFacts:
    solids = _explore(shape.native, TopAbs_SOLID)
    details: list[SolidFacts] = []
    for solid in solids:
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(solid, props)
        shells = _explore(solid, TopAbs_SHELL)
        details.append(
            SolidFacts(
                volume=props.Mass(),
                bbox=_bounds(solid),
                closed_shell=bool(shells) and all(BRep_Tool.IsClosed_s(shell) for shell in shells),
            )
        )
    return ShapeFacts(
        solid_count=len(solids),
        solids=tuple(details),
        valid=BRepCheck_Analyzer(shape.native).IsValid(),
        units=shape.units,
    )


def slab_regions(shape: Shape, z_lo: float, z_hi: float) -> list[SlabRegion]:
    if not z_hi > z_lo:
        raise OcctError("slab thickness must be positive")
    source_solids = _explore(shape.native, TopAbs_SOLID)
    regions: list[SlabRegion] = []
    for source_index, solid in enumerate(source_solids):
        bounds = _bounds(solid)
        width = bounds.x_max - bounds.x_min
        height = bounds.y_max - bounds.y_min
        slab = BRepPrimAPI_MakeBox(
            gp_Pnt(bounds.x_min - 1.0, bounds.y_min - 1.0, z_lo),
            width + 2.0,
            height + 2.0,
            z_hi - z_lo,
        ).Shape()
        common = BRepAlgoAPI_Common(solid, slab)
        common.Build()
        if not common.IsDone():
            raise OcctError("solid/slab intersection failed")
        pieces = _explore(common.Shape(), TopAbs_SOLID)
        for piece in pieces:
            piece_bounds = _bounds(piece)
            props = GProp_GProps()
            BRepGProp.VolumeProperties_s(piece, props)
            regions.append(
                SlabRegion(
                    source_solid=source_index,
                    bbox_xy=(
                        piece_bounds.x_min,
                        piece_bounds.y_min,
                        piece_bounds.x_max,
                        piece_bounds.y_max,
                    ),
                    area=props.Mass() / (z_hi - z_lo),
                )
            )
    return regions


def pin1_marker(shape: Shape, body_bbox: Bounds) -> MarkerEvidence | None:
    top = body_bbox.z_max
    middle_x = (body_bbox.x_min + body_bbox.x_max) / 2
    middle_y = (body_bbox.y_min + body_bbox.y_max) / 2
    candidates: list[MarkerEvidence] = []
    for face_shape in _explore(shape.native, TopAbs_FACE):
        face = TopoDS.Face(face_shape)
        adaptor = BRepAdaptor_Surface(face, True)
        surface_kind = adaptor.GetType()
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        center = props.CentreOfMass()
        x, y, z = center.X(), center.Y(), center.Z()
        if abs(z - top) > 0.15:
            continue
        if surface_kind == GeomAbs_Plane and z >= top - 1e-5:
            continue
        if surface_kind not in (GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Sphere, GeomAbs_Plane):
            continue
        quadrant = (
            ("top" if y < middle_y else "bottom") + "_" + ("left" if x < middle_x else "right")
        )
        candidates.append(
            MarkerEvidence(
                quadrant=quadrant,
                face_kind=str(surface_kind),
                centroid=(x, y, z),
            )
        )
    return (
        sorted(candidates, key=lambda item: (item.centroid[2], item.quadrant))[0]
        if candidates
        else None
    )


def face_color_marker(path: Path, body_bbox: Bounds) -> str | None:
    try:
        reader = STEPCAFControl_Reader()
        reader.SetColorMode(True)
        if reader.ReadFile(str(path)) != IFSelect_RetDone:
            return None
        document = TDocStd_Document(TCollection_ExtendedString("circuit-model"))
        if not reader.Transfer(document):
            return None
        shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
        color_tool = XCAFDoc_DocumentTool.ColorTool_s(document.Main())
        shape = shape_tool.GetOneShape()
        if shape.IsNull():
            return None
        middle_x = (body_bbox.x_min + body_bbox.x_max) / 2
        middle_y = (body_bbox.y_min + body_bbox.y_max) / 2
        top_colors: list[tuple[tuple[float, float, float], float]] = []
        for face in _explore(shape, TopAbs_FACE):
            adaptor = BRepAdaptor_Surface(face, True)
            if adaptor.GetType() != GeomAbs_Plane:
                continue
            props = GProp_GProps()
            BRepGProp.SurfaceProperties_s(face, props)
            center = props.CentreOfMass()
            if abs(center.Z() - body_bbox.z_max) > 0.01:
                continue
            color = Quantity_Color()
            if color_tool.GetColor(face, XCAFDoc_ColorSurf, color):
                rgb = (color.Red(), color.Green(), color.Blue())
                top_colors.append((rgb, props.Mass()))
        if len(top_colors) < 2:
            return None
        counts: dict[tuple[float, float, float], float] = {}
        for color, area in top_colors:
            counts[color] = counts.get(color, 0.0) + area
        baseline = max(counts, key=counts.__getitem__)
        for face in _explore(shape, TopAbs_FACE):
            if BRepAdaptor_Surface(face, True).GetType() != GeomAbs_Plane:
                continue
            props = GProp_GProps()
            BRepGProp.SurfaceProperties_s(face, props)
            center = props.CentreOfMass()
            if abs(center.Z() - body_bbox.z_max) > 0.01:
                continue
            color = Quantity_Color()
            if not color_tool.GetColor(face, XCAFDoc_ColorSurf, color):
                continue
            rgb = (color.Red(), color.Green(), color.Blue())
            if sum((left - right) ** 2 for left, right in zip(rgb, baseline, strict=True)) < 0.01:
                continue
            return (
                ("top" if center.Y() < middle_y else "bottom")
                + "_"
                + ("left" if center.X() < middle_x else "right")
            )
        return None
    except Exception:
        return None


def transform(
    shape: Shape,
    *,
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0),
    rotation_z_deg: float = 0.0,
    scale: float = 1.0,
    mirror_x: bool = False,
) -> Shape:
    if not math.isfinite(scale) or scale <= 0:
        raise OcctError("scale must be finite and positive")
    result = shape.native
    operations: list[gp_Trsf] = []
    if mirror_x:
        mirror = gp_Trsf()
        mirror.SetMirror(gp_Ax2(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(1.0, 0.0, 0.0)))
        operations.append(mirror)
    if rotation_z_deg:
        rotation = gp_Trsf()
        rotation.SetRotation(
            gp_Ax1(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0.0, 0.0, 1.0)),
            math.radians(rotation_z_deg),
        )
        operations.append(rotation)
    if scale != 1.0:
        scaling = gp_Trsf()
        scaling.SetScale(gp_Pnt(0.0, 0.0, 0.0), scale)
        operations.append(scaling)
    if translation != (0.0, 0.0, 0.0):
        offset = gp_Trsf()
        offset.SetTranslation(gp_Vec(*translation))
        operations.append(offset)
    for operation in operations:
        result = BRepBuilderAPI_Transform(result, operation, True).Shape()
    return Shape(result, shape.units)


def solids(shape: Shape) -> tuple[Shape, ...]:
    return tuple(Shape(solid, shape.units) for solid in _explore(shape.native, TopAbs_SOLID))


def fuse(shapes: Sequence[Shape]) -> Shape:
    if not shapes:
        raise OcctError("cannot fuse an empty shape list")
    value = shapes[0].native
    for shape in shapes[1:]:
        operation = BRepAlgoAPI_Fuse(value, shape.native)
        operation.Build()
        if not operation.IsDone():
            raise OcctError("shape fuse failed")
        value = operation.Shape()
    return Shape(value, shapes[0].units)


def box(
    x: float,
    y: float,
    z: float,
    length: float,
    width: float,
    height: float,
) -> Shape:
    return Shape(BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), length, width, height).Shape())


def cylinder_cut(
    shape: Shape,
    *,
    x: float,
    y: float,
    top_z: float,
    radius: float,
    depth: float,
) -> Shape:
    cutter = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(x, y, top_z - depth), gp_Dir(0.0, 0.0, 1.0)),
        radius,
        depth,
    ).Shape()
    operation = BRepAlgoAPI_Cut(shape.native, cutter)
    operation.Build()
    if not operation.IsDone():
        raise OcctError("cylinder cut failed")
    return Shape(operation.Shape(), shape.units)


def compound(shapes: Sequence[Shape]) -> Shape:
    builder = BRep_Builder()
    result = TopoDS_Compound()
    builder.MakeCompound(result)
    units = shapes[0].units if shapes else "mm"
    for shape in shapes:
        builder.Add(result, shape.native)
    return Shape(result, units)
