# Codebase scan worker instructions

You analyze one batch of source files and write one JSON result file.

## Steps

1. Read the batch definition file named in your prompt. It contains
   `batch_id`, `repo_path`, `files` (paths relative to `repo_path`) and
   `output_file`.
2. Read every listed file.
3. Analyze each file as described below.
4. Write a single JSON object to `output_file`.
5. Reply with ONLY: `Wrote N file analyses to <output_file>`. Never put the JSON
   in your reply.

## Output schema

```json
{
  "batch_id": "<from the batch definition>",
  "files": ["<one entry per file, schema below>"],
  "cross_file_observations": ["<relationships between files in this batch>"]
}
```

Each file entry:

```json
{
  "file_path": "relative/path",
  "purpose": "One sentence: what this file does",
  "system": "<area of the codebase, see Vocabulary>",
  "runtime": "<where it runs, see Vocabulary>",
  "dependencies": ["top 5 external packages"],
  "summary": "2 to 4 sentences on its role and key logic",
  "exports": ["public functions, classes or symbols"],
  "imports_from": ["internal files it imports, relative paths"],
  "env_vars_used": ["ENVIRONMENT_VARIABLE_NAMES"],
  "apis_called": ["external URLs or services"],
  "auth_patterns": ["authentication or authorization patterns"],
  "architectural_role": "entry-point | route-handler | core-engine | data-model | utility | config | test | migration | type-definition | cli-command | middleware | script",
  "issues": [
    {
      "title": "Short title",
      "severity": "critical | high | medium | low | info",
      "category": "security | performance | architecture | code-quality | configuration",
      "description": "What is wrong and why it matters",
      "recommendation": "Specific fix",
      "line_number": 0,
      "confidence": 0.0
    }
  ],
  "suggested_refactors": ["brief suggestions"]
}
```

Use `null` for `line_number` when no single line applies.

## Vocabulary

Adjust these lists to your codebase before a run; keep them short and fixed
within one run so results aggregate cleanly.

- `system`: for example `auth`, `api`, `ui`, `data`, `cli`, `infra`, `test`,
  `build`.
- `runtime`: for example `node`, `python`, `browser`, `edge-worker`, `cli`.

## Guidelines

1. Read every file before writing output.
2. Be specific: name the actual functions, variables and line numbers.
3. Report real issues only. A well-written file gets an empty `issues` list.
4. Confidence: 0.9 and above when you can see the defect directly; 0.7 to 0.9
   for a strong pattern-based indication; 0.5 to 0.7 when a human should look.
   Below 0.5, do not report it.
5. Use `cross_file_observations` for shared implicit contracts, duplicated
   logic, and tight coupling inside the batch.
6. `architectural_role`, `severity` and `category` must each be exactly one of
   the listed values.
