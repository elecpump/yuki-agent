# Issue tracker: GitHub

Issues and PRDs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`.
- **Read an issue**: `gh issue view <number> --comments` and include labels.
- **List issues**: use `gh issue list` with suitable state and label filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`.
- **Apply or remove labels**: use `gh issue edit` with `--add-label` or `--remove-label`.
- **Close an issue**: `gh issue close <number> --comment "..."`.

Infer the repository from `git remote -v`; `gh` does this automatically inside the clone.

## Skill terminology

- “Publish to the issue tracker” means creating a GitHub issue.
- “Fetch the relevant ticket” means running `gh issue view <number> --comments`.
