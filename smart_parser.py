"""
Smart parser with regex-based number extraction and constraint inference.
Much more capable than simple keyword matching.
"""
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field


@dataclass
class ExtractedDimension:
    """A dimension extracted from text with context."""
    value: float
    unit: str  # 'mm', 'inch', 'unitless'
    context: str  # what it refers to
    raw_text: str

@dataclass
class ExtractedPattern:
    """A hole or feature pattern."""
    pattern_type: str  # 'rectangular', 'circular', 'linear'
    count: int
    diameter: float | None = None
    spacing: float | None = None
    layout: str | None = None

class ParsedParameters(BaseModel):
    """Structured parameters extracted from natural language."""
    part_type: str
    parameters: dict[str, Any]
    material: str | None = None
    operations: list[dict[str, Any]] = Field(default_factory=list)
    assembly_parts: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = 0.0
    warnings: list[str] = Field(default_factory=list)

# Unit conversion
UNIT_TO_MM = {
    'mm': 1.0,
    'millimeter': 1.0,
    'millimeters': 1.0,
    'cm': 10.0,
    'centimeter': 10.0,
    'm': 1000.0,
    'meter': 1000.0,
    'inch': 25.4,
    'inches': 25.4,
    'in': 25.4,
    '"': 25.4,
    "'": 304.8,
    'ft': 304.8,
    'foot': 304.8,
    'feet': 304.8,
}

# Standard part sizes (for inference)
STANDARD_SIZES = {
    'nema_17': {'face': 42.0, 'shaft': 5.0, 'holes': 31.0},
    'nema_23': {'face': 57.0, 'shaft': 6.35, 'holes': 47.0},
    'nema_34': {'face': 86.0, 'shaft': 14.0, 'holes': 71.5},
    'm3': {'diameter': 3.0, 'tap_drill': 2.5, 'clearance': 3.2},
    'm4': {'diameter': 4.0, 'tap_drill': 3.3, 'clearance': 4.3},
    'm5': {'diameter': 5.0, 'tap_drill': 4.2, 'clearance': 5.3},
    'm6': {'diameter': 6.0, 'tap_drill': 5.0, 'clearance': 6.4},
    'm8': {'diameter': 8.0, 'tap_drill': 6.8, 'clearance': 8.4},
    '1/4-20': {'diameter': 6.35, 'tap_drill': 5.1, 'clearance': 7.0},
    '3/8-16': {'diameter': 9.525, 'tap_drill': 8.0, 'clearance': 10.5},
}

def extract_all_dimensions(text: str) -> list[ExtractedDimension]:
    """Extract all dimensions with units from text using regex."""
    dimensions = []

    # Extract fractional inches
    for match in re.finditer(r'(\d+)?-?(\d+)/(\d+)\s*(["\']|inch|inches|in)', text, re.IGNORECASE):
        whole = int(match.group(1)) if match.group(1) else 0
        num = int(match.group(2))
        den = int(match.group(3))
        value = whole + num/den
        dimensions.append(ExtractedDimension(
            value=value * 25.4,
            unit='inch',
            context='',
            raw_text=match.group(0)
        ))
    
    # Extract decimal with units
    for match in re.finditer(r'(\d+(?:\.\d+)?)\s*(mm|millimeters?|cm|centimeters?|m|meters?|inch|inches|in|ft|feet|foot)\b', text, re.IGNORECASE):
        value = float(match.group(1))
        unit = match.group(2).lower()
        # Normalize unit
        if unit in ['mm', 'millimeter', 'millimeters']:
            unit_mm = 'mm'
        elif unit in ['cm', 'centimeter', 'centimeters']:
            unit_mm = 'cm'
        elif unit in ['m', 'meter', 'meters']:
            unit_mm = 'm'
        elif unit in ['inch', 'inches', 'in']:
            unit_mm = 'inch'
        elif unit in ['ft', 'feet', 'foot']:
            unit_mm = 'ft'
        else:
            unit_mm = unit
        
        dimensions.append(ExtractedDimension(
            value=value,
            unit=unit_mm,
            context='',
            raw_text=match.group(0)
        ))
    
    # Extract bare numbers with context
    context_patterns = [
        (r'(width|wide|w)\s*(?:of|is|=|:)?\s*(\d+(?:\.\d+)?)', 'width'),
        (r'(height|tall|h)\s*(?:of|is|=|:)?\s*(\d+(?:\.\d+)?)', 'height'),
        (r'(depth|deep|d)\s*(?:of|is|=|:)?\s*(\d+(?:\.\d+)?)', 'depth'),
        (r'(length|long|l)\s*(?:of|is|=|:)?\s*(\d+(?:\.\d+)?)', 'length'),
        (r'(thick(?:ness)?)\s*(?:of|is|=|:)?\s*(\d+(?:\.\d+)?)', 'thickness'),
        (r'(diameter|dia|diam)\s*(?:of|is|=|:)?\s*(\d+(?:\.\d+)?)', 'diameter'),
        (r'(radius|r)\s*(?:of|is|=|:)?\s*(\d+(?:\.\d+)?)', 'radius'),
        (r'(module)\s*(?:of|is|=|:)?\s*(\d+(?:\.\d+)?)', 'module'),
        (r'(teeth|tooth\s*count)\s*(?:of|is|=|:)?\s*(\d+(?:\.\d+)?)', 'teeth'),
        (r'(bore)\s*(?:diameter\s*)?(?:of|is|=|:)?\s*(\d+(?:\.\d+)?)', 'bore'),
        (r'(throws|num\s*throws)\s*(?:of|is|=|:)?\s*(\d+(?:\.\d+)?)', 'throws'),
        (r'(cylinders?)\s*(?:of|is|=|:)?\s*(\d+(?:\.\d+)?)', 'cylinders'),
        (r'(stroke)\s*(?:of|is|=|:)?\s*(\d+(?:\.\d+)?)', 'stroke'),
        (r'(\d+(?:\.\d+)?)\s*(?:mm\s*)?(?:thick|thickness)', 'thickness'),
        (r'(\d+(?:\.\d+)?)\s*(?:mm\s*)?(?:wide|width)', 'width'),
        (r'(\d+(?:\.\d+)?)\s*(?:mm\s*)?(?:tall|height)', 'height'),
        (r'(\d+(?:\.\d+)?)\s*(?:mm\s*)?(?:deep|depth)', 'depth'),
        (r'(\d+(?:\.\d+)?)\s*(?:mm\s*)?(?:long|length)', 'length'),
        (r'(\d+(?:\.\d+)?)\s*(?:mm\s*)?(?:\w+\s+){0,2}(?:diameter|dia)', 'diameter'),
        (r'(\d+(?:\.\d+)?)\s*(?:mm\s*)?radius', 'radius'),
        (r'(\d+(?:\.\d+)?)\s*teeth', 'teeth'),
        (r'(\d+(?:\.\d+)?)\s*(?:mm\s*)?bore(?:\s*diameter)?', 'bore'),
        (r'(\d+(?:\.\d+)?)\s*throws?', 'throws'),
        (r'(\d+(?:\.\d+)?)\s*-?\s*cylinders?', 'cylinders'),
        (r'(\d+(?:\.\d+)?)\s*(?:mm\s*)?stroke', 'stroke'),
    ]
    
    for pattern, context in context_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            # Find the number in the match
            num_match = re.search(r'(\d+(?:\.\d+)?)', match.group(0))
            if num_match:
                value = float(num_match.group(1))
                # Check if unit follows
                unit_match = re.search(r'(mm|cm|m|inch|in|ft)', match.group(0)[num_match.end():], re.IGNORECASE)
                unit = unit_match.group(1).lower() if unit_match else 'mm'
                
                dimensions.append(ExtractedDimension(
                    value=value,
                    unit=unit,
                    context=context,
                    raw_text=match.group(0)
                ))
    
    return dimensions

def extract_hole_patterns(text: str) -> list[ExtractedPattern]:
    """Extract hole patterns from text."""
    patterns = []
    text_lower = text.lower()
    
    # Pattern: "4 holes", "6 holes", etc.
    hole_count_match = re.search(r'(\d+)\s*(?:mounting\s*)?holes?', text_lower)
    hole_diameter = None
    hole_spacing = None
    
    # Extract hole diameter. Two orderings show up in practice:
    # "hole diameter of 6mm" / "holes with 6mm dia" (keyword before number)
    # and "6mm hole diameter" (number before keyword) - the original regex
    # only caught the first, so "6mm hole diameter" fell through silently.
    dia_match = re.search(r'(?:hole|holes)\s*(?:of|with|dia(?:meter)?)\s*(\d+(?:\.\d+)?)', text_lower)
    if dia_match:
        hole_diameter = float(dia_match.group(1))
    else:
        dia_match_rev = re.search(r'(\d+(?:\.\d+)?)\s*(?:mm\s*)?holes?\s*dia(?:meter)?', text_lower)
        if dia_match_rev:
            hole_diameter = float(dia_match_rev.group(1))
        else:
            # Check for M-size
            m_match = re.search(r'\b(m\d+)\b', text_lower)
            if m_match:
                m_size = m_match.group(1).upper()
                if m_size in STANDARD_SIZES:
                    hole_diameter = STANDARD_SIZES[m_size]['clearance']
    
    # Extract spacing
    spacing_match = re.search(r'(?:spacing|pitch|center)\s*(?:of|is|=|:)?\s*(\d+(?:\.\d+)?)', text_lower)
    if spacing_match:
        hole_spacing = float(spacing_match.group(1))
    
    # Determine pattern type
    if 'circular' in text_lower or 'bolt circle' in text_lower or 'bcd' in text_lower:
        pattern_type = 'circular'
    elif 'rectangular' in text_lower or 'grid' in text_lower or 'array' in text_lower:
        pattern_type = 'rectangular'
    elif 'linear' in text_lower or 'row' in text_lower:
        pattern_type = 'linear'
    else:
        pattern_type = 'rectangular'  # default
    
    if hole_count_match:
        count = int(hole_count_match.group(1))
        patterns.append(ExtractedPattern(
            pattern_type=pattern_type,
            count=count,
            diameter=hole_diameter,
            spacing=hole_spacing
        ))
    
    # Check for X by Y pattern (e.g., "4x3 holes", "4 by 3 pattern")
    grid_match = re.search(r'(\d+)\s*(?:x|by|×)\s*(\d+)\s*(?:holes?|pattern|grid|array)', text_lower)
    if grid_match:
        count_x = int(grid_match.group(1))
        count_y = int(grid_match.group(2))
        patterns.append(ExtractedPattern(
            pattern_type='rectangular',
            count=count_x * count_y,
            diameter=hole_diameter,
            spacing=hole_spacing,
            layout=f'{count_x}x{count_y}'
        ))
    
    return patterns

def extract_operations(text: str) -> list[dict[str, Any]]:
    """Extract CAD operations like fillets, chamfers, shells."""
    operations = []
    text_lower = text.lower()
    
    # Fillets
    fillet_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:mm\s*)?fillet', text_lower)
    if fillet_match:
        operations.append({
            'type': 'fillet',
            'radius': float(fillet_match.group(1)),
            'edges': 'all'
        })
    
    # Chamfers
    chamfer_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:mm\s*)?chamfer', text_lower)
    if chamfer_match:
        operations.append({
            'type': 'chamfer',
            'size': float(chamfer_match.group(1)),
            'edges': 'all'
        })
    
    # Shell
    shell_match = re.search(r'shell(?:ed)?\s*(?:with|of|is|=|:)?\s*(\d+(?:\.\d+)?)', text_lower)
    if shell_match or 'shell' in text_lower or 'hollow' in text_lower:
        thickness = float(shell_match.group(1)) if shell_match else 2.0
        operations.append({
            'type': 'shell',
            'thickness': thickness,
            'open_top': 'lid' in text_lower or 'open' in text_lower
        })
    
    # Revolve
    if 'revolve' in text_lower or 'lathe' in text_lower or 'turned' in text_lower:
        operations.append({'type': 'revolve'})
    
    # Sweep
    if 'sweep' in text_lower or 'extrude along' in text_lower:
        operations.append({'type': 'sweep'})
    
    # Loft
    if 'loft' in text_lower:
        operations.append({'type': 'loft'})
    
    return operations

def infer_part_type(text: str, dimensions: list[ExtractedDimension], operations: list[dict]) -> str:
    """Infer part type from text and extracted data."""
    text_lower = text.lower()
    
    # Check for assembly keywords
    if any(word in text_lower for word in ['assembly', 'assemble', 'multiple parts', 'together']):
        return 'assembly'
    
    # Check for freeform operations
    has_freeform = any(op['type'] in ['revolve', 'sweep', 'loft'] for op in operations)
    
    # Part type detection with priority
    if any(word in text_lower for word in ['motor', 'stepper', 'servo']):
        return 'motor_mount'
    # These three are checked early, before the shorter/more generic
    # 'standoff' and 'channel' keywords below, which would otherwise
    # shadow them (e.g. "hex standoff" contains "standoff", "channel
    # bracket" contains "channel" - substring checks need the more
    # specific phrase tested first).
    elif any(word in text_lower for word in ['hex standoff', 'hex spacer', 'hexagonal standoff']):
        return 'hex_standoff'
    elif any(word in text_lower for word in ['t-bracket', 't bracket', 'tee bracket']):
        return 't_bracket'
    elif any(word in text_lower for word in ['channel bracket', 'cable channel', 'cable clamp', 'cable clip', 'mounting channel']):
        return 'channel_bracket'
    # Same shadowing concern as the three above: 'crankshaft' contains
    # 'shaft' and 'connecting rod'/'conrod' would otherwise match the
    # much more generic 'rod'/'pin' keywords in the plain shaft check
    # further down - both need to be tested first.
    elif any(word in text_lower for word in ['crankshaft', 'crank shaft', 'crank']):
        return 'crankshaft'
    elif any(word in text_lower for word in ['connecting rod', 'conrod', 'con-rod', 'con rod', 'piston rod']):
        return 'connecting_rod'
    elif any(word in text_lower for word in ['l-bracket', 'l bracket', 'angle bracket']):
        return 'l_bracket'
    elif any(word in text_lower for word in ['shaft', 'axle', 'rod', 'pin']):
        return 'shaft'
    elif any(word in text_lower for word in ['sprocket', 'chain sprocket']):
        return 'sprocket'
    elif any(word in text_lower for word in ['gear']):
        return 'gear'
    elif any(word in text_lower for word in ['pulley', 'belt']):
        return 'pulley'
    elif any(word in text_lower for word in ['bearing', 'bushing', 'bush']):
        return 'bearing'
    elif any(word in text_lower for word in ['pipe', 'tube', 'elbow', 'fitting']):
        return 'pipe_fitting'
    elif any(word in text_lower for word in ['flange']):
        return 'flange'
    elif any(word in text_lower for word in ['beam', 'i-beam', 'channel', 'c-channel']):
        return 'structural_beam'
    elif any(word in text_lower for word in ['hinge', 'piano hinge']):
        return 'hinge'
    elif any(word in text_lower for word in ['spacer', 'standoff']):
        return 'spacer'
    elif any(word in text_lower for word in ['washer']):
        return 'washer'
    elif any(word in text_lower for word in ['cam', 'cam profile']):
        return 'cam'
    elif any(word in text_lower for word in ['box', 'enclosure', 'case', 'housing']):
        return 'simple_box'
    elif any(word in text_lower for word in ['plate', 'flange', 'blank']):
        return 'flat_plate'
    elif has_freeform:
        return 'freeform'
    else:
        # Default based on dimension count
        if len(dimensions) <= 2:
            return 'flat_plate'
        elif len(dimensions) <= 4:
            return 'l_bracket'
        else:
            return 'flat_plate'

def infer_missing_dimensions(part_type: str, dimensions: list[ExtractedDimension], patterns: list[ExtractedPattern], description: str = "", operations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Infer missing dimensions based on part type and context."""
    params = {}
    text_lower = description.lower()
    operations = operations or []
    
    # A fillet phrased naturally ("8mm fillets on all edges") is caught by
    # extract_operations' fillet regex, not by the 'radius' context pattern
    # below (which needs the literal word "radius"). Prefer that parsed
    # value for the template's own fillet_radius_mm/corner_fillet_mm so the
    # part isn't built with a silent 2.0/3.0mm default while the real
    # request sits unused in `operations` and later fails as a redundant
    # second fillet pass on already-rounded edges.
    fillet_op_radius = next((op['radius'] for op in operations if op.get('type') == 'fillet'), None)
    
    # Map extracted dimensions to parameters
    dim_map = {}
    for dim in dimensions:
        # Convert to mm
        value_mm = dim.value * UNIT_TO_MM.get(dim.unit, 1.0)
        if dim.context:
            dim_map[dim.context] = value_mm
        elif 'width' not in dim_map:
            dim_map['width'] = value_mm
        elif 'height' not in dim_map:
            dim_map['height'] = value_mm
        elif 'length' not in dim_map:
            dim_map['length'] = value_mm
        elif 'thickness' not in dim_map:
            dim_map['thickness'] = value_mm
    
    # Part-specific inference
    if part_type == 'motor_mount':
        motor_size = dim_map.get('width', dim_map.get('diameter', 50))
        params['motor_size_mm'] = motor_size
        params['thickness_mm'] = dim_map.get('thickness', 3.0)
        params['hole_diameter_mm'] = dim_map.get('diameter', 3.2)
        params['fillet_radius_mm'] = fillet_op_radius if fillet_op_radius is not None else dim_map.get('radius', 2.0)
        
    elif part_type == 'l_bracket':
        params['width_mm'] = dim_map.get('width', 50.0)
        params['height_mm'] = dim_map.get('height', 60.0)
        params['depth_mm'] = dim_map.get('depth', 40.0)
        params['thickness_mm'] = dim_map.get('thickness', 3.0)
        params['hole_count'] = patterns[0].count if patterns else 2
        params['hole_diameter_mm'] = dim_map.get('diameter', 3.2)
        params['fillet_radius_mm'] = fillet_op_radius if fillet_op_radius is not None else dim_map.get('radius', 2.0)
        
    elif part_type == 'flat_plate':
        params['length_mm'] = dim_map.get('length', 100.0)
        params['width_mm'] = dim_map.get('width', 80.0)
        params['thickness_mm'] = dim_map.get('thickness', 5.0)
        params['hole_pattern'] = 'rectangular'
        if patterns:
            p = patterns[0]
            if p.layout:
                parts = p.layout.split('x')
                params['hole_count_x'] = int(parts[0])
                params['hole_count_y'] = int(parts[1])
            else:
                params['hole_count_x'] = min(4, p.count)
                params['hole_count_y'] = max(1, p.count // 4)
            if p.diameter:
                params['hole_diameter_mm'] = p.diameter
        else:
            params['hole_count_x'] = 4
            params['hole_count_y'] = 3
            params['hole_diameter_mm'] = dim_map.get('diameter', 3.2)
        params['corner_fillet_mm'] = fillet_op_radius if fillet_op_radius is not None else dim_map.get('radius', 3.0)
        
    elif part_type == 'shaft':
        params['diameter_mm'] = dim_map.get('diameter', 10.0)
        params['length_mm'] = dim_map.get('length', 50.0)
        if 'chamfer' in [d.context for d in dimensions]:
            params['chamfer_mm'] = dim_map.get('chamfer', 0.5)
        
    elif part_type == 'gear':
        params['module'] = dim_map.get('module', 2.0)
        params['teeth'] = int(dim_map.get('teeth', 20))
        params['thickness_mm'] = dim_map.get('thickness', 10.0)
        params['bore_diameter_mm'] = dim_map.get('bore', 5.0)
        
    elif part_type == 'sprocket':
        params['teeth'] = int(dim_map.get('teeth', 18))
        params['chain_pitch_mm'] = dim_map.get('pitch', 12.7)
        params['thickness_mm'] = dim_map.get('thickness', 8.0)
        params['bore_diameter_mm'] = dim_map.get('bore', 8.0)
        
    elif part_type == 'pulley':
        params['outer_diameter_mm'] = dim_map.get('diameter', 40.0)
        params['belt_width_mm'] = dim_map.get('width', 10.0)
        params['bore_diameter_mm'] = dim_map.get('bore', 5.0)
        params['thickness_mm'] = dim_map.get('thickness', 15.0)
        
    elif part_type == 'bearing':
        params['inner_diameter_mm'] = dim_map.get('inner_diameter', dim_map.get('diameter', 10.0))
        params['outer_diameter_mm'] = dim_map.get('outer_diameter', 20.0)
        params['width_mm'] = dim_map.get('width', 5.0)
        
    elif part_type == 'pipe_fitting':
        params['outer_diameter_mm'] = dim_map.get('diameter', 20.0)
        params['wall_thickness_mm'] = dim_map.get('thickness', 2.0)
        params['length_mm'] = dim_map.get('length', 50.0)
        
    elif part_type == 'flange':
        params['outer_diameter_mm'] = dim_map.get('diameter', 100.0)
        params['inner_diameter_mm'] = dim_map.get('inner_diameter', 50.0)
        params['thickness_mm'] = dim_map.get('thickness', 10.0)
        params['hole_count'] = 4
        params['hole_diameter_mm'] = 5.0
        
    elif part_type == 'structural_beam':
        params['height_mm'] = dim_map.get('height', 100.0)
        params['width_mm'] = dim_map.get('width', 50.0)
        params['length_mm'] = dim_map.get('length', 200.0)
        params['thickness_mm'] = dim_map.get('thickness', 5.0)
        # Previously never set at all, so every regex-parsed structural
        # beam silently defaulted to i_beam regardless of what the
        # description said - "c-channel beam" would generate an I-beam.
        params['beam_type'] = 'channel' if any(
            kw in text_lower for kw in ['channel', 'c-channel']
        ) else 'i_beam'
        
    elif part_type == 'hinge':
        params['length_mm'] = dim_map.get('length', 100.0)
        params['width_mm'] = dim_map.get('width', 30.0)
        params['thickness_mm'] = dim_map.get('thickness', 2.0)
        params['knuckle_count'] = 5
        
    elif part_type == 'spacer':
        params['outer_diameter_mm'] = dim_map.get('diameter', 10.0)
        params['inner_diameter_mm'] = dim_map.get('inner_diameter', 5.0)
        params['length_mm'] = dim_map.get('length', 10.0)
        
    elif part_type == 'washer':
        params['outer_diameter_mm'] = dim_map.get('diameter', 10.0)
        params['inner_diameter_mm'] = dim_map.get('inner_diameter', 5.0)
        params['thickness_mm'] = dim_map.get('thickness', 1.5)
        
    elif part_type == 'cam':
        params['base_radius_mm'] = dim_map.get('radius', 20.0)
        params['lift_mm'] = dim_map.get('lift', 10.0)
        params['thickness_mm'] = dim_map.get('thickness', 5.0)
        params['bore_diameter_mm'] = dim_map.get('bore', 5.0)
        
    elif part_type == 'simple_box':
        params['length_mm'] = dim_map.get('length', 100.0)
        params['width_mm'] = dim_map.get('width', 80.0)
        params['height_mm'] = dim_map.get('height', 50.0)
        params['wall_thickness_mm'] = dim_map.get('thickness', 3.0)
        params['has_lid'] = False

    elif part_type == 'connecting_rod':
        params['center_distance_mm'] = dim_map.get('length', dim_map.get('center_distance', 120.0))
        params['big_end_diameter_mm'] = dim_map.get('bore', dim_map.get('diameter', 24.0))
        params['small_end_diameter_mm'] = dim_map.get('inner_diameter', 12.0)
        params['thickness_mm'] = dim_map.get('thickness', 12.0)

    elif part_type == 'crankshaft':
        if 'throws' in dim_map or 'cylinders' in dim_map:
            params['num_throws'] = int(dim_map.get('throws', dim_map.get('cylinders', 4)))
        params['stroke_mm'] = dim_map.get('stroke', dim_map.get('length', 80.0))
        params['main_journal_diameter_mm'] = dim_map.get('diameter', 50.0)

    elif part_type == 'freeform':
        # Two bugs found via live testing, not just one:
        # (1) profile_points was set to an explicit [] here, which defeats
        #     the template's own `params.get('profile_points', [default])`
        #     fallback - a present-but-empty key is not a missing key, so
        #     the template crashed on profile_points[0] instead of using
        #     its default shape. Omitting the key when we have nothing
        #     real to put there lets that fallback actually work.
        # (2) generate_freeform() dispatches on params['operation'], not
        #     on separate sweep/loft/revolve booleans - setting those
        #     booleans (as this branch previously did) was dead code that
        #     nothing downstream reads. The real dispatch key was never
        #     set at all, so it silently always defaulted to 'revolve'.
        op_types = [op.get('type') for op in operations if op.get('type') in ('sweep', 'loft', 'revolve')]
        params['operation'] = op_types[0] if op_types else 'revolve'
        
    return params

def extract_material(text: str) -> str | None:
    """Extract material from text."""
    text_lower = text.lower()
    
    materials = {
        'aluminum': 'aluminum_6061',
        'aluminium': 'aluminum_6061',
        '6061': 'aluminum_6061',
        '6063': 'aluminum_6063',
        '7075': 'aluminum_7075',
        'steel': 'steel_1018',
        '1018': 'steel_1018',
        '4140': 'steel_4140',
        'stainless': 'stainless_304',
        '304': 'stainless_304',
        '316': 'stainless_316',
        'brass': 'brass_c360',
        'copper': 'copper_c110',
        'bronze': 'bronze_phosphor',
        'titanium': 'titanium_ti6al4v',
        'plastic': 'plastic_abs',
        'abs': 'plastic_abs',
        'pla': 'plastic_pla',
        'nylon': 'plastic_nylon',
        'delrin': 'plastic_delrin',
        'acetal': 'plastic_delrin',
        'wood': 'wood_plywood',
        'plywood': 'wood_plywood',
        'mdf': 'wood_mdf',
    }
    
    for keyword, material in materials.items():
        if keyword in text_lower:
            return material
    
    return None

def parse_description(description: str, use_claude: bool = False, api_key: str = None) -> ParsedParameters:
    """
    Main parsing function with smart extraction.
    """
    warnings = []
    
    # Extract all data
    dimensions = extract_all_dimensions(description)
    patterns = extract_hole_patterns(description)
    operations = extract_operations(description)
    material = extract_material(description)
    
    # Infer part type
    part_type = infer_part_type(description, dimensions, operations)
    
    # Infer missing dimensions
    parameters = infer_missing_dimensions(part_type, dimensions, patterns, description, operations)
    
    # Templates that expose their own fillet_radius_mm/corner_fillet_mm
    # already apply the parsed fillet value directly (see infer_missing_dimensions
    # above). Leaving the matching entry in `operations` would make
    # cad_generator._apply_operations attempt a second, redundant fillet
    # pass on edges that are already rounded, which fails and gets
    # silently dropped. Drop it here once it's been consumed by the template.
    consumed_fillet_radius = parameters.get('fillet_radius_mm', parameters.get('corner_fillet_mm'))
    if consumed_fillet_radius is not None:
        operations = [
            op for op in operations
            if not (op.get('type') == 'fillet' and op.get('radius') == consumed_fillet_radius)
        ]
    
    # Same double-application problem for freeform parts: infer_missing_dimensions
    # already consumed the sweep/loft/revolve entry into parameters['operation']
    # to pick which generator function to call. Leaving it in `operations` too
    # makes apply_operations() try to treat it as a post-build op it doesn't
    # recognize (it only knows fillet/chamfer/shell), logging a harmless but
    # noisy "unknown operation type 'revolve' ignored" warning downstream.
    consumed_operation = parameters.get('operation')
    if consumed_operation is not None:
        operations = [op for op in operations if op.get('type') != consumed_operation]
    
    # Add operations to parameters
    if operations:
        parameters['operations'] = operations
    
    # Validate and warn
    if not dimensions:
        warnings.append("No dimensions found in description, using defaults")
    
    if part_type == 'freeform' and not parameters.get('profile_points'):
        warnings.append("Freeform part requires profile points, using default shape")
    
    # Confidence scoring
    confidence = 0.5
    if dimensions:
        confidence += 0.2
    if patterns:
        confidence += 0.1
    if material:
        confidence += 0.1
    if part_type != 'flat_plate':  # More specific than default
        confidence += 0.1
    
    return ParsedParameters(
        part_type=part_type,
        parameters=parameters,
        material=material,
        operations=operations,
        assembly_parts=[],
        confidence=min(confidence, 1.0),
        warnings=warnings
    )
