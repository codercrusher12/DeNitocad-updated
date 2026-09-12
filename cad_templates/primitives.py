"""
Basic primitive shapes: shafts, cylinders, spheres, etc.
"""
import cadquery as cq
import math
from ._safe_ops import safe_chamfer

def generate_shaft(params: dict) -> cq.Workplane:
    """
    Generate a simple shaft/cylinder.
    
    Expected params:
    - diameter_mm: Shaft diameter
    - length_mm: Shaft length
    - chamfer_mm: Optional end chamfer
    - keyway_width_mm: Optional keyway width
    - keyway_depth_mm: Optional keyway depth
    """
    diameter = params.get('diameter_mm', 10.0)
    length = params.get('length_mm', 50.0)
    chamfer = params.get('chamfer_mm', 0.0)
    keyway_width = params.get('keyway_width_mm', 0.0)
    keyway_depth = params.get('keyway_depth_mm', 0.0)
    
    # Create cylinder
    result = cq.Workplane("XY").circle(diameter / 2).extrude(length)
    
    # Add chamfer to ends
    if chamfer > 0:
        result = safe_chamfer(result, chamfer, "|Z")
    
    # Add keyway if specified
    if keyway_width > 0 and keyway_depth > 0:
        keyway = (
            cq.Workplane("XZ")
            .center(0, length / 2)
            .rect(keyway_width, length)
            .extrude(diameter / 2 + keyway_depth, both=True)
        )
        result = result.cut(keyway)
    
    return result

def generate_bearing(params: dict) -> cq.Workplane:
    """
    Generate a simple bearing representation (outer race, inner race, gap).
    
    Expected params:
    - inner_diameter_mm: Inner bore
    - outer_diameter_mm: Outer diameter
    - width_mm: Bearing width
    """
    inner = params.get('inner_diameter_mm', 10.0)
    outer = params.get('outer_diameter_mm', 20.0)
    width = params.get('width_mm', 5.0)
    
    # Outer cylinder
    outer_cyl = cq.Workplane("XY").circle(outer / 2).extrude(width)
    
    # Inner bore
    result = outer_cyl.faces(">Z").workplane().hole(inner)
    
    return result

def generate_spacer(params: dict) -> cq.Workplane:
    """
    Generate a spacer/standoff.
    
    Expected params:
    - outer_diameter_mm: Outer diameter
    - inner_diameter_mm: Inner bore
    - length_mm: Length
    """
    outer = params.get('outer_diameter_mm', 10.0)
    inner = params.get('inner_diameter_mm', 5.0)
    length = params.get('length_mm', 10.0)
    
    result = cq.Workplane("XY").circle(outer / 2).extrude(length)
    result = result.faces(">Z").workplane().hole(inner)
    
    return result

def generate_washer(params: dict) -> cq.Workplane:
    """
    Generate a washer.
    
    Expected params:
    - outer_diameter_mm: Outer diameter
    - inner_diameter_mm: Inner hole
    - thickness_mm: Thickness
    """
    outer = params.get('outer_diameter_mm', 10.0)
    inner = params.get('inner_diameter_mm', 5.0)
    thickness = params.get('thickness_mm', 1.5)
    
    result = cq.Workplane("XY").circle(outer / 2).extrude(thickness)
    result = result.faces(">Z").workplane().hole(inner)
    
    return result

def generate_sphere(params: dict) -> cq.Workplane:
    """
    Generate a sphere.
    
    Expected params:
    - diameter_mm: Sphere diameter
    """
    diameter = params.get('diameter_mm', 20.0)
    result = cq.Workplane("XY").sphere(diameter / 2)
    return result
