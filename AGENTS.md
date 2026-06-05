# Exordos S3 Agent Guide

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:

- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:

```text
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## Project Structure and Module Organization

```
exordos_s3/
├── agent/          # Universal agent implementations
├── cmd/            # CLI commands (bootstrap, gservice, orch_api, status_api, user_api)
├── common/         # Shared utilities (config, constants, log, permissions, utils)
├── infra/          # Infrastructure components
├── orch_api/       # Orchestration API layer
├── paas/           # Platform-as-a-Service entities
├── services/       # Core service implementations
├── status_api/     # Status monitoring endpoints
├── tests/          # Unit and functional tests
├── user_api/       # User-facing API endpoints
└── migrations/     # Database migrations
```

**Key paths:**
- Source code: `exordos_s3/`
- Unit tests: `exordos_s3/tests/unit/`
- Functional tests: `exordos_s3/tests/functional/`
- Configuration: `exordos_s3/common/config.py`

## Build, Test and Development Commands

**Testing:**
```bash
# Run all tests (unit + functional)
tox

# Run specific test environment
tox -e py312           # Unit tests on Python 3.12
tox -e py312-functional  # Functional tests on Python 3.12

# Run specific test file
tox -e py312 -- exordos_s3/tests/unit/test_config.py

# Code linting
tox -e ruff            # Auto-fix style issues
tox -e ruff-check      # Check style without fixing
tox -e mypy            # Type checking
```

**Development:**
```bash
# Start docs server
tox -e docs            # Live-reload on 0.0.0.0:8181

# Deploy docs
tox -e docs-deploy
```

## Code Style and Naming Conventions

- **Language**: Python 3.10+
- **Formatter**: Ruff (format + linting)
- **Type Checking**: MyPy
- **Naming**: snake_case for functions/variables, PascalCase for classes
- **Tests**: Located in `exordos_core/tests/unit` and `exordos_core/tests/functional`
- **Comments for code**: write on english

## VCS Conventions

### Commit Message Format

```text
<type>(<scope>): <subject>

<body>

<footer>
```

**Example:**

```text
feat(repo): add HTTP server proxy driver

- Implement SimplePythonRepoDriver for file serving
- Add port configuration and error handling
- Include unit tests for driver lifecycle

Closes #123
```

### Pull Request Requirements

- **Title**: Use imperative, present tense: "Add feature", not "Added feature"
- **Description**: Clear summary of changes

## Additional Guidelines

### License

All source files must include Apache 2.0 license header:

```python
#    Copyright 2026 Genesis Corporation.
#    Licensed under the Apache License, Version 2.0 (the "License")
```

## Additional Tips

**Environment Variables:**
- `EXORDOS_*`, `GEN_*` - Project-specific configuration
- `DATABASE_URI` - Required for functional tests
- `NO_PROXY` - Proxy configuration for functional tests

**Dependencies:**
- Managed via `pyproject.toml` and `uv.lock`
- Use `uv` for package management
- Python versions: 3.10-3.14 supported
