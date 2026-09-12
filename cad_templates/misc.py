"""
Miscellaneous parts: hinges, cams, etc.
"""
import cadquery as cq
import math

def generate_hinge(params: dict) -> cq.Workplane:
    """
    Generate a simple hinge.
    
    Expected params:
    - length_mm: Hinge length
    - width_mm: Hinge width (each leaf)
    - thickness_mm: Material thickness
    - knuckle_count: Number of knuckles
    - pin_diameter_mm: Pin diameter
    """
    length = params.get('length_mm', 100.0)
    width = params.get('width_mm', 30.0)
    thickness = params.get('thickness_mm', 2.0)
    knuckle_count = params.get('knuckle_count', 5)
    pin_dia = params.get('pin_diameter_mm', 3.0)
    
    # Create first leaf
    leaf1 = (
        cq.Workplane("XY")
        .rect(width, thickness)
        .extrude(length)
    )
    
    # Create second leaf
    leaf2 = (
        cq.Workplane("XY")
        .center(width + pin_dia, 0)
        .rect(width, thickness)
        .extrude(length)
    )
    
    # Create knuckles (simplified as cylinders)
    knuckle_length = length / knuckle_count
    knuckles = cq.Workplane("XY")
    
    for i in range(knuckle_count):
        z_offset = i * knuckle_length
        knuckle = (
            cq.Workplane("XY")
            .workplane(offset=z_offset)
            .center(width, thickness / 2)
            .circle(pin_dia / 2)
            .extrude(knuckle_length * 0.9)
        )
        if i == 0:
            knuckles = knuckle
        else:
            knuckles = knuckles.union(knuckle)
    
    result = leaf1.union(leaf2).union(knuckles)
    
    return result

def generate_cam(params: dict) -> cq.Workplane:
    """
    Generate a simple cam with a lift profile.
    
    Expected params:
    - base_radius_mm: Base circle radius
    - lift_mm: Maximum lift
    - thickness_mm: Cam thickness
    - bore_diameter_mm: Center bore
    - lift_profile: "simple", "harmonic", or "cycloidal"
    """
    base_radius = params.get('base_radius_mm', 20.0)
    lift = params.get('lift_mm', 10.0)
    thickness = params.get('thickness_mm', 5.0)
    bore = params.get('bore_diameter_mm', 5.0)
    profile_type = params.get('lift_profile', 'simple')
    
    # Generate cam profile points
    num_points = 72  # 5-degree increments
    points = []
    
    for i in range(num_points):
        angle = i * 360.0 / num_points
        angle_rad = math.radians(angle)
        
        # Calculate radius at this angle
        if angle < 90:
            # Rise
            t = angle / 90.0
            if profile_type == 'harmonic':
                r = base_radius + lift * (1 - math.cos(math.pi * t)) / 2
            elif profile_type == 'cycloidal':
                r = base_radius + lift * (t - math.sin(2 * math.pi * t) / (2 * math.pi))
            else:  # simple
                r = base_radius + lift * t
        elif angle < 180:
            # Dwell at max
            r = base_radius + lift
        elif angle < 270:
            # Return
            t = (angle - 180) / 90.0
            if profile_type == 'harmonic':
                r = base_radius + lift * (1 + math.cos(math.pi * t)) / 2
            elif profile_type == 'cycloidal':
                r = base_radius + lift * (1 - t + math.sin(2 * math.pi * t) / (2 * math.pi))
            else:  # simple
                r = base_radius + lift * (1 - t)
        else:
            # Dwell at base
            r = base_radius
        
        x = r * math.cos(angle_rad)
        y = r * math.sin(angle_rad)
        points.append((x, y))
    
    # Create cam profile
    result = cq.Workplane("XY")
    result = result.moveTo(points[0][0], points[0][1])
    for i in range(1, len(points)):
        result = result.lineTo(points[i][0], points[i][1])
    result = result.close()
    result = result.extrude(thickness)
    
    # Add center bore
    if bore > 0:
        result = result.faces(">Z").workplane().hole(bore)
    
    return result
