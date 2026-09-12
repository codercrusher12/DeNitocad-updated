"""
Freeform shapes: sweeps, lofts, revolves.
"""
import cadquery as cq
import math

def generate_revolved_part(params: dict) -> cq.Workplane:
    """
    Generate a revolved part from a profile.
    
    Expected params:
    - profile_points: List of (x, y) points defining the profile
    - revolve_axis: "X" or "Y" (default "Y")
    - revolve_angle: Angle in degrees (default 360)
    """
    profile_points = params.get('profile_points', [
        (0, 0), (10, 0), (10, 20), (5, 25), (0, 25)
    ])
    axis = params.get('revolve_axis', 'Y')
    angle = params.get('revolve_angle', 360)
    
    # Create profile wire
    result = cq.Workplane("XY")
    
    # Start at first point
    result = result.moveTo(profile_points[0][0], profile_points[0][1])
    
    # Draw lines to subsequent points
    for i in range(1, len(profile_points)):
        result = result.lineTo(profile_points[i][0], profile_points[i][1])
    
    # Close the profile
    result = result.close()
    
    # Revolve around axis
    if axis == 'Y':
        result = result.revolve(angle, (0, 0, 0), (0, 1, 0))
    else:
        result = result.revolve(angle, (0, 0, 0), (1, 0, 0))
    
    return result

def generate_swept_part(params: dict) -> cq.Workplane:
    """
    Generate a swept part with a profile along a path.
    
    Expected params:
    - profile_points: List of (x, y) points for cross-section
    - path_points: List of (x, y, z) points for path
    - profile_size: Scale factor for profile
    """
    profile_points = params.get('profile_points', [
        (-5, -5), (5, -5), (5, 5), (-5, 5)
    ])
    path_points = params.get('path_points', [
        (0, 0, 0), (20, 0, 0), (40, 10, 0), (60, 10, 10)
    ])
    profile_size = params.get('profile_size', 1.0)
    
    # Create profile
    profile = cq.Workplane("XY")
    profile = profile.moveTo(profile_points[0][0] * profile_size, profile_points[0][1] * profile_size)
    for i in range(1, len(profile_points)):
        profile = profile.lineTo(profile_points[i][0] * profile_size, profile_points[i][1] * profile_size)
    profile = profile.close()
    
    # Create path
    path = cq.Workplane("XZ")
    path = path.moveTo(path_points[0][0], path_points[0][2])
    for i in range(1, len(path_points)):
        path = path.lineTo(path_points[i][0], path_points[i][2])
    
    # Sweep profile along path
    result = profile.sweep(path)
    
    return result

def generate_lofted_part(params: dict) -> cq.Workplane:
    """
    Generate a lofted part between multiple profiles.
    
    Expected params:
    - profiles: List of profile definitions, each with points and z-offset
    """
    profiles = params.get('profiles', [
        {'points': [(-10, -10), (10, -10), (10, 10), (-10, 10)], 'z': 0},
        {'points': [(-5, -5), (5, -5), (5, 5), (-5, 5)], 'z': 20},
        {'points': [(-8, -8), (8, -8), (8, 8), (-8, 8)], 'z': 40}
    ])
    
    # Create each profile as a workplane
    wires = []
    for profile_def in profiles:
        points = profile_def['points']
        z_offset = profile_def['z']
        
        wp = cq.Workplane("XY").workplane(offset=z_offset)
        wp = wp.moveTo(points[0][0], points[0][1])
        for i in range(1, len(points)):
            wp = wp.lineTo(points[i][0], points[i][1])
        wp = wp.close()
        wires.append(wp)
    
    # Loft between profiles
    result = wires[0].loft(wires[1:])
    
    return result

def generate_freeform(params: dict) -> cq.Workplane:
    """
    Main freeform generator that routes to sweep/loft/revolve.
    
    Expected params:
    - operation: "sweep", "loft", "revolve"
    - Other params depend on operation
    """
    operation = params.get('operation', 'revolve')
    
    if operation == 'revolve':
        return generate_revolved_part(params)
    elif operation == 'sweep':
        return generate_swept_part(params)
    elif operation == 'loft':
        return generate_lofted_part(params)
    else:
        # Default to a simple revolved shape
        return generate_revolved_part(params)
