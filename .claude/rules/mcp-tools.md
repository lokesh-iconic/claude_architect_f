---
description: Conventions for MCP tool and server code
paths: ["**/issue_tracker/**/*.py"]
---

# MCP tool conventions

- Raise the SDK's `ToolError`, never a bare exception, from a tool handler.
  Any other exception type is swallowed by the MCP SDK into the generic
  string `"Error executing tool <name>"`, and the structured payload
  (`errorCategory`, `isRetryable`, `remediation`) is lost.
- Describe every argument with `Annotated[<type>, Field(description=...)]`.
  Schemas are generated from the function signature — an argument without a
  `Field` description ships with no description in the tool's schema at all.
- Put validation constraints (`ge`, `le`, `pattern`, `min_length`) on the
  `Field` itself so the SDK rejects bad input before the handler runs, rather
  than checking by hand inside the handler.
- When two tools could plausibly answer the same request, the description
  must name what the tool is *not* for using the request's own vocabulary
  category — never quote the sibling tool's example terms verbatim, which
  dilutes the sibling's own distinguishing words instead of clarifying the
  boundary.
