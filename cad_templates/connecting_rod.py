"""
Connecting rod (conrod) - the link between a crank pin (big end) and a
piston pin (small end), as seen on any piston engine crankshaft assembly.

Genuinely more complex than this project's other templates: two
different-diameter bores at each end, a shank connecting them (with an
optional raised center rib - the visual signature of a forged I-section
rod), and representative big-end cap bolt holes. Still deliberately
simplified in the same documented spirit as generate_gear()'s
trapezoidal tooth profile: this is NOT a true tapered/I-beam shank
cross-section (that needs a lofted or swept profile between two
different end shapes, which is a further iteration worth doing if the
rib/rectangular shank approximation here isn't enough), and there's no
actual two-piece rod+cap split (a single STEP solid can't represent a
bolted joint between two separate bodies) - the cap bolt holes are a
visual convention, not a mechanism.

Implementation note: every hole cut below uses an explicit cutter solid
+ `.cut()` at a known global (x, y) rather than `.faces(">Z").workplane()
.hole(...)`. That face-workplane pattern (used elsewhere in this
project, e.g. flat_plate.py) centers its local coordinate system on the
selected face's center of mass - harmless for a part that's symmetric
about the global origin (a plate centered at (0,0)), but silently wrong
here: this rod's two bosses sit at different, deliberately off-origin
coordinates, so a face-centered workplane would place every hole offset
from where the caller actually asked for it.
"""

from __future__ import annotations

import cadquery as cq

from ._safe_ops import safe_fillet


def _cut_hole(solid: cq.Workplane, center_xy: tuple, diameter: float, thickness: float) -> cq.Workplane:
    """Cuts a through-hole of `diameter` at global (x, y) = center_xy,
    spanning the full part thickness. See module docstring for why this
    explicit-cutter approach is used instead of faces(">Z").hole()."""
    cutter = cq.Workplane("XY").moveTo(*center_xy).circle(diameter / 2).extrude(thickness)
    return solid.cut(cutter)


def generate_connecting_rod(params: dict) -> cq.Workplane:
    """
    Expected params (all in mm unless noted):
    - big_end_diameter_mm: crank-pin bore diameter (default 24.0)
    - big_end_boss_diameter_mm: outer diameter of material around the
      big-end bore (default 40.0)
    - small_end_diameter_mm: piston-pin (gudgeon pin) bore diameter
      (default 12.0)
    - small_end_boss_diameter_mm: outer diameter around the small-end
      bore (default 22.0)
    - center_distance_mm: distance between the two bore centers - this
      IS the rod's effective length (default 120.0)
    - shank_width_mm: width of the connecting shank, in-plane,
      perpendicular to the rod's long axis (default 14.0)
    - thickness_mm: out-of-plane depth of the whole part (default 12.0)
    - rib_height_mm: height of the raised center rib above the shank
      face, 0 to disable (default 2.5)
    - rib_width_mm: width of the rib, must be less than shank_width_mm
      (default: 45% of shank_width_mm)
    - cap_bolt_diameter_mm: representative big-end cap bolt hole
      diameter, 0 to disable (default 5.0)
    - fillet_mm: fillet radius at shank-to-boss transitions (default 3.0)
    """
    big_bore = params.get("big_end_diameter_mm", 24.0)
    big_boss = params.get("big_end_boss_diameter_mm", 40.0)
    small_bore = params.get("small_end_diameter_mm", 12.0)
    small_boss = params.get("small_end_boss_diameter_mm", 22.0)
    center_distance = params.get("center_distance_mm", 120.0)
    shank_width = params.get("shank_width_mm", 14.0)
    thickness = params.get("thickness_mm", 12.0)
    rib_height = params.get("rib_height_mm", 2.5)
    rib_width = params.get("rib_width_mm", shank_width * 0.45)
    cap_bolt_dia = params.get("cap_bolt_diameter_mm", 5.0)
    fillet_radius = params.get("fillet_mm", 3.0)

    small_center = (0.0, 0.0)
    big_center = (center_distance, 0.0)

    # --- Bosses (the circular pads of material around each bore) ------
    small_boss_solid = (
        cq.Workplane("XY").moveTo(*small_center).circle(small_boss / 2).extrude(thickness)
    )
    big_boss_solid = (
        cq.Workplane("XY").moveTo(*big_center).circle(big_boss / 2).extrude(thickness)
    )

    # --- Shank: a straight-sided link between the two bosses -----------
    # Not a true tapered I-beam profile (see module docstring) - a
    # constant-width rectangle from boss to boss, which the fillet pass
    # below softens at the boss transitions so it doesn't read as a
    # crude rectangular slab butted against two circles.
    half_w = shank_width / 2
    shank_solid = (
        cq.Workplane("XY")
        .polyline(
            [
                (0, -half_w),
                (center_distance, -half_w),
                (center_distance, half_w),
                (0, half_w),
            ]
        )
        .close()
        .extrude(thickness)
    )

    rod_body = small_boss_solid.union(big_boss_solid).union(shank_solid)

    # --- Fillet the shank/boss transitions ------------------------------
    # Degrades gracefully (see _safe_ops.py's module docstring on why
    # that matters) rather than failing the whole part if OCCT rejects
    # this radius against the actual resulting edge topology.
    if fillet_radius > 0:
        rod_body = safe_fillet(rod_body, fillet_radius, "|Z", label="shank fillet")

    # --- Bores -------------------------------------------------------------
    if small_bore > 0:
        rod_body = _cut_hole(rod_body, small_center, small_bore, thickness)
    if big_bore > 0:
        rod_body = _cut_hole(rod_body, big_center, big_bore, thickness)

    # --- Raised center rib (the forged-I-section visual signature) -----
    if rib_height > 0 and rib_width > 0:
        # Kept clear of both bosses so it reads as a rib along the
        # shank, not a slab overlapping the bore pads.
        rib_start = small_boss / 2 * 0.8
        rib_end = center_distance - big_boss / 2 * 0.8
        if rib_end > rib_start:
            rib_half_w = min(rib_width, shank_width * 0.9) / 2
            rib = (
                cq.Workplane("XY")
                .workplane(offset=thickness)
                .polyline(
                    [
                        (rib_start, -rib_half_w),
                        (rib_end, -rib_half_w),
                        (rib_end, rib_half_w),
                        (rib_start, rib_half_w),
                    ]
                )
                .close()
                .extrude(rib_height)
            )
            rod_body = rod_body.union(rib)

    # --- Representative big-end cap bolt holes --------------------------
    # See module docstring - this is a visual convention (two holes
    # flanking the big-end bore, matching the split-cap look in real
    # conrod photos/renders) rather than a modeled bolted joint.
    #
    # Positioned at the radial midpoint between the bore's edge and the
    # boss's outer edge, not a fixed fraction of the boss radius - a
    # fixed fraction can walk the hole into the bore (if the boss is
    # only slightly larger than the bore) or off the edge of the boss
    # entirely (if it's much larger). If there isn't at least
    # `cap_bolt_dia` worth of clear radial material either side of the
    # midpoint, the holes are skipped rather than punched through a
    # bore wall or off the boss edge.
    if cap_bolt_dia > 0:
        bore_r = big_bore / 2
        boss_r = big_boss / 2
        radial_space = boss_r - bore_r
        if radial_space > cap_bolt_dia * 1.5:
            bolt_offset = bore_r + radial_space / 2
            for sign in (-1, 1):
                bolt_center = (big_center[0], big_center[1] + sign * bolt_offset)
                rod_body = _cut_hole(rod_body, bolt_center, cap_bolt_dia, thickness)

    return rod_body
