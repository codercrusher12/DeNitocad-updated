"""
Structural components: beams, channels, angles.
"""
import cadquery as cq

def generate_structural_beam(params: dict) -> cq.Workplane:
    """
    Generate an I-beam or channel.
    
    Expected params:
    - beam_type: "i_beam" or "channel"
    - height_mm: Total height
    - width_mm: Flange width
    - length_mm: Beam length
    - thickness_mm: Web/flange thickness
    """
    beam_type = params.get('beam_type', 'i_beam')
    height = params.get('height_mm', 100.0)
    width = params.get('width_mm', 50.0)
    length = params.get('length_mm', 200.0)
    thickness = params.get('thickness_mm', 5.0)
    
    if beam_type == 'i_beam':
        # Previously: the flanges were built on the default XY plane as
        # rect(width, thickness).extrude(thickness) - i.e. only
        # `thickness` deep in the beam's length direction, essentially
        # thin end-caps - while the web was extruded the full `length`
        # in a different local axis and then rotated 90 deg into place.
        # Fixed by building the whole cross-section (top flange, web,
        # bottom flange) in the same YZ plane and extruding all three
        # together along X by the same `length`, the same technique
        # used for generate_angle's leg and t_bracket's cap+stem -
        # guarantees every piece spans the full beam length by
        # construction, no rotation-induced coordinate mismatch possible.
        top_flange = (
            cq.Workplane("YZ")
            .center(0, height - thickness / 2)
            .rect(width, thickness)
            .extrude(length)
        )

        # A tiny deliberate overlap (not just exact face-to-face contact)
        # between the web and each flange - coincident faces are a known
        # fragile case for OpenCASCADE boolean unions (can fuse fine or
        # fail depending on tolerance), a small real overlap avoids the
        # question entirely. thickness/2 is generous relative to a
        # cosmetic overlap and still leaves the "clear span" between
        # flanges visually/dimensionally correct.
        web_overlap = min(0.5, thickness / 4)
        web = (
            cq.Workplane("YZ")
            .center(0, height / 2)
            .rect(thickness, height - 2 * thickness + 2 * web_overlap)
            .extrude(length)
        )

        bottom_flange = (
            cq.Workplane("YZ")
            .center(0, thickness / 2)
            .rect(width, thickness)
            .extrude(length)
        )

        result = top_flange.union(web).union(bottom_flange)

    else:  # channel
        # Previously: `bottom` was built on the default XY plane and
        # rotated 90 deg about X to reorient it, which (going by the
        # standard +90 deg rotation matrix) lands its length-extent on
        # the opposite side of zero ([-length, 0]) from left_wall/
        # right_wall's un-rotated XZ-plane extrude ([0, length]) -
        # they likely wouldn't actually overlap/join. Fixed the same way
        # as the i-beam above: build `bottom` directly in the same XZ
        # plane as the walls, no rotation, so all three pieces share one
        # coordinate frame by construction.
        bottom = (
            cq.Workplane("XZ")
            .center(0, thickness / 2)
            .rect(width, thickness)
            .extrude(length)
        )

        left_wall = (
            cq.Workplane("XZ")
            .center(-width / 2 + thickness / 2, height / 2)
            .rect(thickness, height)
            .extrude(length)
        )

        right_wall = (
            cq.Workplane("XZ")
            .center(width / 2 - thickness / 2, height / 2)
            .rect(thickness, height)
            .extrude(length)
        )

        result = bottom.union(left_wall).union(right_wall)
    
    return result

def generate_angle(params: dict) -> cq.Workplane:
    """
    Generate an L-angle structural member.
    
    Expected params:
    - leg1_mm: First leg length
    - leg2_mm: Second leg length
    - thickness_mm: Material thickness
    - length_mm: Length of angle
    """
    leg1 = params.get('leg1_mm', 50.0)
    leg2 = params.get('leg2_mm', 50.0)
    thickness = params.get('thickness_mm', 5.0)
    length = params.get('length_mm', 100.0)
    
    # Create L-profile and extrude
    result = (
        cq.Workplane("XY")
        .rect(leg1, thickness)
        .extrude(length)
    )
    
    vert = (
        cq.Workplane("YZ")
        .center(0, leg2 / 2)
        .rect(thickness, leg2)
        .extrude(leg1)
    )
    
    result = result.union(vert)
    
    return result

def generate_tube(params: dict) -> cq.Workplane:
    """
    Generate a hollow tube/pipe.
    
    Expected params:
    - outer_diameter_mm: Outer diameter
    - wall_thickness_mm: Wall thickness
    - length_mm: Tube length
    - shape: "round", "square", or "rectangular"
    """
    outer = params.get('outer_diameter_mm', 20.0)
    wall = params.get('wall_thickness_mm', 2.0)
    length = params.get('length_mm', 100.0)
    shape = params.get('shape', 'round')
    
    inner = outer - 2 * wall
    
    if shape == 'round':
        result = (
            cq.Workplane("XY")
            .circle(outer / 2)
            .circle(inner / 2)
            .extrude(length)
        )
    elif shape == 'square':
        result = (
            cq.Workplane("XY")
            .rect(outer, outer)
            .rect(inner, inner)
            .extrude(length)
        )
    else:  # rectangular
        width = params.get('width_mm', outer)
        height = params.get('height_mm', outer)
        inner_width = width - 2 * wall
        inner_height = height - 2 * wall
        result = (
            cq.Workplane("XY")
            .rect(width, height)
            .rect(inner_width, inner_height)
            .extrude(length)
        )
    
    return result
