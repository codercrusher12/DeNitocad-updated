"""
Flat plate / flange template with hole patterns.
"""
import math

import cadquery as cq

from ._safe_ops import safe_fillet


def generate_flat_plate(params: dict) -> cq.Workplane:
    """
    Generate a flat plate with holes.
    
    Expected params:
    - length_mm: Plate length
    - width_mm: Plate width
    - thickness_mm: Plate thickness
    - hole_pattern: "rectangular" or "circular"
    - hole_count_x: Holes in X direction ("rectangular"), or total hole
      count evenly spaced on a circle ("circular")
    - hole_count_y: Holes in Y direction ("rectangular" only, ignored for
      "circular")
    - hole_diameter_mm: Hole diameter
    - corner_fillet_mm: Corner fillet radius
    """
    length = params.get("length_mm", 100.0)
    width = params.get("width_mm", 80.0)
    thickness = params.get("thickness_mm", 5.0)
    hole_pattern = params.get("hole_pattern", "rectangular")
    hole_count_x = params.get("hole_count_x", 4)
    hole_count_y = params.get("hole_count_y", 3)
    hole_diameter = params.get("hole_diameter_mm", 3.2)
    corner_fillet = params.get("corner_fillet_mm", 3.0)
    
    # Create base plate
    result = cq.Workplane("XY").box(length, width, thickness)
    
    # Add holes
    if hole_pattern == "rectangular" and hole_count_x > 0 and hole_count_y > 0:
        spacing_x = (length - 20) / max(1, hole_count_x - 1) if hole_count_x > 1 else 0
        spacing_y = (width - 20) / max(1, hole_count_y - 1) if hole_count_y > 1 else 0
        
        points = []
        for i in range(hole_count_x):
            for j in range(hole_count_y):
                x = -((hole_count_x - 1) * spacing_x) / 2 + i * spacing_x
                y = -((hole_count_y - 1) * spacing_y) / 2 + j * spacing_y
                points.append((x, y))
        
        result = (
            result.faces(">Z")
            .workplane()
            .pushPoints(points)
            .hole(hole_diameter)
        )
    elif hole_pattern == "circular" and hole_count_x > 0:
        # hole_count_x doubles as "total hole count" here - the schema
        # only exposes one count field to the circular case, matching
        # rectangular's border convention (holes stay >=10mm from each
        # edge on the smaller dimension).
        bolt_circle_radius = (min(length, width) - 20) / 2
        
        if bolt_circle_radius > 0:
            points = []
            for i in range(hole_count_x):
                angle = 2 * math.pi * i / hole_count_x
                x = bolt_circle_radius * math.cos(angle)
                y = bolt_circle_radius * math.sin(angle)
                points.append((x, y))
            
            result = (
                result.faces(">Z")
                .workplane()
                .pushPoints(points)
                .hole(hole_diameter)
            )
    
    # Add corner fillets
    if corner_fillet > 0 and corner_fillet < min(length, width) / 4:
        result = safe_fillet(result, corner_fillet, "|Z")
    
    return result
