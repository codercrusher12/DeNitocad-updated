# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
make install-dev        # installs runtime + dev deps, sets up pre-commit
cp .env.example .env    # fill in what you need; everything has a sane default
```

CadQuery's `OCP` dependency is a large native wheel stack (see
`Dockerfile`'s comments on `libgl1`/etc). If you only need to touch the
parser, validator, or API layer, `pip install -r requirements.txt`
minus `cadquery` still gets you a working dev loop — `make test-fast`
skips everything that needs real geometry.

## Before pushing

```bash
make check   # lint + typecheck + fast tests, same as CI's first three jobs
```

If you touched anything in `cad_templates/`, `validator.py`,
`assembly.py`, or `cad_generator.py`'s CadQuery-calling paths, also run:

```bash
make test    # full suite, needs a real CadQuery install
python smoke_test.py
```

## Project conventions

- **Config**: add new environment variables to `config.py`'s `Settings`
  class, not a bare `os.getenv()` call somewhere else. See that file's
  module docstring for why.
- **Logging**: `from logging_config import get_logger; logger = get_logger(__name__)`,
  not `print()`. Use `extra={...}` for structured fields you want
  queryable in JSON log output, not string interpolation.
- **Errors**: raise a specific subclass of `NitocadError` (see
  `exceptions.py`) for anything client-input-related (422) vs.
  internal-failure-related (500), rather than a bare `Exception` or
  `HTTPException` inline in a route.
- **Tests**: unit tests that don't need CadQuery go in the main test
  files and run everywhere. Anything that calls into `cadquery` directly
  (or through a template/`CADGenerator`) belongs in
  `tests/test_cad_templates.py` and should be marked implicitly by
  living in that file (it's marked `requires_cadquery` at module level).
- **New CAD template**: per the README's existing "Extending" section —
  add the generator function, register it in `cad_templates/__init__.py`,
  add a validator in `validator.py`, wire keyword detection into
  `smart_parser.py`'s `infer_part_type`, and add it to the parametrized
  list in `tests/test_cad_templates.py::TestAllTemplatesProduceValidGeometry`
  (it's automatic — that test iterates `TEMPLATES.keys()`).

## Commit style

No enforced convention, but a commit message that says *why* (not just
what) is worth the extra sentence — this codebase's own commit/changelog
style (see `CHANGELOG.md`, `README.md`) leans heavily on explaining
reasoning, not just describing the diff. Keep that up.
