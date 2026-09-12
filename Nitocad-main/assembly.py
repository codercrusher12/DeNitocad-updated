"""
Assembly support for multi-part CAD generation.
"""
from typing import Any

import cadquery as cq

from cad_templates import TEMPLATES
from logging_config import get_logger

logger = get_logger(__name__)


def generate_assembly(parts_spec: list[dict[str, Any]]) -> cq.Assembly:
    """
    Generate an assembly from multiple parts.
    
    Each part spec should have:
    - name: Part name
    - part_type: Type of part
    - parameters: Part parameters
    - position: (x, y, z) position
    - rotation: (rx, ry, rz) rotation in degrees (optional)
    - constraints: List of constraints (optional)
    """
    assy = cq.Assembly()
    
    for part_spec in parts_spec:
        name = part_spec.get('name', 'part')
        part_type = part_spec.get('part_type')
        params = part_spec.get('parameters', {})
        position = part_spec.get('position', (0, 0, 0))
        rotation = part_spec.get('rotation', (0, 0, 0))
        
        # Generate the part
        if part_type in TEMPLATES:
            template_func = TEMPLATES[part_type]
            workplane = template_func(params)
            
            # Add to assembly with position and rotation
            loc = cq.Location(
                cq.Vector(*position),
                cq.Vector(1, 0, 0),
                rotation[0]
            ) * cq.Location(
                cq.Vector(0, 0, 0),
                cq.Vector(0, 1, 0),
                rotation[1]
            ) * cq.Location(
                cq.Vector(0, 0, 0),
                cq.Vector(0, 0, 1),
                rotation[2]
            )
            
            assy.add(workplane, name=name, loc=loc)
        else:
            # Previously silent - a typo'd or unsupported part_type in an
            # assembly spec just vanished from the output with no signal
            # anywhere. Now at least logged, so a shrinking assembly is
            # diagnosable instead of mysterious.
            logger.warning(
                "assembly part %r has unsupported part_type %r, skipping",
                name, part_type,
            )

    return assy


def get_assembly_parts(assy: cq.Assembly) -> list[tuple[str, cq.Shape]]:
    """Flatten an Assembly into (name, shape) pairs for every leaf part,
    with each part's geometry moved into its assembly-world location -
    so a bracket that was built at the origin and placed at (0, 0, 5)
    comes back already translated there.

    This exists for the multi-part DXF/PDF export path in exporters.py:
    export_all(compound, ...) on the merged compound gives one drawing
    of the whole assembly, but a real fabrication drawing set needs each
    sub-part's own front/top/side views, individually labeled - which
    means walking the assembly tree rather than flattening straight to
    one compound like STL/IGES/section-DXF already did.
    """
    parts: list[tuple[str, cq.Shape]] = []

    def _walk(node: cq.Assembly, parent_loc: cq.Location) -> None:
        world_loc = parent_loc * node.loc
        if node.obj is not None:
            shape = node.obj.val() if isinstance(node.obj, cq.Workplane) else node.obj
            parts.append((node.name, shape.moved(world_loc)))
        for child in node.children:
            _walk(child, world_loc)

    _walk(assy, cq.Location())
    return parts


def parse_assembly_description(description: str) -> list[dict[str, Any]]:
    """
    Parse assembly description into parts list.
    This is a simplified parser - in production, use LLM.
    """
    # For MVP, just return a simple example assembly
    # In production, this would use the LLM to parse complex descriptions
    return [
        {
            'name': 'base_plate',
            'part_type': 'flat_plate',
            'parameters': {
                'length_mm': 100.0,
                'width_mm': 80.0,
                'thickness_mm': 5.0,
                'hole_pattern': 'rectangular',
                'hole_count_x': 4,
                'hole_count_y': 3,
                'hole_diameter_mm': 3.2,
                'corner_fillet_mm': 3.0
            },
            'position': (0, 0, 0),
            'rotation': (0, 0, 0)
        },
        {
            'name': 'bracket',
            'part_type': 'l_bracket',
            'parameters': {
                'width_mm': 40.0,
                'height_mm': 50.0,
                'depth_mm': 30.0,
                'thickness_mm': 3.0,
                'hole_count': 2,
                'hole_diameter_mm': 3.2,
                'fillet_radius_mm': 2.0
            },
            'position': (0, 0, 5.0),
            'rotation': (0, 0, 0)
        }
    ]
