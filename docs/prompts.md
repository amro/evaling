# Prompts, templating, and multimodal inputs

A **prompt** is an ordered list of messages — multi-turn is native, single-turn
is just the one-message case. Each variant in `variants:` names one prompt,
defined inline or in an external file.

## Prompt files

An external prompt file is a YAML list of messages:

```yaml
# prompts/concise.yaml
- role: system
  content: Answer in one sentence.
- role: user
  content: "{{ question }}"
```

Reference it from the config with a path relative to the config file:

```yaml
variants:
  - name: concise
    prompt: prompts/concise.yaml
```

## Messages and content parts

Roles are `system`, `user`, and `assistant` (assistant messages let you script
conversation history). A message's `content` is either a plain string —
shorthand for a single text part — or a list of typed parts:

```yaml
- role: user
  content:
    - text: "{{ question }}"
    - image: "{{ files.photo }}"
    - file: manual.pdf
    - audio: recording.mp3
```

| Part | Accepted files |
|---|---|
| `text` | — (Jinja2 template) |
| `image` | `.png` `.jpg` `.jpeg` `.gif` `.webp` |
| `file` | `.pdf` |
| `audio` | `.mp3` `.wav` `.ogg` `.flac` `.m4a` |

Using a part type a model can't accept (e.g. audio on a text-only model) fails
with a clear error rather than being silently dropped.

## Templating

Text parts (and media path expressions) are [Jinja2](https://jinja.palletsprojects.com/)
templates with the full language available — variables, conditionals, loops,
filters:

```yaml
- role: user
  content: |
    {% for example in examples %}
    Example: {{ example }}
    {% endfor %}
    Now answer: {{ question | trim }}
```

The template context for a case:

- Every key in the case's `vars` is a top-level name (`{{ question }}`).
- File attachments are available as `files.<name>` (`{{ files.photo }}`).
- `files` is reserved: a case var named `files` is an error.

Rendering is **strict**: referencing an undefined variable is an error naming
the message it occurred in — typos fail loudly instead of producing empty text.

## Binary inputs

Media parts reference files, never inline binary in templates. The reference is
itself a template, so it can come from case attachments (`{{ files.photo }}`)
or be a literal path (`diagram.png`, resolved relative to the config file).

Files are identified by extension, validated against the part type, and hashed
by content (sha256) — so response caching and run storage recognize the same
file wherever it lives, and renamed-but-identical inputs don't invalidate the
cache.

## Case datasets

Cases live inline in the config or in a CSV/JSONL dataset:

```yaml
cases:
  file: data/cases.jsonl
```

Dataset rows are flat mappings. Reserved keys become case fields: `id`,
`expected`, `human_label`, `files`. Every other key becomes a template
variable. A string value `file://<path>` turns that field into a file
attachment instead:

```jsonl
{"id": "c1", "question": "What breed?", "expected": "Collie", "photo": "file://images/dog1.jpg"}
{"question": "What color?", "photo": "file://images/cat.jpg"}
```

The same conventions apply to CSV columns (all CSV values are strings; empty
`id`/`expected`/`human_label` cells mean "not provided").

Path resolution: attachment paths in a dataset resolve relative to the dataset
file; attachment paths in inline cases resolve relative to the config file.
Cases without an `id` get `case-<position>` (1-based).
