"""
Multi-throw crankshaft - the part in the screenshots this template was
written in response to (a 4-throw automotive-style crank with
connecting rods on it).

Construction strategy: each main journal is built as its own segment
(not one continuous full-length cylinder) - a crankshaft's main-journal
diameter is only present where the main bearings actually are; through
each throw, the shaft pinches down and offsets sideways to the rod
journal via the webs, it doesn't stay at main-journal diameter the
whole way through. (An earlier draft of this template built one
continuous backbone cylinder for simplicity and unioned the throws onto
it - that's wrong for exactly this reason: it leaves a full-diameter
main journal running straight through every throw region as well,
producing a fat cylinder with bumps instead of an actual crank profile.
Fixed here by building N+1 discrete main-journal segments instead.)

Deliberately simplified, in the same spirit as generate_gear()'s
non-involute teeth and connecting_rod.py's non-tapered shank:
- The counterweight is a circular lobe, not a true machined arc/sector
  profile - visually reads as a counterweight opposite the rod journal,
  but isn't the crescent shape a real counterweight is machined to.
- Throws are evenly spaced around 360°/num_throws by default. Real
  engines use firing-order-specific phase angles (e.g. a production
  inline-4 is usually 180°/180°/0° pairs, not 90° apart) - pass your
  own `phase_angles_deg` list to match a specific engine instead of the
  generic even spacing.
- No fillets between journals and webs by default (`fillet_mm` is
  accepted but only applied if `apply_fillets=True`) - this is a large
  multi-body boolean union with a lot of edges, and a global fillet pass
  across all of it is a real risk of a very slow or failing OCCT
  operation. Left opt-in rather than defaulted on so the common case
  stays fast and robust; turn it on and see whether safe_fillet's
  degrade-gracefully behavior handles it acceptably on your actual
  geometry.
"""

from __future__ import annotations

import math

import cadquery as cq

from ._safe_ops import safe_fillet


def _rect_link_solid(p1: tuple, p2: tuple, width: float, thickness: float, z0: float) -> cq.Workplane:
    """A straight prismatic link between two points, `width` wide,
    extruded by `thickness`, starting at global Z=z0. Built from
    explicit perpendicular-offset corner points (not workplane.rotate())
    so the result sits at exact global coordinates regardless of the
    link's angle - see connecting_rod.py's module docstring for why
    face/workplane-centered positioning is the wrong tool for
    off-origin, non-symmetric geometry like this."""
    x1, y1 = p1
    x2, y2 = p2
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-6:
        # Degenerate (coincident points) - nothing to connect.
        return cq.Workplane("XY")
    ux, uy = dx / length, dy / length
    nx, ny = -uy * (width / 2), ux * (width / 2)
    corners = [
        (x1 + nx, y1 + ny),
        (x2 + nx, y2 + ny),
        (x2 - nx, y2 - ny),
        (x1 - nx, y1 - ny),
    ]
    return (
        cq.Workplane("XY")
        .workplane(offset=z0)
        .polyline(corners)
        .close()
        .extrude(thickness)
    )


def _web_solid(
    main_center: tuple,
    rod_center: tuple,
    cw_center: tuple,
    main_r: float,
    rod_r: float,
    cw_radius: float,
    link_width: float,
    thickness: float,
    z0: float,
) -> cq.Workplane:
    """One crank web (cheek): a disc around the main journal, a disc
    around the rod journal, a straight link between them, and a
    counterweight lobe on the opposite side of the main journal from
    the rod journal. All positioned at explicit global coordinates."""
    main_disc = cq.Workplane("XY").workplane(offset=z0).moveTo(*main_center).circle(main_r).extrude(thickness)
    rod_disc = cq.Workplane("XY").workplane(offset=z0).moveTo(*rod_center).circle(rod_r).extrude(thickness)
    link = _rect_link_solid(main_center, rod_center, link_width, thickness, z0)
    cw_disc = cq.Workplane("XY").workplane(offset=z0).moveTo(*cw_center).circle(cw_radius).extrude(thickness)
    return main_disc.union(rod_disc).union(link).union(cw_disc)


def generate_crankshaft(params: dict) -> cq.Workplane:
    """
    Expected params (all in mm/degrees unless noted):
    - num_throws: number of crank throws / rod journals (default 4)
    - stroke_mm: piston stroke = 2x crank radius (default 80.0)
    - main_journal_diameter_mm (default 50.0)
    - main_journal_length_mm: length of each main bearing surface,
      i.e. the backbone segments between/around throws (default 25.0)
    - rod_journal_diameter_mm (default 45.0)
    - rod_journal_length_mm (default 22.0)
    - web_thickness_mm: axial thickness of each crank cheek (default 12.0)
    - web_link_width_mm: width of the disc-to-disc link in each web
      (default: 60% of main_journal_diameter_mm)
    - counterweight_radius_mm (default: main_journal_diameter_mm/2 + 22)
    - counterweight_offset_mm: how far the counterweight's center sits
      from the main axis, opposite the rod journal (default: stroke_mm/2)
    - phase_angles_deg: optional list of length num_throws overriding
      the default even 360/num_throws spacing
    - nose_diameter_mm / nose_length_mm: front snout for a pulley/damper,
      0 length to disable (defaults: 0.7x main journal dia, 15.0)
    - flange_diameter_mm / flange_length_mm: rear flywheel flange,
      0 length to disable (defaults: 1.3x main journal dia, 10.0)
    - fillet_mm: fillet radius, only applied if apply_fillets is True
      (default 2.0)
    - apply_fillets: bool, see module docstring (default False)
    """
    num_throws = max(1, int(params.get("num_throws", 4)))
    stroke = params.get("stroke_mm", 80.0)
    crank_radius = stroke / 2

    main_dia = params.get("main_journal_diameter_mm", 50.0)
    main_len = params.get("main_journal_length_mm", 25.0)
    rod_dia = params.get("rod_journal_diameter_mm", 45.0)
    rod_len = params.get("rod_journal_length_mm", 22.0)
    web_thickness = params.get("web_thickness_mm", 12.0)
    web_link_width = params.get("web_link_width_mm", main_dia * 0.6)
    cw_radius = params.get("counterweight_radius_mm", main_dia / 2 + 22.0)
    cw_offset = params.get("counterweight_offset_mm", crank_radius)
    apply_fillets = bool(params.get("apply_fillets", False))
    fillet_radius = params.get("fillet_mm", 2.0)

    phase_angles = params.get("phase_angles_deg")
    if not phase_angles or len(phase_angles) != num_throws:
        phase_angles = [360.0 * i / num_throws for i in range(num_throws)]

    nose_dia = params.get("nose_diameter_mm", main_dia * 0.7)
    nose_len = params.get("nose_length_mm", 15.0)
    flange_dia = params.get("flange_diameter_mm", main_dia * 1.3)
    flange_len = params.get("flange_length_mm", 10.0)

    # --- Build main journals + throws as discrete segments -------------
    # See module docstring for why this is segments, not one continuous
    # backbone cylinder: main-journal diameter material should only
    # exist at the main bearing surfaces, not run straight through every
    # throw as well.
    main_r = main_dia / 2
    rod_r = rod_dia / 2
    z_cursor = 0.0

    result = cq.Workplane("XY").workplane(offset=z_cursor).circle(main_r).extrude(main_len)
    z_cursor += main_len

    for i in range(num_throws):
        phase_rad = math.radians(phase_angles[i])
        rod_center = (crank_radius * math.cos(phase_rad), crank_radius * math.sin(phase_rad))
        cw_center = (-cw_offset * math.cos(phase_rad), -cw_offset * math.sin(phase_rad))

        web1 = _web_solid(
            (0.0, 0.0), rod_center, cw_center, main_r, rod_r, cw_radius,
            web_link_width, web_thickness, z_cursor,
        )
        result = result.union(web1)
        z_cursor += web_thickness

        rod_journal = (
            cq.Workplane("XY")
            .workplane(offset=z_cursor)
            .moveTo(*rod_center)
            .circle(rod_r)
            .extrude(rod_len)
        )
        result = result.union(rod_journal)
        z_cursor += rod_len

        web2 = _web_solid(
            (0.0, 0.0), rod_center, cw_center, main_r, rod_r, cw_radius,
            web_link_width, web_thickness, z_cursor,
        )
        result = result.union(web2)
        z_cursor += web_thickness

        # Next main-journal segment (one more than there are throws -
        # every throw sits between two main journals).
        next_main = cq.Workplane("XY").workplane(offset=z_cursor).circle(main_r).extrude(main_len)
        result = result.union(next_main)
        z_cursor += main_len

    total_length = z_cursor

    # --- Optional front nose (pulley/damper mount) ----------------------
    if nose_len > 0 and nose_dia > 0:
        nose = cq.Workplane("XY").circle(nose_dia / 2).extrude(nose_len).translate((0, 0, -nose_len))
        result = result.union(nose)

    # --- Optional rear flange (flywheel mount) --------------------------
    if flange_len > 0 and flange_dia > 0:
        flange = (
            cq.Workplane("XY")
            .workplane(offset=total_length)
            .circle(flange_dia / 2)
            .extrude(flange_len)
        )
        result = result.union(flange)

    # --- Optional fillets (see module docstring - off by default) ------
    if apply_fillets and fillet_radius > 0:
        result = safe_fillet(result, fillet_radius, None, label="crank web fillet")

    return result
