## Merge conflicts to resolve

The pull request conflicts with its base branch, so the base was merged into
the checkout before you started, and the merge is not committed. Resolving
these conflicts is part of your job alongside the findings, and it is the one
place you may change code no finding names.

Each conflicted file is listed below with git's two-letter status. Resolve
every one:

- `UU`, `AA`: the file has conflict markers in diff3 style (`<<<<<<<`,
  `|||||||`, `=======`, `>>>>>>>`). Keep the intent of both sides: the base
  branch's change and the pull request's change. Remove every marker.
- `UD`: the base branch deleted or renamed a file the pull request modified.
  The pull request's version is in the checkout. Find where the code went on
  the base branch, usually a renamed file or type, and carry the pull
  request's change there. Then list the leftover file under "delete" unless
  the base branch's code still needs it.
- `DU`: the pull request deleted a file the base branch modified. Keep the
  deletion unless the base branch's change is needed elsewhere, in which case
  carry it over. List the file under "delete" to keep it deleted.
- `AU`, `UA`, `DD`: decide from both sides' intent, and say what you chose.

Report each conflict in "applied" with its path, line 0, and one line on how
you resolved it. If a conflict needs a judgment you are not confident in,
still resolve it as best you can and say so in "change", because unresolved
markers stop the whole fix from being pushed.
