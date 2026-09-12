"""
LLM-based parameter extraction from natural language descriptions.
"""
import json
from typing import Any

from pydantic import BaseModel, Field


class ParsedParameters(BaseModel):
    """Structured parameters extracted from natural language."""
    part_type: str = Field(..., description="Type of part: motor_mount, l_bracket, flat_plate, simple_box")
    parameters: dict[str, Any] = Field(..., description="Part-specific parameters")
    material: str | None = Field(None, description="Material specification")
    confidence: float = Field(0.0, description="Confidence score 0-1")

SYSTEM_PROMPT = """You are a CAD parameter extraction system. Parse natural language descriptions of mechanical parts into structured JSON.

You must extract parameters for one of these part types:
1. motor_mount: For stepper motor mounting plates
   - motor_size_mm: NEMA size (17=42mm, 23=57mm, 34=86mm) or actual size
   - thickness_mm: Plate thickness (default 3mm)
   - hole_diameter_mm: Mounting hole diameter (default 3.2mm for M3)
   - fillet_radius_mm: Edge fillet radius (default 2mm)

2. l_bracket: L-shaped mounting bracket
   - width_mm: Bracket width (default 50mm)
   - height_mm: Vertical leg height (default 60mm)
   - depth_mm: Horizontal leg depth (default 40mm)
   - thickness_mm: Material thickness (default 3mm)
   - hole_count: Holes per leg (default 2)
   - hole_diameter_mm: Hole diameter (default 3.2mm)
   - fillet_radius_mm: Edge fillet radius (default 2mm)

3. flat_plate: Flat plate with hole pattern
   - length_mm: Plate length (default 100mm)
   - width_mm: Plate width (default 80mm)
   - thickness_mm: Plate thickness (default 5mm)
   - hole_pattern: "rectangular" or "circular" (default rectangular)
   - hole_count_x: Holes in X direction (default 4)
   - hole_count_y: Holes in Y direction (default 3)
   - hole_diameter_mm: Hole diameter (default 3.2mm)
   - corner_fillet_mm: Corner fillet radius (default 3mm)

4. simple_box: Box enclosure
   - length_mm: Outer length (default 100mm)
   - width_mm: Outer width (default 80mm)
   - height_mm: Outer height (default 50mm)
   - wall_thickness_mm: Wall thickness (default 3mm)
   - has_lid: Boolean for separate lid (default false)
   - lid_fit_tolerance_mm: Lid fit tolerance (default 0.2mm)

Return JSON in this exact format:
{
  "part_type": "motor_mount|l_bracket|flat_plate|simple_box",
  "parameters": { ... },
  "material": "aluminum_6061|steel_1018|plastic_abs|...",
  "confidence": 0.95
}

If the description is ambiguous or incomplete, use sensible defaults and set confidence accordingly.
If the description cannot be mapped to these part types, set part_type to "unknown" and confidence to 0.0."""

def parse_with_mock_llm(description: str) -> ParsedParameters:
    """
    Mock LLM parser for testing without API keys.
    Uses simple keyword matching to extract parameters.
    """
    desc_lower = description.lower()
    
    # Determine part type
    if "motor" in desc_lower or "stepper" in desc_lower:
        part_type = "motor_mount"
        params = {
            "motor_size_mm": 50,
            "thickness_mm": 3.0,
            "hole_diameter_mm": 3.2,
            "fillet_radius_mm": 2.0
        }
        
        # Extract motor size
        if "50mm" in desc_lower:
            params["motor_size_mm"] = 50
        elif "42mm" in desc_lower or "nema 17" in desc_lower:
            params["motor_size_mm"] = 42
        elif "57mm" in desc_lower or "nema 23" in desc_lower:
            params["motor_size_mm"] = 57
        elif "86mm" in desc_lower or "nema 34" in desc_lower:
            params["motor_size_mm"] = 86
        
        # Extract thickness
        if "3mm thick" in desc_lower or "3mm thickness" in desc_lower:
            params["thickness_mm"] = 3.0
        elif "5mm thick" in desc_lower:
            params["thickness_mm"] = 5.0
        
        # Extract fillet
        if "5mm fillet" in desc_lower:
            params["fillet_radius_mm"] = 5.0
        elif "2mm fillet" in desc_lower:
            params["fillet_radius_mm"] = 2.0
        
        # Extract hole count
        if "4 holes" in desc_lower or "4 mounting holes" in desc_lower:
            pass  # Default is 4 for motor mount
        
    elif "l-bracket" in desc_lower or "l bracket" in desc_lower or "bracket" in desc_lower:
        part_type = "l_bracket"
        params = {
            "width_mm": 50.0,
            "height_mm": 60.0,
            "depth_mm": 40.0,
            "thickness_mm": 3.0,
            "hole_count": 2,
            "hole_diameter_mm": 3.2,
            "fillet_radius_mm": 2.0
        }
        
        # Extract dimensions
        if "50mm wide" in desc_lower:
            params["width_mm"] = 50.0
        if "60mm tall" in desc_lower or "60mm height" in desc_lower:
            params["height_mm"] = 60.0
        
    elif "plate" in desc_lower or "flange" in desc_lower:
        part_type = "flat_plate"
        params = {
            "length_mm": 100.0,
            "width_mm": 80.0,
            "thickness_mm": 5.0,
            "hole_pattern": "rectangular",
            "hole_count_x": 4,
            "hole_count_y": 3,
            "hole_diameter_mm": 3.2,
            "corner_fillet_mm": 3.0
        }
        
        # Extract dimensions
        if "100mm" in desc_lower:
            params["length_mm"] = 100.0
        if "80mm" in desc_lower:
            params["width_mm"] = 80.0
        
    elif "box" in desc_lower or "enclosure" in desc_lower:
        part_type = "simple_box"
        params = {
            "length_mm": 100.0,
            "width_mm": 80.0,
            "height_mm": 50.0,
            "wall_thickness_mm": 3.0,
            "has_lid": "lid" in desc_lower,
            "lid_fit_tolerance_mm": 0.2
        }
        
    else:
        # Default to flat plate if unclear
        part_type = "flat_plate"
        params = {
            "length_mm": 100.0,
            "width_mm": 80.0,
            "thickness_mm": 5.0,
            "hole_pattern": "rectangular",
            "hole_count_x": 4,
            "hole_count_y": 3,
            "hole_diameter_mm": 3.2,
            "corner_fillet_mm": 3.0
        }
    
    # Extract material
    material = None
    if "aluminum" in desc_lower or "6061" in desc_lower:
        material = "aluminum_6061"
    elif "steel" in desc_lower:
        material = "steel_1018"
    elif "plastic" in desc_lower or "abs" in desc_lower:
        material = "plastic_abs"
    
    return ParsedParameters(
        part_type=part_type,
        parameters=params,
        material=material,
        confidence=0.85
    )

def parse_with_claude(description: str, api_key: str) -> ParsedParameters:
    """
    Parse using Claude API.
    Requires anthropic package and valid API key.
    """
    try:
        import anthropic
        
        client = anthropic.Anthropic(api_key=api_key)
        
        message = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": description}
            ]
        )
        
        # Parse JSON response
        response_text = message.content[0].text
        # Extract JSON from response (in case Claude adds explanation)
        if "{" in response_text:
            json_start = response_text.find("{")
            json_end = response_text.rfind("}") + 1
            json_str = response_text[json_start:json_end]
            data = json.loads(json_str)
            return ParsedParameters(**data)
        else:
            raise ValueError("No JSON found in response")
            
    except Exception as e:
        print(f"Claude API error: {e}")
        print("Falling back to mock parser")
        return parse_with_mock_llm(description)

def parse_description(description: str, use_claude: bool = False, api_key: str = None) -> ParsedParameters:
    """
    Main parsing function. Uses Claude if available, otherwise mock.
    """
    if use_claude and api_key:
        return parse_with_claude(description, api_key)
    else:
        return parse_with_mock_llm(description)
