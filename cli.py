"""
Command-line interface for quick testing.
"""
import sys
import os
import argparse
from cad_generator import CADGenerator

def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Generate CAD from a text description")
    parser.add_argument("description", nargs="+", help="Part description")
    parser.add_argument("--deepseek", action="store_true", help="Use DeepSeek to parse the description")
    parser.add_argument("--api-key", default=os.environ.get("DEEPSEEK_API_KEY"),
                         help="DeepSeek API key (or set DEEPSEEK_API_KEY env var)")
    parser.add_argument("--model", default="deepseek-v4-flash",
                         help="deepseek-v4-flash (default) or deepseek-v4-pro")
    args = parser.parse_args()

    if args.deepseek and not args.api_key:
        print("--deepseek was passed but no API key found "
              "(use --api-key or set DEEPSEEK_API_KEY). Falling back to regex parsing.")

    description = " ".join(args.description)
    
    print(f"\nGenerating CAD from: {description}\n")
    
    generator = CADGenerator()
    result = generator.generate_from_text(
        description,
        use_deepseek=args.deepseek,
        api_key=args.api_key,
        model=args.model,
    )
    
    if result["success"]:
        print("\n✓ Success!")
        print(f"Part type: {result['parameters']['part_type']}")
        print(f"STEP file: {result['step_file']}")
        print(f"STL file: {result['stl_file']}")
        print("\nParameters used:")
        for key, value in result['parameters']['parameters'].items():
            print(f"  {key}: {value}")
    else:
        print(f"\n✗ Failed: {result['error']}")
        sys.exit(1)

if __name__ == "__main__":
    main()
