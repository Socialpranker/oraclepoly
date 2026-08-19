1# Prevention Guide: Maintaining a Clean Codebase

This guide outlines best practices and automated tools to prevent dead code, unused imports, and other code quality issues in the OraclePoly project.

## 🛡️ Automated Checks

### Python (Backend)
We use `vulture` to detect unused code in Python files.

**Running manual check:**
```bash
python scripts/dead_code_audit.py
```

**Configuration:**
- Minimum confidence: 80%
- Whitelist file: `scripts/vulture_whitelist.py` (if needed)

### TypeScript (Frontend)
We use `ts-prune` to detect unused exports in the frontend codebase.

**Running manual check:**
```bash
cd client
npx ts-prune
```

## 🧹 Commit Hygiene

### Pre-commit Hooks
It is recommended to install `pre-commit` to automatically check for issues before every commit.

1. **Install pre-commit:**
   ```bash
   pip install pre-commit
   ```

2. **Install hooks:**
   ```bash
   pre-commit install
   ```

### Review Checklist
Before merging any Pull Request, ensure:
- [ ] No unused imports (check your IDE warnings)
- [ ] No commented-out code blocks
- [ ] No `print()` statements (use `logger` instead)
- [ ] All new functions are used at least once

## 🔍 IDE Setup

### VS Code
Enable the following settings to catch issues early:
- **Python:** "Python › Linting › Pylint Enabled"
- **TypeScript:** "TypeScript › Validate: Enable"
- **Editor:** "Editor › Code Actions On Save -> source.organizeImports"

## 🚫 Common Pitfalls

1. **Wildcard Imports:** Avoid `from module import *`. It makes it hard to detect unused code.
2. **Implicit Usage:** If a function is called dynamically (e.g., via `getattr` or Redis Queue handlers), add a comment `# vulture: ignore` to suppress false positives.
3. **Frontend Components:** If a component is only used in routing (`App.tsx`), `ts-prune` might flag it. Add `// ts-prune-ignore-next` if necessary.
