"""Fail-closed parsers for the KiCad Gerber and Excellon subset used by export checks."""

from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass
from pathlib import Path


class ExportParseError(ValueError):
    """Raised when a manufacturing export uses an unsupported encoding."""


@dataclass(frozen=True)
class GerberFeature:
    x: float
    y: float
    width: float
    height: float
    area: float
    shape: str
    polygon: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class GerberFile:
    unit: str
    features: tuple[GerberFeature, ...]


@dataclass(frozen=True)
class DrillHit:
    x: float
    y: float
    diameter: float


@dataclass(frozen=True)
class ExcellonFile:
    unit: str
    hits: tuple[DrillHit, ...]


@dataclass(frozen=True)
class _Primitive:
    code: int
    values: tuple[float, ...]
    exposure: bool


@dataclass(frozen=True)
class _Aperture:
    shape: str
    width: float
    height: float
    area: float
    polygon: tuple[tuple[float, float], ...] = ()
    primitives: tuple[_Primitive, ...] = ()


def _polygon_area(points: tuple[tuple[float, float], ...]) -> float:
    if len(points) < 3:
        return 0.0
    return abs(
        sum(
            first[0] * second[1] - second[0] * first[1]
            for first, second in zip(points, (*points[1:], points[0]), strict=True)
        )
        / 2
    )


def _rotate(point: tuple[float, float], angle_deg: float) -> tuple[float, float]:
    angle = math.radians(angle_deg)
    return (
        point[0] * math.cos(angle) - point[1] * math.sin(angle),
        point[0] * math.sin(angle) + point[1] * math.cos(angle),
    )


def _macro_expression(value: str, variables: dict[str, float]) -> float:
    normalized = re.sub(
        r"\$(\d+)",
        lambda match: f"var_{match.group(1)}",
        value.strip(),
    )
    try:
        node = ast.parse(normalized, mode="eval").body
    except SyntaxError as error:
        raise ExportParseError(f"invalid aperture macro expression: {value!r}") from error

    def evaluate(item: ast.expr) -> float:
        if isinstance(item, ast.Constant) and isinstance(item.value, int | float):
            return float(item.value)
        if isinstance(item, ast.Name) and item.id in variables:
            return variables[item.id]
        if isinstance(item, ast.UnaryOp) and isinstance(item.op, (ast.UAdd, ast.USub)):
            operand = evaluate(item.operand)
            return operand if isinstance(item.op, ast.UAdd) else -operand
        if isinstance(item, ast.BinOp) and isinstance(
            item.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
        ):
            left, right = evaluate(item.left), evaluate(item.right)
            if isinstance(item.op, ast.Add):
                return left + right
            if isinstance(item.op, ast.Sub):
                return left - right
            if isinstance(item.op, ast.Mult):
                return left * right
            if right == 0:
                raise ExportParseError("division by zero in aperture macro")
            return left / right
        raise ExportParseError(f"unsupported aperture macro expression: {value!r}")

    result = evaluate(node)
    if not math.isfinite(result):
        raise ExportParseError("non-finite aperture macro value")
    return result


def _primitive_polygon(primitive: _Primitive) -> tuple[tuple[float, float], ...]:
    values = primitive.values
    if primitive.code == 1:
        return ()
    if primitive.code == 21:
        width, height, center_x, center_y, rotation = values
        points = (
            (-width / 2, -height / 2),
            (width / 2, -height / 2),
            (width / 2, height / 2),
            (-width / 2, height / 2),
        )
        return tuple(
            (
                center_x + _rotate(point, rotation)[0],
                center_y + _rotate(point, rotation)[1],
            )
            for point in points
        )
    if primitive.code == 22:
        width, height, lower_x, lower_y, rotation = values
        points = (
            (0.0, 0.0),
            (width, 0.0),
            (width, height),
            (0.0, height),
        )
        return tuple(
            (
                lower_x + _rotate(point, rotation)[0],
                lower_y + _rotate(point, rotation)[1],
            )
            for point in points
        )
    if primitive.code == 20:
        width, x0, y0, x1, y1, rotation = values
        half = width / 2
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length == 0:
            return ()
        nx, ny = -dy * half / length, dx * half / length
        points = ((x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny))
        return tuple(_rotate(point, rotation) for point in points)
    if primitive.code == 4:
        count = int(values[0])
        points = tuple((values[1 + index * 2], values[2 + index * 2]) for index in range(count))
        rotation = values[1 + count * 2]
        return tuple(_rotate(point, rotation) for point in points)
    if primitive.code == 5:
        count, center_x, center_y, diameter, rotation = values
        radius = diameter / 2
        return tuple(
            (
                center_x + math.cos(math.radians(rotation + 360 * index / int(count))) * radius,
                center_y + math.sin(math.radians(rotation + 360 * index / int(count))) * radius,
            )
            for index in range(int(count))
        )
    raise ExportParseError(f"unsupported aperture macro primitive {primitive.code}")


def _primitive_contains(primitive: _Primitive, point: tuple[float, float]) -> bool:
    values = primitive.values
    if primitive.code == 1:
        diameter, center_x, center_y = values[:3]
        return math.dist(point, (center_x, center_y)) <= diameter / 2
    if primitive.code == 20:
        width, x0, y0, x1, y1, rotation = values
        px, py = _rotate(point, -rotation)
        dx, dy = x1 - x0, y1 - y0
        length_sq = dx * dx + dy * dy
        t = (
            0.0
            if length_sq == 0
            else min(1.0, max(0.0, ((px - x0) * dx + (py - y0) * dy) / length_sq))
        )
        return math.dist((px, py), (x0 + t * dx, y0 + t * dy)) <= width / 2
    polygon = _primitive_polygon(primitive)
    if not polygon:
        return False
    x, y = point
    inside = False
    previous = polygon[-1]
    for current in polygon:
        if (current[1] > y) != (previous[1] > y) and x < (
            (previous[0] - current[0]) * (y - current[1]) / (previous[1] - current[1]) + current[0]
        ):
            inside = not inside
        previous = current
    return inside


def _macro_aperture(primitives: tuple[_Primitive, ...]) -> _Aperture:
    polygons = [_primitive_polygon(item) for item in primitives if item.exposure]
    extents = [point for polygon in polygons for point in polygon]
    for primitive in primitives:
        if primitive.code == 1 and primitive.exposure:
            diameter, center_x, center_y = primitive.values[:3]
            extents.extend(
                (
                    (center_x - diameter / 2, center_y - diameter / 2),
                    (center_x + diameter / 2, center_y + diameter / 2),
                )
            )
    if not extents:
        raise ExportParseError("aperture macro has no exposed geometry")
    x0 = min(point[0] for point in extents)
    y0 = min(point[1] for point in extents)
    x1 = max(point[0] for point in extents)
    y1 = max(point[1] for point in extents)
    width, height = x1 - x0, y1 - y0
    if width <= 0 or height <= 0:
        raise ExportParseError("aperture macro has degenerate extents")
    resolution = 320
    dx, dy = width / resolution, height / resolution
    filled = 0
    for row in range(resolution):
        y = y0 + (row + 0.5) * dy
        for column in range(resolution):
            point = (x0 + (column + 0.5) * dx, y)
            if any(
                _primitive_contains(item, point) for item in primitives if item.exposure
            ) and not any(
                _primitive_contains(item, point) for item in primitives if not item.exposure
            ):
                filled += 1
    return _Aperture("macro", width, height, filled * dx * dy, primitives=primitives)


def _parse_macro(
    body: str,
    parameters: list[float],
    unit_scale: float,
) -> tuple[_Primitive, ...]:
    variables = {f"var_{index}": value for index, value in enumerate(parameters, start=1)}
    primitives: list[_Primitive] = []
    for raw in body.split("*"):
        line = raw.strip()
        if not line or line.startswith("0"):
            continue
        fields = [field.strip() for field in line.split(",")]
        try:
            code = int(fields[0])
            values = [_macro_expression(field, variables) for field in fields[1:]]
        except (ValueError, IndexError) as error:
            raise ExportParseError("invalid aperture macro primitive") from error
        if code == 1:
            if len(values) not in (4, 5):
                raise ExportParseError("unsupported circle aperture macro primitive")
            exposure = values[0] != 0
            diameter, center_x, center_y = values[1:4]
            rotation = values[4] if len(values) == 5 else 0.0
            center_x, center_y = _rotate((center_x, center_y), rotation)
            primitive_values = (
                diameter * unit_scale,
                center_x * unit_scale,
                center_y * unit_scale,
            )
        elif code == 20:
            if len(values) != 7:
                raise ExportParseError("unsupported vector-line macro primitive")
            exposure = values[0] != 0
            primitive_values = (*(value * unit_scale for value in values[1:6]), values[6])
        elif code == 21:
            if len(values) != 6:
                raise ExportParseError("unsupported rectangle macro primitive")
            exposure = values[0] != 0
            primitive_values = (*(value * unit_scale for value in values[1:5]), values[5])
        elif code == 22:
            if len(values) != 6:
                raise ExportParseError("unsupported lower-left rectangle macro primitive")
            exposure = values[0] != 0
            primitive_values = (*(value * unit_scale for value in values[1:5]), values[5])
        elif code == 4:
            if len(values) < 8:
                raise ExportParseError("unsupported outline macro primitive")
            exposure = values[0] != 0
            count = int(values[1])
            if len(values) != 3 + count * 2:
                raise ExportParseError("outline macro vertex count is inconsistent")
            primitive_values = (
                float(count),
                *(value * unit_scale for value in values[2 : 2 + count * 2]),
                values[-1],
            )
        elif code == 5:
            if len(values) != 6:
                raise ExportParseError("unsupported polygon macro primitive")
            exposure = values[0] != 0
            primitive_values = (
                values[1],
                values[2] * unit_scale,
                values[3] * unit_scale,
                values[4] * unit_scale,
                values[5],
            )
        else:
            raise ExportParseError(f"unsupported aperture macro primitive {code}")
        primitive = _Primitive(code, primitive_values, exposure)
        if code == 20 and not exposure:
            raise ExportParseError("clear vector-line macro primitives are unsupported")
        primitives.append(primitive)
    return tuple(primitives)


def _aperture(
    code: int,
    shape: str,
    raw_parameters: str,
    macros: dict[str, str],
    unit_scale: float,
) -> _Aperture:
    raw_values = [float(value) for value in re.split(r"[Xx]", raw_parameters) if value]
    values = [value * unit_scale for value in raw_values]
    if shape == "C" and len(values) in (1, 2, 3):
        diameter = values[0]
        hole_width = values[1] if len(values) > 1 else 0.0
        hole_height = values[2] if len(values) > 2 else hole_width
        area = math.pi * (diameter**2 - hole_width * hole_height) / 4
        if diameter <= 0 or hole_width > diameter or hole_height > diameter or area <= 0:
            raise ExportParseError("invalid circular aperture dimensions")
        return _Aperture(shape, diameter, diameter, area)
    if shape == "R" and len(values) in (2, 3, 4):
        width, height = values[:2]
        hole_width = values[2] if len(values) > 2 else 0.0
        hole_height = values[3] if len(values) > 3 else hole_width
        area = width * height - math.pi * hole_width * hole_height / 4
        if width <= 0 or height <= 0 or hole_width > width or hole_height > height or area <= 0:
            raise ExportParseError("invalid rectangular aperture dimensions")
        return _Aperture(shape, width, height, area)
    if shape == "O" and len(values) in (2, 3, 4):
        width, height = values
        short, long = min(width, height), max(width, height)
        area = short * (long - short) + math.pi * short * short / 4
        hole_width = values[2] if len(values) > 2 else 0.0
        hole_height = values[3] if len(values) > 3 else hole_width
        area -= math.pi * hole_width * hole_height / 4
        if width <= 0 or height <= 0 or area <= 0:
            raise ExportParseError("invalid obround aperture dimensions")
        return _Aperture(shape, width, height, area)
    if shape == "P" and len(raw_values) in (2, 3, 4, 5):
        diameter = raw_values[0] * unit_scale
        count = int(raw_values[1])
        rotation = raw_values[2] if len(raw_values) == 3 else 0.0
        if len(raw_values) > 3:
            rotation = raw_values[2]
        points = tuple(
            (
                math.cos(math.radians(rotation + 360 * index / count)) * diameter / 2,
                math.sin(math.radians(rotation + 360 * index / count)) * diameter / 2,
            )
            for index in range(count)
        )
        width = max(point[0] for point in points) - min(point[0] for point in points)
        height = max(point[1] for point in points) - min(point[1] for point in points)
        area = _polygon_area(points)
        if len(values) > 3:
            hole_width = values[3]
            hole_height = values[4] if len(values) > 4 else hole_width
            area -= math.pi * hole_width * hole_height / 4
        if count < 3 or area <= 0:
            raise ExportParseError("invalid polygon aperture dimensions")
        return _Aperture(shape, width, height, area, polygon=points)
    if shape in macros:
        parameters = [float(value) for value in re.split(r"[Xx]", raw_parameters) if value]
        primitives = _parse_macro(macros[shape], parameters, unit_scale)
        result = _macro_aperture(primitives)
        return _Aperture(
            result.shape,
            result.width,
            result.height,
            result.area,
            result.polygon,
            result.primitives,
        )
    raise ExportParseError(f"unsupported aperture D{code} shape {shape!r}")


def _coordinate(
    value: str,
    integer_digits: int,
    decimal_digits: int,
    scale: float,
    zero_suppression: str,
) -> float:
    if "." in value:
        result = float(value) * scale
        integer_length = len(value.lstrip("+-").split(".", maxsplit=1)[0])
        if integer_length > integer_digits:
            raise ExportParseError("Gerber coordinate exceeds declared format")
    else:
        sign = -1 if value.startswith("-") else 1
        digits = value.lstrip("+-")
        maximum_digits = integer_digits + decimal_digits
        if len(digits) > maximum_digits:
            raise ExportParseError("Gerber coordinate exceeds declared format")
        if zero_suppression == "T":
            digits = digits.ljust(maximum_digits, "0")
        result = sign * int(digits or "0") / 10**decimal_digits * scale
    if not math.isfinite(result):
        raise ExportParseError("non-finite Gerber coordinate")
    return result


def parse_gerber(path: Path) -> GerberFile:
    text = path.read_text(encoding="ascii")
    macros: dict[str, str] = {}
    for match in re.finditer(r"%(AM[^%]+)%", text, re.DOTALL):
        macro = match.group(1)
        name, separator, body = macro[2:].partition("*")
        if not separator or not name:
            raise ExportParseError("malformed Gerber aperture macro")
        macros[name] = body
    text = re.sub(r"%(AM[^%]+)%", "", text, flags=re.DOTALL)
    apertures: dict[int, _Aperture] = {}
    unit = ""
    unit_scale = 1.0
    x_integer = y_integer = 2
    x_decimal = y_decimal = 4
    zero_suppression = "L"
    features: list[GerberFeature] = []
    current_aperture: _Aperture | None = None
    current_code: int | None = None
    modal_operation = 2
    position = (0.0, 0.0)
    region: list[tuple[float, float]] | None = None

    for block in re.findall(r"%([^%]+)%", text, flags=re.DOTALL):
        for command in block.split("*"):
            command = command.strip()
            if not command:
                continue
            if command.startswith("FS"):
                match = re.fullmatch(r"FS[LT]A?X(\d)(\d)Y(\d)(\d)", command)
                if match is None:
                    raise ExportParseError("unsupported Gerber coordinate format")
                zero_suppression = command[2]
                x_integer, x_decimal, y_integer, y_decimal = map(int, match.groups())
            elif command == "MOMM":
                unit, unit_scale = "mm", 1.0
            elif command == "MOIN":
                unit, unit_scale = "in", 25.4
            elif match := re.fullmatch(r"ADD(\d+)([A-Za-z0-9_]+)(?:,(.*))?", command):
                aperture_code = int(match.group(1))
                apertures[aperture_code] = _aperture(
                    aperture_code,
                    match.group(2),
                    match.group(3) or "",
                    macros,
                    unit_scale,
                )
            elif command == "LPD" or command.startswith(
                ("TF.", "TA.", "TO.", "TD", "IN", "IP", "AS", "MI", "OF", "SF", "IR", "LN")
            ):
                continue
            elif command == "LPC":
                raise ExportParseError("Gerber clear-polarity geometry is unsupported")
            else:
                raise ExportParseError(f"unsupported Gerber extended command {command!r}")

    if not unit:
        raise ExportParseError("Gerber file has no supported MO unit")
    body = re.sub(r"%[^%]*%", "", text, flags=re.DOTALL)
    body = re.sub(r"G04[^\*]*\*", "", body)
    for raw in body.split("*"):
        command = raw.strip()
        if not command:
            continue
        if command in {"M02", "M00", "G01", "G70", "G71", "G90"}:
            continue
        if command == "G36":
            if region is not None:
                raise ExportParseError("nested Gerber regions are unsupported")
            region = []
            continue
        if command == "G37":
            if region is None or len(region) < 3:
                raise ExportParseError("malformed Gerber region")
            polygon = tuple(region)
            xs, ys = [point[0] for point in polygon], [point[1] for point in polygon]
            features.append(
                GerberFeature(
                    x=(min(xs) + max(xs)) / 2,
                    y=(min(ys) + max(ys)) / 2,
                    width=max(xs) - min(xs),
                    height=max(ys) - min(ys),
                    area=_polygon_area(polygon),
                    shape="polygon",
                    polygon=polygon,
                )
            )
            region = None
            continue
        command = command.removeprefix("G01")
        if command.startswith("G"):
            raise ExportParseError(f"unsupported Gerber interpolation command {command!r}")
        if match := re.fullmatch(r"D(\d+)", command):
            value = int(match.group(1))
            if value >= 10:
                current_code = value
                current_aperture = apertures.get(value)
                if current_aperture is None:
                    raise ExportParseError(f"Gerber selects undefined aperture D{value}")
            else:
                raise ExportParseError(f"unsupported standalone Gerber operation D{value}")
            continue
        match = re.fullmatch(
            r"(?:X([+-]?\d+(?:\.\d+)?))?(?:Y([+-]?\d+(?:\.\d+)?))?(?:D0?([123])|D(\d+))?",
            command,
        )
        if match is None or not (
            match.group(1) or match.group(2) or match.group(3) or match.group(4)
        ):
            raise ExportParseError(f"unsupported Gerber command {command!r}")
        x = (
            _coordinate(match.group(1), x_integer, x_decimal, unit_scale, zero_suppression)
            if match.group(1) is not None
            else position[0]
        )
        y = (
            _coordinate(match.group(2), y_integer, y_decimal, unit_scale, zero_suppression)
            if match.group(2) is not None
            else position[1]
        )
        operation = (
            (int(match.group(3)) if match.group(3) is not None else modal_operation)
            if match.group(4) is None
            else None
        )
        if match.group(4) is not None:
            aperture_code = int(match.group(4))
            if aperture_code < 10:
                raise ExportParseError(f"unsupported Gerber operation D{aperture_code}")
            current_code = aperture_code
            current_aperture = apertures.get(aperture_code)
            if current_aperture is None:
                raise ExportParseError(f"Gerber selects undefined aperture D{aperture_code}")
            operation = None
        elif match.group(3) is not None:
            modal_operation = int(match.group(3))
        if operation == 2:
            position = (x, y)
            if region is not None:
                if region and region[-1] != position:
                    raise ExportParseError("Gerber region has multiple contours")
                region.append(position)
        elif operation == 1:
            if region is None and position != (x, y):
                if current_aperture is None:
                    raise ExportParseError("Gerber draw has no selected aperture")
                length = math.dist(position, (x, y))
                feature_width = abs(x - position[0]) + current_aperture.width
                feature_height = abs(y - position[1]) + current_aperture.height
                features.append(
                    GerberFeature(
                        x=(position[0] + x) / 2,
                        y=(position[1] + y) / 2,
                        width=feature_width,
                        height=feature_height,
                        area=current_aperture.area
                        + length * max(current_aperture.width, current_aperture.height),
                        shape="stroke",
                    )
                )
            elif region is not None:
                region.append((x, y))
            position = (x, y)
        elif operation == 3:
            if region is not None or current_aperture is None or current_code is None:
                raise ExportParseError("Gerber flash has no selected aperture")
            features.append(
                GerberFeature(
                    x=x,
                    y=y,
                    width=current_aperture.width,
                    height=current_aperture.height,
                    area=current_aperture.area,
                    shape=current_aperture.shape,
                    polygon=current_aperture.polygon,
                )
            )
            position = (x, y)
        else:
            if operation is not None:
                raise ExportParseError("Gerber command has no supported operation")
    if region is not None:
        raise ExportParseError("unterminated Gerber region")
    return GerberFile(unit=unit, features=tuple(features))


def _excellon_number(
    value: str,
    integer_digits: int,
    decimal_digits: int,
    scale: float,
    zero_suppression: str,
) -> float:
    if "." in value:
        result = float(value)
    else:
        sign = -1 if value.startswith("-") else 1
        digits = value.lstrip("+-")
        if zero_suppression == "TZ":
            digits = digits.ljust(integer_digits + decimal_digits, "0")
        result = sign * int(digits or "0") / 10**decimal_digits
    result *= scale
    if not math.isfinite(result):
        raise ExportParseError("non-finite Excellon coordinate")
    return result


def parse_excellon(path: Path) -> ExcellonFile:
    text = path.read_text(encoding="ascii")
    unit = ""
    unit_scale = 1.0
    integer_digits = 2
    decimal_digits = 4
    zero_suppression = "L"
    tools: dict[str, float] = {}
    selected_tool: str | None = None
    hits: list[DrillHit] = []
    for raw_line in text.splitlines():
        line = raw_line.strip().upper()
        if not line:
            continue
        if line.startswith(";FILE_FORMAT="):
            match = re.fullmatch(r";FILE_FORMAT=(\d):(\d)", line)
            if match is None:
                raise ExportParseError("unsupported Excellon file format")
            integer_digits = int(match.group(1))
            decimal_digits = int(match.group(2))
            continue
        if line.startswith(";") or line in {
            "M48",
            "%",
            "M95",
            "M30",
            "M00",
            "G05",
            "G90",
            "FMAT,2",
            "VER,1",
        }:
            continue
        if line == "M71":
            unit, unit_scale = "mm", 1.0
            continue
        if line == "M72":
            unit, unit_scale = "in", 25.4
            continue
        if match := re.fullmatch(r"(METRIC|INCH)(?:,(LZ|TZ)(?:,.*)?)?", line):
            unit = "mm" if match.group(1) == "METRIC" else "in"
            unit_scale = 1.0 if unit == "mm" else 25.4
            zero_suppression = match.group(2) or zero_suppression
            continue
        if match := re.fullmatch(
            r"T(\d+)C(\d+(?:\.\d+)?)(?:F\d+)?(?:S\d+)?",
            line,
        ):
            if not unit:
                raise ExportParseError("Excellon tool table appears before a unit declaration")
            diameter = float(match.group(2)) * unit_scale
            if not math.isfinite(diameter) or diameter <= 0:
                raise ExportParseError("Excellon tool diameter is invalid")
            tools[match.group(1)] = diameter
            continue
        if match := re.fullmatch(r"T(\d+)", line):
            selected_tool = match.group(1)
            if selected_tool not in tools:
                raise ExportParseError(f"Excellon selects undefined tool T{selected_tool}")
            continue
        if line.startswith(("G00", "G01", "G02", "G03", "M15", "M16")):
            raise ExportParseError(f"unsupported Excellon routing command {line!r}")
        inline_hit = re.fullmatch(
            r"T(\d+)X([+-]?\d+(?:\.\d+)?)Y([+-]?\d+(?:\.\d+)?)(?:M00)?",
            line,
        )
        if inline_hit is not None:
            selected_tool = inline_hit.group(1)
            if selected_tool not in tools:
                raise ExportParseError(f"Excellon selects undefined tool T{selected_tool}")
            coordinates = inline_hit
            coordinate_offset = 1
        else:
            coordinates = re.fullmatch(
                r"(?:G05)?X([+-]?\d+(?:\.\d+)?)Y([+-]?\d+(?:\.\d+)?)(?:M00)?",
                line,
            )
            coordinate_offset = 0
        if coordinates is None:
            raise ExportParseError(f"unsupported Excellon command {line!r}")
        if selected_tool is None or not unit:
            raise ExportParseError("Excellon hit appears before a unit or tool selection")
        hits.append(
            DrillHit(
                x=_excellon_number(
                    coordinates.group(1 + coordinate_offset),
                    integer_digits,
                    decimal_digits,
                    unit_scale,
                    zero_suppression,
                ),
                y=_excellon_number(
                    coordinates.group(2 + coordinate_offset),
                    integer_digits,
                    decimal_digits,
                    unit_scale,
                    zero_suppression,
                ),
                diameter=tools[selected_tool],
            )
        )
    if not unit:
        raise ExportParseError("Excellon file has no supported unit")
    return ExcellonFile(unit=unit, hits=tuple(hits))
