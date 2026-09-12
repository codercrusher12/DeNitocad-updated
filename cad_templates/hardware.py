"""
Additional hardware/structural templates: hex standoff, T-bracket,
channel bracket.

Each of these is built only from CadQuery calls already exercised
elsewhere in this codebase (box/rect/circle + extrude, faces().workplane()
.hole(), pushPoints+hole, edges().fillet(), .union(), .shell()) - the one
new call is Workplane.polygon() in generate_hex_standoff, whose exact
diameter semantics (circumscribed circle, i.e. across corners, not across
flats) were verified against CadQuery's own API reference before use:
https://cadquery.readthedocs.io/en/latest/classreference.html

UNVERIFIED IN THIS ENVIRONMENT, same caveat as the rest of this project:
no CadQuery install here to actually run these. Covered by smoke_test.py -
add these three descriptions to its TEST_CASES before trusting them.
"""
import math

import cadquery as cq
from ._safe_ops import safe_fillet


def generate_hex_standoff(params: dict) -> cq.Workplane:
    """
    Hexagonal standoff, common PCB/electronics mounting hardware -
    distinct from the round spacer/washer/bearing templates by having a
    hex outer profile instead of a cylindrical one.

    Expected params:
    - across_flats_mm: hex size as normally quoted for hex stock/nut
      drivers (distance between opposite flat faces), NOT the diameter
      polygon() itself expects - converted internally.
    - inner_diameter_mm: through-bore diameter
    - length_mm: standoff length
    """
    across_flats = params.get('across_flats_mm', 6.0)
    inner_diameter = params.get('inner_diameter_mm', 3.2)
    length = params.get('length_mm', 10.0)

    # polygon(nSides, diameter) inscribes the polygon in a circle of the
    # given diameter (i.e. the diameter is measured across corners, not
    # across flats) - convert the commonly-quoted across-flats hex size.
    across_corners = across_flats / math.cos(math.radians(30))

    result = cq.Workplane("XY").polygon(6, across_corners).extrude(length)
    result = result.faces(">Z").workplane().hole(inner_diameter)
    return result


def generate_t_bracket(params: dict) -> cq.Workplane:
    """
    T-shaped bracket: a T cross-section (a cap bar sitting on top of a
    stem) extruded along its length. Both pieces are drawn in the same
    YZ plane and extruded along the same axis (X) before being unioned -
    same center()-on-a-perpendicular-plane idiom generate_angle already
    uses for its vertical leg, just with two pieces instead of one,
    avoiding the mismatched-extrusion-length issue a naive "flange + web"
    port of generate_structural_beam's i_beam construction would risk
    (there, the flanges are only extruded `thickness` deep in one local
    axis while the web is extruded the full `length` in a different local
    axis before being rotated into place - worth a second look on its
    own, separately from this).

    Expected params:
    - length_mm: extrusion length (the bracket's long axis)
    - cap_width_mm: width of the top bar
    - stem_height_mm: height of the vertical stem
    - thickness_mm: material thickness (both cap and stem)
    - hole_count / hole_diameter_mm: mounting holes through the cap
    - fillet_radius_mm: edge fillet along the length
    """
    length = params.get('length_mm', 60.0)
    cap_width = params.get('cap_width_mm', 40.0)
    stem_height = params.get('stem_height_mm', 30.0)
    thickness = params.get('thickness_mm', 4.0)
    hole_count = params.get('hole_count', 2)
    hole_diameter = params.get('hole_diameter_mm', 3.2)
    fillet_radius = params.get('fillet_radius_mm', 1.5)

    # Small deliberate overlap between cap and stem instead of exact
    # face-to-face contact - same reasoning as the i-beam fix in
    # structural.py: coincident faces are a known fragile case for
    # OpenCASCADE boolean unions.
    overlap = min(0.5, thickness / 4)

    cap = (
        cq.Workplane("YZ")
        .center(0, stem_height + thickness / 2)
        .rect(cap_width, thickness)
        .extrude(length)
    )

    stem = (
        cq.Workplane("YZ")
        .center(0, (stem_height + overlap) / 2)
        .rect(thickness, stem_height + overlap)
        .extrude(length)
    )

    result = cap.union(stem)

    if hole_count > 0 and hole_diameter > 0:
        spacing = (length - 20) / max(1, hole_count - 1) if hole_count > 1 else 0
        points = []
        for i in range(hole_count):
            x = -((hole_count - 1) * spacing) / 2 + i * spacing
            points.append((x, 0))
        result = (
            result.faces(">Z")
            .workplane()
            .pushPoints(points)
            .hole(hole_diameter)
        )

    if fillet_radius > 0 and fillet_radius < thickness / 2:
        result = safe_fillet(result, fillet_radius, "|X")

    return result


def generate_channel_bracket(params: dict) -> cq.Workplane:
    """
    Open channel bracket (U-shaped cross-section), open along one long
    side - a mounting channel, cable raceway, or cradle. Built the same
    way simple_box.py builds its open-top lid box (Workplane.shell() on
    one open face), just open on a side face instead of the top, with
    mounting holes through the opposite (closed) wall.

    Expected params:
    - length_mm: channel length
    - width_mm: channel width (the open dimension)
    - height_mm: channel height
    - wall_thickness_mm: wall thickness
    - mount_hole_count / mount_hole_diameter_mm: mounting holes through
      the back wall
    """
    length = params.get('length_mm', 60.0)
    width = params.get('width_mm', 20.0)
    height = params.get('height_mm', 15.0)
    wall_thickness = params.get('wall_thickness_mm', 2.0)
    mount_hole_count = params.get('mount_hole_count', 2)
    mount_hole_diameter = params.get('mount_hole_diameter_mm', 4.0)

    outer = cq.Workplane("XY").box(length, width, height)
    # Open along +Y (one long side) - same Workplane.shell() call
    # simple_box.py already uses for its open-top lid box, just a
    # different face selector.
    result = outer.faces(">Y").shell(-wall_thickness)

    if mount_hole_count > 0 and mount_hole_diameter > 0:
        spacing = (length - 20) / max(1, mount_hole_count - 1) if mount_hole_count > 1 else 0
        points = []
        for i in range(mount_hole_count):
            x = -((mount_hole_count - 1) * spacing) / 2 + i * spacing
            points.append((x, 0))
        result = (
            result.faces("<Y")
            .workplane()
            .pushPoints(points)
            .hole(mount_hole_diameter)
        )

    return result
