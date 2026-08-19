# Contributing

Thanks for considering a contribution to OraclePoly. This document covers code standards and the development process.

## Code standards

### Backend (Python)
- Style: [PEP 8](https://www.python.org/dev/peps/pep-0008/).
- Typing: type hints required for all functions and classes.
- Docs: every public function should have a Google-style docstring.
- Linting: `ruff` (configured in `pyproject.toml`).

### Frontend (React/TypeScript)
- Standard React patterns, functional components with hooks.
- TypeScript strict mode, minimal use of `any`.
- Tailwind CSS for styling.
- Linting: ESLint + Prettier (config at repo root).

## Development process

1. **GitHub Flow.** Branch per feature or fix: `feature/cool-new-thing`, `fix/annoying-bug`.
2. **Commits.** Meaningful messages in English (e.g. `feat: add whale segmentation by topic`).
3. **Pull requests.** Describe the change, make sure tests pass locally, request a review.

## Testing

No feature lands on `main` without tests.

```bash
pytest tests/
```

Add tests for critical logic and components on the frontend where it applies.

## Security

If you find a vulnerability, don't open a public GitHub issue — report it to the maintainers directly.
