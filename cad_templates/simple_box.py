"""
Simple box / enclosure template.
"""
import cadquery as cq

def generate_simple_box(params: dict) -> cq.Workplane:
    """
    Generate a simple box with walls.
    
    Expected params:
    - length_mm: Outer length
    - width_mm: Outer width
    - height_mm: Outer height
    - wall_thickness_mm: Wall thickness
    - has_lid: Whether to create a separate lid
    - lid_fit_tolerance_mm: Tolerance for lid fit
    """
    length = params.get("length_mm", 100.0)
    width = params.get("width_mm", 80.0)
    height = params.get("height_mm", 50.0)
    wall_thickness = params.get("wall_thickness_mm", 3.0)
    has_lid = params.get("has_lid", False)
    lid_tolerance = params.get("lid_fit_tolerance_mm", 0.2)
    
    # Create outer box
    outer = cq.Workplane("XY").box(length, width, height)
    
    # Shell it (remove top face and add walls)
    if has_lid:
        # Create box with open top
        result = outer.faces(">Z").shell(-wall_thickness)
    else:
        # Solid box (no lid)
        result = outer
    
    return result
