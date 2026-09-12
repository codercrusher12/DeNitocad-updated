"""
Motor mount template for NEMA stepper motors.
Creates a mounting plate with center shaft hole and corner mounting holes.
"""
import cadquery as cq
from ._safe_ops import safe_fillet

def generate_motor_mount(params: dict) -> cq.Workplane:
    """
    Generate a motor mount plate.
    
    Expected params:
    - motor_size_mm: NEMA size (17=42mm, 23=57mm, 34=86mm)
    - thickness_mm: Plate thickness
    - hole_diameter_mm: Mounting hole diameter (typically 3.2mm for M3)
    - fillet_radius_mm: Edge fillet radius
    """
    # NEMA motor dimensions (face size in mm)
    nema_sizes = {
        17: 42.0,
        23: 57.0,
        34: 86.0
    }
    
    motor_size = params.get("motor_size_mm", 50)
    # Find closest NEMA size
    if motor_size <= 45:
        face_size = nema_sizes[17]
    elif motor_size <= 60:
        face_size = nema_sizes[23]
    else:
        face_size = nema_sizes[34]
    
    thickness = params.get("thickness_mm", 3.0)
    hole_diameter = params.get("hole_diameter_mm", 3.2)
    fillet_radius = params.get("fillet_radius_mm", 2.0)
    
    # Plate is 20mm larger than motor on each side
    plate_size = face_size + 40.0
    
    # Create base plate
    result = (
        cq.Workplane("XY")
        .box(plate_size, plate_size, thickness)
    )
    
    # Add center shaft hole (typical NEMA shaft is 5mm for NEMA17, 6.35mm for NEMA23)
    shaft_diameter = 5.0 if face_size == 42.0 else 6.35
    result = result.faces(">Z").workplane().hole(shaft_diameter)
    
    # Add corner mounting holes
    # Hole spacing is typically 31mm for NEMA17, 47mm for NEMA23
    hole_spacing = 31.0 if face_size == 42.0 else 47.0
    
    result = (
        result.faces(">Z")
        .workplane()
        .rect(hole_spacing, hole_spacing, forConstruction=True)
        .vertices()
        .hole(hole_diameter)
    )
    
    # Add fillets to top and bottom edges
    if fillet_radius > 0 and fillet_radius < thickness / 2:
        result = safe_fillet(result, fillet_radius, "|Z")
    
    return result
