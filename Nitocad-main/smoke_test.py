"""
End-to-end smoke test - run this on the VPS after `pip install -r requirements.txt`.

Exercises the REAL pipeline: regex parser -> validator -> CadQuery template
-> OpenCASCADE export -> file on disk. No DeepSeek key needed (defaults to
the regex fallback), so this only tests the geometry engine itself.

Usage:
    python3 smoke_test.py

Exits non-zero if anything fails, so you can wire it into a CI step later.
"""
import os
import sys
import tempfile

# Isolate this run's db + output dir so it doesn't touch real data
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "smoke_test.db")

import db
from cad_generator import CADGenerator

TEST_CASES = [
    "mounting bracket for a 50mm stepper motor, 4 holes, 5mm fillets, 3mm thick",
    "L-bracket 50mm wide, 60mm tall, 40mm deep, 3mm thick, 2 holes per leg",
    "flat plate 100x80mm, 5mm thick, 4x3 hole pattern, 3mm corner fillets",
    "shaft 10mm diameter, 50mm long, 0.5mm chamfer",
    "gear with 20 teeth, module 2, 10mm thick, 5mm bore",
    "box enclosure 100x80x50mm, 3mm walls, with lid",
    "bearing 10mm inner, 20mm outer, 5mm wide",
    "pipe 20mm outer, 2mm wall, 50mm long",
    "hex standoff, 6mm across flats, 3.2mm bore, 12mm long",
    "t-bracket 60mm long, 40mm cap width, 30mm stem height, 4mm thick",
    "cable channel bracket, 60mm long, 20mm wide, 15mm tall, 2mm walls",
    "i-beam structural beam, 200mm long, 100mm tall, 50mm wide, 5mm thick",
    "c-channel structural beam, 200mm long, 100mm tall, 50mm wide, 5mm thick",
    "connecting rod, 120mm center distance, 24mm big end bore, 12mm small end bore",
    "crankshaft with 4 throws, 80mm stroke",
]


def main() -> int:
    db.init_db()
    generator = CADGenerator(output_dir=os.path.join(tempfile.gettempdir(), "smoke_test_output"))

    failures = []

    for i, description in enumerate(TEST_CASES, 1):
        print(f"\n[{i}/{len(TEST_CASES)}] {description}")
        result = generator.generate_from_text(description, user_id="smoke_test")

        if not result["success"]:
            print(f"  FAIL: {result['error']}")
            failures.append(description)
            continue

        step_path = result["step_file"]
        stl_path = result["stl_file"]

        # Real checks, not just "did it return success"
        if not os.path.exists(step_path) or os.path.getsize(step_path) == 0:
            print(f"  FAIL: STEP file missing or empty at {step_path}")
            failures.append(description)
            continue

        if not os.path.exists(stl_path) or os.path.getsize(stl_path) == 0:
            print(f"  FAIL: STL file missing or empty at {stl_path}")
            failures.append(description)
            continue

        # Re-import the STEP file to confirm OpenCASCADE itself considers
        # it valid geometry, not just "a file got written"
        try:
            import cadquery as cq
            reimported = cq.importers.importStep(step_path)
            solid_count = len(reimported.solids().vals())
            if solid_count == 0:
                print("  FAIL: STEP file re-imports but contains zero solids")
                failures.append(description)
                continue
        except Exception as e:
            print(f"  FAIL: STEP file failed to re-import: {e}")
            failures.append(description)
            continue

        job_row = db.get_job(result["job_id"])
        if job_row is None:
            print(f"  FAIL: job {result['job_id']} was not persisted to db")
            failures.append(description)
            continue

        print(f"  OK: part_type={result['parameters']['part_type']}, "
              f"step={os.path.getsize(step_path)}B, "
              f"solids={solid_count}, job persisted")

    print(f"\n{'='*60}")
    print(f"{len(TEST_CASES) - len(failures)}/{len(TEST_CASES)} passed")

    if failures:
        print("\nFailed cases:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("All checks passed - geometry engine, export, and db persistence all verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
