"""
L-bracket template with mounting holes.
"""
import cadquery as cq
from ._safe_ops import safe_fillet

def generate_l_bracket(params: dict) -> cq.Workplane:
    """
    Generate an L-bracket.
    
    Expected params:
    - width_mm: Bracket width
    - height_mm: Vertical leg height
    - depth_mm: Horizontal leg depth
    - thickness_mm: Material thickness
    - hole_count: Number of holes per leg (default 2)
    - hole_diameter_mm: Hole diameter
    - fillet_radius_mm: Edge fillet radius
    """
    width = params.get("width_mm", 50.0)
    height = params.get("height_mm", 60.0)
    depth = params.get("depth_mm", 40.0)
    thickness = params.get("thickness_mm", 3.0)
    hole_count = params.get("hole_count", 2)
    hole_diameter = params.get("hole_diameter_mm", 3.2)
    fillet_radius = params.get("fillet_radius_mm", 2.0)
    
    # Create L-shape profile
    result = (
        cq.Workplane("XY")
        .rect(width, thickness)
        .extrude(height)
        .faces(">Z")
        .workplane()
        .rect(width, depth)
        .extrude(thickness)
    )
    
    # Add holes to vertical leg
    if hole_count > 0:
        hole_spacing = (height - 20) / max(1, hole_count - 1) if hole_count > 1 else 0
        vertical_holes = (
            result.faces(">X")
            .workplane()
            .center(0, height / 2)
        )
        
        for i in range(hole_count):
            y_offset = -((hole_count - 1) * hole_spacing) / 2 + i * hole_spacing
            vertical_holes = vertical_holes.pushPoints([(0, y_offset)]).hole(hole_diameter)
        
        result = vertical_holes
    
    # Add holes to horizontal leg
    if hole_count > 0:
        hole_spacing = (depth - 20) / max(1, hole_count - 1) if hole_count > 1 else 0
        horizontal_holes = (
            result.faces(">Z")
            .workplane()
            .center(0, depth / 2 - thickness / 2)
        )
        
        for i in range(hole_count):
            y_offset = -((hole_count - 1) * hole_spacing) / 2 + i * hole_spacing
            horizontal_holes = horizontal_holes.pushPoints([(0, y_offset)]).hole(hole_diameter)
        
        result = horizontal_holes
    
    # Add fillets
    if fillet_radius > 0 and fillet_radius < min(width, thickness) / 2:
        # Confirmed via a real VPS smoke-test run: even a numerically
        # "safe" radius (well under thickness/2) can still fail here -
        # OCCT's fillet feasibility depends on edge topology at the
        # L-corner, not just magnitude. safe_fillet retries smaller and
        # degrades gracefully instead of crashing the whole job.
        result = safe_fillet(result, fillet_radius, "|Y")
    
    return result
