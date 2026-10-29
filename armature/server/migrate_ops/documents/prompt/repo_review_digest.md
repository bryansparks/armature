Write the Slack post for this morning's review.

Output the message text and nothing else. No JSON, no object, no code
fence, no preamble, no field names. The first character of your response
is the first character of the Slack message. Whatever you emit is posted
verbatim.

Slack mrkdwn, no headings, under 40 lines.

Open with coverage: what was reviewed and anything that went unreviewed.
An engineer skimming at 9am must not read a missing review as a clean
one.

Lead with the count of high-severity changes. One line per change: repo,
file, what to do. Note any conflict the lead resolved in one line each,
because that is where the judgment was. Close with a single line naming
what was reviewed and what was clean.

If the lead decided nothing needs to change, say that in one line and
stop.

{{ team_lead }}
