import pytest

cq = pytest.importorskip("cadquery")

from cad_templates.freeform import generate_freeform, generate_revolved_part  # noqa: E402


def test_revolve_uses_revolve_angle_deg():
    shape = generate_revolved_part({
        "profile_points": [(0, 0), (10, 0), (10, 20), (0, 20)],
        "revolve_axis": "Y", "revolve_angle_deg": 180,
    })
    assert shape.val().Volume() > 0


def test_repeated_closing_point_is_tolerated():
    shape = generate_freeform({"operation": "sweep",
                               "profile_points": [[0, 0], [30, 0], [30, 50], [0, 0]],
                               "path_points": [[0, 0, 0], [0, 0, 5]]})
    assert shape.val().Volume() == pytest.approx(30 * 50 / 2 * 5, rel=1e-3)


def test_sweep_with_y_in_path_is_refused():
    with pytest.raises(ValueError):
        generate_freeform({"operation": "sweep",
                           "profile_points": [[0, 0], [5, 0], [5, 5], [0, 5]],
                           "path_points": [[0, 0, 0], [0, 10, 5]]})


def test_loft_builds_from_profiles():
    shape = generate_freeform({"operation": "loft", "profiles": [
        {"points": [[-10, -10], [10, -10], [10, 10], [-10, 10]], "z": 0},
        {"points": [[-5, -5], [5, -5], [5, 5], [-5, 5]], "z": 20}]})
    assert shape.val().Volume() > 0
