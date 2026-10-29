Decide what this Slack message should do in Jira.

intent is "create" only when the message describes a specific piece of
work or a reproducible defect that does not already have a ticket.

issue_type is "Defect" for something broken, "Enhancement" for an
improvement to existing behavior, and "Task" for everything else.
intent is "comment" only when the message names an existing issue key
and adds information to it. Everything else, including questions,
opinions, status updates, and jokes, is "ignore".

summary is one line under 100 characters, written as a task.
description restates the message as a ticket body and quotes the
original text. Never invent detail the message does not contain.

confidence is how sure you are the author wanted a ticket, not how well
you understood the words.

Message from {{ trigger_payload.user }}:
{{ trigger_payload.text }}
