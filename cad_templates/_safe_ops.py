"""
Defensive fillet/chamfer helpers, shared by cad_generator.py and any
template that calls .fillet()/.chamfer() directly.

Why this exists: confirmed via a real VPS smoke-test run that OCCT's
fillet operation can fail (`OCP.StdFail.StdFail_NotDone: BRep_API:
command not done`) even at a radius that already passes a numeric
sanity check like "less than half the material thickness" - the
l_bracket case failed at 1.4mm on 3mm-thick material, which the
validator had already deemed safe. Fillet feasibility depends on edge
topology (how edges meet, how close adjacent fillets would be), not
just magnitude, and that's not something worth trying to fully model
analytically here. Instead: retry at progressively smaller radii, and
if it still can't be done, ship the unfilleted/unchamfered geometry
with a warning rather than failing the whole job over a cosmetic edge
treatment.

Catches Exception broadly rather than only OCP.StdFail.StdFail_NotDone
on purpose - OCCT can raise other Standard_Failure subclasses for
different geometric failure modes, and the fallback behavior (keep the
solid as-is) is safe regardless of which one fired.

SECOND FINDING, also from a real VPS run: motor_mount's fillet call
completed with no exception at all, but the resulting STEP file
re-imported with zero solids - OCCT can silently produce degenerate/
empty geometry right at the edge of fillet feasibility, without
raising.

THIRD FINDING: fixing that by checking `result.solids().vals()` was
non-empty was NOT sufficient - re-ran on the VPS and motor_mount still
failed the exact same way. A solid *object* existing in the CadQuery
stack isn't the same as it being a *valid* one; the fillet can produce
something CadQuery still calls "a solid" that's topologically broken,
which only surfaces as "zero solids" once it round-trips through a real
STEP writer/reader (the smoke test's actual check). Fixed by using
CadQuery's own validity checker instead of a solids-count proxy:
`Shape.isValid()`, documented as wrapping OCCT's `BRepCheck_Analyzer`
(source: cadquery/occ_impl/shapes.py and the OCCT BRepCheck_Analyzer
reference docs) - the same mechanism CadQuery itself recommends for
exactly this kind of defect detection. Every solid returned by a
fillet/chamfer attempt is now checked with `.isValid()`, not just
counted.
"""


def _is_valid_and_nonempty(workplane) -> bool:
    """
    Stronger than counting solids: also runs OCCT's own BRepCheck_Analyzer
    validity check (via CadQuery's Shape.isValid(), see module docstring)
    on every solid. A solid that "exists" in the CadQuery stack but fails
    this check is exactly the failure mode that a solids-count-only check
    missed on the VPS - confirmed by the motor_mount case still failing
    after that first, weaker fix.
    """
    try:
        solids = workplane.solids().vals()
        if len(solids) == 0:
            return False
        return all(s.isValid() for s in solids)
    except Exception:
        return False


def safe_fillet(workplane, radius: float, edge_selector: str | None = None,
                 min_radius: float = 0.05, warnings: list | None = None,
                 label: str = "fillet"):
    """
    Attempt workplane.edges([edge_selector]).fillet(radius), retrying at
    half, then a quarter, of the requested radius if OCCT rejects it -
    either by raising, or by silently returning empty/invalid geometry
    (checked explicitly via OCCT's own validity check, since that
    failure mode doesn't raise and a naive solids-count check was
    confirmed insufficient to catch it). Returns the filleted workplane,
    or the original workplane unchanged (with a note appended to
    `warnings`, if provided) if no radius in the retry sequence produces
    valid geometry.
    """
    last_error = None
    for r in (radius, radius * 0.5, radius * 0.25):
        if r < min_radius:
            break
        try:
            edges = workplane.edges(edge_selector) if edge_selector else workplane.edges()
            candidate = edges.fillet(r)
            if not _is_valid_and_nonempty(candidate):
                last_error = RuntimeError(f"produced invalid/empty geometry at {r}mm (no exception raised)")
                continue
            return candidate
        except Exception as e:
            last_error = e
            continue

    if warnings is not None:
        detail = f" ({last_error})" if last_error else ""
        warnings.append(
            f"Requested {radius}mm {label} could not be applied to this "
            f"geometry, even at reduced radii{detail} - shipped without it."
        )
    return workplane


def safe_chamfer(workplane, size: float, edge_selector: str | None = None,
                  min_size: float = 0.05, warnings: list | None = None,
                  label: str = "chamfer"):
    """Same retry-then-degrade behavior as safe_fillet, including the
    OCCT validity check, for chamfer()."""
    last_error = None
    for s in (size, size * 0.5, size * 0.25):
        if s < min_size:
            break
        try:
            edges = workplane.edges(edge_selector) if edge_selector else workplane.edges()
            candidate = edges.chamfer(s)
            if not _is_valid_and_nonempty(candidate):
                last_error = RuntimeError(f"produced invalid/empty geometry at {s}mm (no exception raised)")
                continue
            return candidate
        except Exception as e:
            last_error = e
            continue

    if warnings is not None:
        detail = f" ({last_error})" if last_error else ""
        warnings.append(
            f"Requested {size}mm {label} could not be applied to this "
            f"geometry, even at reduced sizes{detail} - shipped without it."
        )
    return workplane
