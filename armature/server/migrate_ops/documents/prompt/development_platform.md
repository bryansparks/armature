Decide which code bases this Jira issue needs a change in: the Android
app (android), the iOS app (ios), and the mobile-api-nodejs backend
both apps call (backend). One issue can need more than one, such as a
new API field that both apps display.

If prepare.ready is false, answer an empty list with the reason
"not ready".

prepare.component_platforms lists the platforms the issue's Jira
components name. Treat it as strong evidence: include each of them
unless the text plainly says that part needs no change.

Otherwise judge from the summary, description, and labels. Platform
names, Kotlin, Compose, Gradle, Swift, SwiftUI, Xcode, or a screen
that exists in only one app are evidence for an app. An endpoint,
response field, API route, service call, Node, TypeScript, or "the
API" or "the backend" is evidence for backend. Include a platform only
when its code must change, not because it is mentioned. Work starts
without a person confirming, so when the evidence is weak, pick the
likelier code base anyway and say so in the reason. Answer an empty
list only when prepare.ready is false. List the code base that has to
change first, such as backend before the apps that use what it adds.
Give the deciding evidence as the reason, in under 20 words.

{{ prepare }}
