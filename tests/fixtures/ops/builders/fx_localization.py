import json
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import os

from fx_common import write

D = __file__.rsplit("/", 2)[0] + "/localization"
os.makedirs(D, exist_ok=True)

GITCONFIG = "[user]\n\tname = Armature\n\temail = armature@armature-ops.invalid\n"
# Fixed commit dates, so the commit the draft publish makes, and its patch, are the same every capture.
DATES = {"GIT_AUTHOR_DATE": "2026-10-28 09:30:00 -0600", "GIT_COMMITTER_DATE": "2026-10-28 09:30:00 -0600"}


def fixture(desc, repo_path, slug, number, platform, repo, pr, fakes):
    return {"description": desc, "source": "generic", "env": dict(DATES),
            "files": {".gitconfig": GITCONFIG,
                      f"state/l10n-jobs/{slug.replace('/', '_')}-{number}.json":
                          {"repo": slug, "number": number, "platform": platform}},
            "repos": {repo_path: repo},
            "context": {"job_file": "{{home}}/state/l10n-jobs/" + slug.replace("/", "_") + "-" + str(number) + ".json"},
            "fakes": {"gh": [{"match": f"^pr view {number} --repo {slug} --json state,title,headRefName,headRefOid,baseRefName,isCrossRepository",
                              "stdout": pr}] + fakes.get("gh", []),
                      "claude": fakes.get("claude", [])},
            "stubs": {}}


# ---------------------------------------------------------------- Android: android-tree
A = "app/src/main/res"
A_REPO = "state/repos/fs-eng_android-tree"
A_BRANCH = "TREE-3517-memories-sort"
KT = "app/src/main/java/org/familysearch/mobile/tree/memories"

def xml(body):
    return ('<?xml version="1.0" encoding="utf-8"?>\n<resources xmlns:tools="http://schemas.android.com/tools">\n'
            + body + "</resources>\n")

SRC_BASE = xml(
    '    <string name="app_name" translatable="false">FamilySearch Tree</string>\n'
    '    <string name="memories_title">Memories</string>\n'
    '    <!-- Shown on a person\'s Memories tab when nothing is attached to them. -->\n'
    '    <string name="memories_empty">No memories yet</string>\n'
    '    <!-- Banner that pointed people to the 2024 gallery redesign. -->\n'
    '    <string name="memories_gallery_moved">Memories now open in the new gallery.</string>\n'
    '    <string name="memories_add">Add Memory</string>\n')
SRC_HEAD = xml(
    '    <string name="app_name" translatable="false">FamilySearch Tree</string>\n'
    '    <string name="memories_title">Memories</string>\n'
    '    <!-- Shown on a person\'s Memories tab when nothing is attached to them. -->\n'
    '    <string name="memories_empty">No memories yet. Add a photo, story, or document to start.</string>\n'
    '    <string name="memories_add">Add Memory</string>\n'
    '    <!-- Label above the sort options in the memories list. -->\n'
    '    <string name="memories_sort_label">Sort by</string>\n'
    '    <string name="memories_sort_newest">Newest first</string>\n'
    '    <string name="memories_sort_oldest">Oldest first</string>\n'
    '    <!-- Under a person\'s name in the memories list. %d is how many memories they have. -->\n'
    '    <plurals name="memories_count">\n'
    '        <item quantity="one">%d memory</item>\n'
    '        <item quantity="other">%d memories</item>\n'
    '    </plurals>\n')

LANG = {
    "de": {"title": "Erinnerungen", "empty": "Noch keine Erinnerungen", "moved": "Erinnerungen werden jetzt in der neuen Galerie geöffnet.",
           "add": "Erinnerung hinzufügen", "label": "Sortieren nach", "newest": "Neueste zuerst", "oldest": "Älteste zuerst",
           "count": [("one", "%d Erinnerung"), ("other", "%d Erinnerungen")]},
    "es": {"title": "Recuerdos", "empty": "Todavía no hay recuerdos", "moved": "Los recuerdos ahora se abren en la nueva galería.",
           "add": "Agregar recuerdo", "label": "Ordenar por", "newest": "Más recientes primero", "oldest": "Más antiguos primero",
           "count": [("one", "%d recuerdo"), ("many", "%d de recuerdos"), ("other", "%d recuerdos")]},
    "fr": {"title": "Souvenirs", "empty": "Aucun souvenir pour l\\'instant", "moved": "Les souvenirs s\\'ouvrent désormais dans la nouvelle galerie.",
           "add": "Ajouter un souvenir", "label": "Trier par", "newest": "Plus récents d\\'abord", "oldest": "Plus anciens d\\'abord",
           "count": [("one", "%d souvenir"), ("many", "%d de souvenirs"), ("other", "%d souvenirs")]},
    "ja": {"title": "思い出", "empty": "思い出はまだありません", "moved": "思い出は新しいギャラリーで開くようになりました。",
           "add": "思い出を追加", "label": "並べ替え", "newest": "新しい順", "oldest": "古い順",
           "count": [("other", "%d件の思い出")]},
}

def lang_base(t):
    return xml(f'    <string name="memories_title">{t["title"]}</string>\n'
               f'    <string name="memories_empty">{t["empty"]}</string>\n'
               f'    <string name="memories_gallery_moved">{t["moved"]}</string>\n'
               f'    <string name="memories_add">{t["add"]}</string>\n')

def lang_head(t):
    items = "".join(f'        <item quantity="{q}">{v}</item>\n' for q, v in t["count"])
    return xml(f'    <string name="memories_title">{t["title"]}</string>\n'
               f'    <string name="memories_empty">{t["empty"]}</string>\n'
               f'    <string name="memories_gallery_moved">{t["moved"]}</string>\n'
               f'    <string name="memories_add">{t["add"]}</string>\n'
               f'    <string name="memories_sort_label">{t["label"]}</string>\n'
               f'    <string name="memories_sort_newest">{t["newest"]}</string>\n'
               f'    <string name="memories_sort_oldest">{t["oldest"]}</string>\n'
               f'    <plurals name="memories_count">\n{items}    </plurals>\n')

BANNER_KT = ("package org.familysearch.mobile.tree.memories\n\n"
             "import androidx.compose.material3.MaterialTheme\nimport androidx.compose.material3.Text\n"
             "import androidx.compose.runtime.Composable\nimport androidx.compose.ui.res.stringResource\n"
             "import org.familysearch.mobile.tree.R\n\n"
             "@Composable\nfun MemoriesGalleryBanner() {\n"
             "    Text(stringResource(R.string.memories_gallery_moved), style = MaterialTheme.typography.bodyMedium)\n}\n")
LIST_BASE = ("package org.familysearch.mobile.tree.memories\n\n"
             "import androidx.compose.foundation.lazy.LazyColumn\nimport androidx.compose.foundation.lazy.items\n"
             "import androidx.compose.material3.MaterialTheme\nimport androidx.compose.material3.Text\n"
             "import androidx.compose.runtime.Composable\nimport androidx.compose.ui.res.stringResource\n"
             "import org.familysearch.mobile.tree.R\n\n"
             "@Composable\nfun MemoriesList(memories: List<Memory>, onOpen: (Memory) -> Unit) {\n"
             "    LazyColumn {\n        item { MemoriesGalleryBanner() }\n"
             "        items(memories, key = { it.id }) { MemoryRow(it, onOpen) }\n    }\n}\n\n" + BANNER_KT.split("\n\n", 2)[2])
LIST_HEAD = ("package org.familysearch.mobile.tree.memories\n\n"
             "import androidx.compose.foundation.lazy.LazyColumn\nimport androidx.compose.foundation.lazy.items\n"
             "import androidx.compose.material3.Text\nimport androidx.compose.runtime.Composable\n"
             "import androidx.compose.ui.res.pluralStringResource\nimport org.familysearch.mobile.tree.R\n\n"
             "@Composable\nfun MemoriesList(memories: List<Memory>, sort: MemorySort, onSort: (MemorySort) -> Unit, onOpen: (Memory) -> Unit) {\n"
             "    LazyColumn {\n"
             "        item { Text(pluralStringResource(R.plurals.memories_count, memories.size, memories.size)) }\n"
             "        item { MemoriesSortMenu(sort, onSort) }\n"
             "        items(memories.sortedWith(sort.comparator), key = { it.id }) { MemoryRow(it, onOpen) }\n    }\n}\n")
SORT_KT = ("package org.familysearch.mobile.tree.memories\n\n"
           "import androidx.compose.material3.DropdownMenuItem\nimport androidx.compose.material3.Text\n"
           "import androidx.compose.runtime.Composable\nimport androidx.compose.ui.res.stringResource\n"
           "import org.familysearch.mobile.tree.R\n\n"
           "enum class MemorySort(val comparator: Comparator<Memory>) {\n"
           "    NEWEST(compareByDescending { it.created }),\n    OLDEST(compareBy { it.created }),\n}\n\n"
           "@Composable\nfun MemoriesSortMenu(sort: MemorySort, onSort: (MemorySort) -> Unit) {\n"
           "    Text(stringResource(R.string.memories_sort_label))\n"
           "    DropdownMenuItem(text = { Text(stringResource(R.string.memories_sort_newest)) }, onClick = { onSort(MemorySort.NEWEST) })\n"
           "    DropdownMenuItem(text = { Text(stringResource(R.string.memories_sort_oldest)) }, onClick = { onSort(MemorySort.OLDEST) })\n}\n")

def android_repo(head_files, message):
    base = {f"{A}/values/strings.xml": SRC_BASE, f"{KT}/MemoriesList.kt": LIST_BASE}
    base.update({f"{A}/values-{c}/strings.xml": lang_base(t) for c, t in LANG.items()})
    return {"base": "dev", "branch": A_BRANCH, "remote": "fs-eng/android-tree", "files": base,
            "commits": [{"message": message, "files": head_files}]}

A_HEAD = f"{{{{rev:{A_REPO}:{A_BRANCH}}}}}"

def a_pr(title, branch=A_BRANCH, head=A_HEAD):
    return {"state": "OPEN", "title": title, "headRefName": branch, "headRefOid": head, "baseRefName": "dev",
            "isCrossRepository": False}

# The sessions' replies. A canned session cannot edit files, so the pull request's head already holds
# the translations they report writing for the added keys. memories_empty keeps its old translation.
def l10n_report(code):
    f = f"{A}/values-{code}/strings.xml"
    keys = ["memories_empty", "memories_sort_label", "memories_sort_newest", "memories_sort_oldest", "memories_count"]
    return json.dumps({"written": [{"file": f, "key": k} for k in keys], "skipped": []}, ensure_ascii=False)

head_files = {f"{A}/values/strings.xml": SRC_HEAD, f"{KT}/MemoriesList.kt": LIST_HEAD, f"{KT}/MemoriesSortMenu.kt": SORT_KT}
head_files.update({f"{A}/values-{c}/strings.xml": lang_head(t) for c, t in LANG.items()})
repo = android_repo(head_files, "TREE-3517 Sort memories by date and show a count")

fx = fixture("An android-tree pull request adds three strings and a plurals entry, rewords one, and drops the "
             "gallery banner's string. One translate session runs per language (de, es, fr, ja). A canned session "
             "cannot write files, so the head already holds the translations of the added keys and keeps the old "
             "translation of the reworded one, which check counts as not written. Prune deletes the dropped key "
             "from every language file, and publish commits the result as a draft patch.",
             A_REPO, "fs-eng/android-tree", 6214, "android", repo,
             a_pr("TREE-3517 Sort memories by date and show a count"),
             {"claude": [{"match": f"Language: {c}\\n", "result": l10n_report(c)} for c in LANG]})
write(f"{D}/android-new-strings.json", fx)

# ---------------------------------------------------------------- nothing to translate
N_BRANCH = "TREE-3530-memories-row-padding"
ROW_BASE = ("package org.familysearch.mobile.tree.memories\n\n"
            "import androidx.compose.foundation.layout.padding\nimport androidx.compose.ui.Modifier\nimport androidx.compose.ui.unit.dp\n\n"
            "val MemoryRowPadding = Modifier.padding(horizontal = 16.dp, vertical = 12.dp)\n")
ROW_HEAD = ROW_BASE.replace("vertical = 12.dp", "vertical = 8.dp")
nrepo = {"base": "dev", "branch": N_BRANCH, "remote": "fs-eng/android-tree",
         "files": {f"{A}/values/strings.xml": SRC_BASE, f"{KT}/MemoryRowPadding.kt": ROW_BASE,
                   **{f"{A}/values-{c}/strings.xml": lang_base(t) for c, t in LANG.items()}},
         "commits": [{"message": "TREE-3530 Tighten memory row padding",
                      "files": {f"{KT}/MemoryRowPadding.kt": ROW_HEAD,
                                f"{A}/values/strings.xml": SRC_BASE.replace(
                                    "FamilySearch Tree</string>", "FamilySearch Tree</string>\n"
                                    '    <string name="memories_debug_tag" translatable="false">MemoriesList</string>')}}]}
N_HEAD = f"{{{{rev:{A_REPO}:{N_BRANCH}}}}}"
nfx = fixture("An android-tree pull request changes only Kotlin and adds one translatable=\"false\" string, so "
              "detect finds nothing to translate or remove, no session starts, and publish finds an empty diff.",
              A_REPO, "fs-eng/android-tree", 6230, "android", nrepo,
              a_pr("TREE-3530 Tighten memory row padding", N_BRANCH, N_HEAD), {})
write(f"{D}/nothing-to-translate.json", nfx)

# ---------------------------------------------------------------- iOS: mobile-ios
I_REPO = "state/repos/fs-eng_mobile-ios"
I_BRANCH = "TEMPLE-2241-reservation-reminders"
R = "shared/TempleUI/Sources/TempleUI/Resources"
SWIFT = "shared/TempleUI/Sources/TempleUI/Reservations"

def strings(entries):
    return "".join((f"/* {c} */\n" if c else "") + f'"{k}" = "{v}";\n\n' for k, v, c in entries).rstrip("\n") + "\n"

I_SRC_BASE = [("temple.reservation.title", "Reservation", "Title of the reservation details screen."),
              ("temple.reservation.cancel", "Cancel Reservation", None),
              ("Tap to see your past reservations", "Tap to see your past reservations", "Footer under the upcoming reservations list."),
              ("temple.reservation.legacyBadge", "Legacy", "Badge on reservations made before 2023.")]
I_SRC_HEAD = [I_SRC_BASE[0], I_SRC_BASE[1],
              ("temple.reservation.reminder.toggle", "Remind Me", "Switch label on the reservation details screen."),
              ("temple.reservation.reminder.body", "Your %1$@ appointment at %2$@ starts in %3$d minutes.",
               "Notification body. %1$@ is the ordinance, %2$@ the temple name, %3$d minutes until it starts.")]
I_LANG = {
    "de": ["Reservierung", "Reservierung stornieren", "Tippe, um deine früheren Reservierungen zu sehen", "Alt",
           "Erinnern", "Dein Termin für %1$@ im %2$@ beginnt in %3$d Minuten."],
    "es": ["Reservación", "Cancelar reservación", "Toca para ver tus reservaciones anteriores", "Antigua",
           "Recordarme", "Tu cita de %1$@ en el %2$@ comienza en %3$d minutos."],
    "fr": ["Réservation", "Annuler la réservation", "Touchez pour voir vos réservations passées", "Ancienne",
           "Me rappeler", "Votre rendez-vous pour %1$@ au %2$@ commence dans %3$d minutes."],
}
def ilang(t, head):
    rows = [("temple.reservation.title", t[0], None), ("temple.reservation.cancel", t[1], None),
            ("Tap to see your past reservations", t[2], None), ("temple.reservation.legacyBadge", t[3], None)]
    if head:
        rows += [("temple.reservation.reminder.toggle", t[4], None), ("temple.reservation.reminder.body", t[5], None)]
    return strings(rows)

CARD_BASE = ("import SwiftUI\n\nstruct ReservationDetailsView: View {\n    let reservation: Reservation\n\n"
             "    var body: some View {\n        List {\n            ReservationSummary(reservation: reservation)\n"
             "            if reservation.isLegacy {\n                StatusPill(key: badgeKey(for: reservation))\n            }\n"
             "            Button(String(localized: \"temple.reservation.cancel\", bundle: .module), role: .destructive) {}\n"
             "        }\n        .navigationTitle(String(localized: \"temple.reservation.title\", bundle: .module))\n    }\n\n"
             "    private func badgeKey(for reservation: Reservation) -> String {\n        \"temple.reservation.\\(reservation.badge)Badge\"\n    }\n}\n")
CARD_HEAD = CARD_BASE.replace(
    "            Button(String(localized: \"temple.reservation.cancel\"",
    "            Toggle(String(localized: \"temple.reservation.reminder.toggle\", bundle: .module), isOn: $remind)\n"
    "            Button(String(localized: \"temple.reservation.cancel\"").replace(
    "    let reservation: Reservation\n", "    let reservation: Reservation\n    @State private var remind = false\n")
HISTORY_BASE = ("import SwiftUI\n\nstruct UpcomingReservationsFooter: View {\n    var body: some View {\n"
                "        Text(\"Tap to see your past reservations\", bundle: .module)\n            .font(.footnote)\n    }\n}\n")
HISTORY_HEAD = ("import SwiftUI\n\nstruct UpcomingReservationsFooter: View {\n    var body: some View {\n"
                "        NavigationLink(value: TempleRoute.pastReservations) {\n"
                "            Label(String(localized: \"temple.reservation.title\", bundle: .module), systemImage: \"clock.arrow.circlepath\")\n"
                "        }\n    }\n}\n")
REMINDER = ("import UserNotifications\n\nenum ReservationReminder {\n"
            "    static func content(for reservation: Reservation) -> UNMutableNotificationContent {\n"
            "        let content = UNMutableNotificationContent()\n"
            "        let format = String(localized: \"temple.reservation.reminder.body\", bundle: .module)\n"
            "        content.body = String(format: format, reservation.ordinanceName, reservation.templeName, 30)\n"
            "        return content\n    }\n}\n")

ibase = {f"{R}/Base.lproj/Localizable.strings": strings(I_SRC_BASE), f"{SWIFT}/ReservationDetailsView.swift": CARD_BASE,
         f"{SWIFT}/UpcomingReservationsFooter.swift": HISTORY_BASE,
         f"{R}/en.lproj/Localizable.strings": strings([(k, v, None) for k, v, _ in I_SRC_BASE])}
ibase.update({f"{R}/{c}.lproj/Localizable.strings": ilang(t, False) for c, t in I_LANG.items()})
ihead = {f"{R}/Base.lproj/Localizable.strings": strings(I_SRC_HEAD), f"{SWIFT}/ReservationDetailsView.swift": CARD_HEAD,
         f"{SWIFT}/ReservationReminder.swift": REMINDER, f"{SWIFT}/UpcomingReservationsFooter.swift": HISTORY_HEAD,
         f"{R}/en.lproj/Localizable.strings": strings([(k, v, None) for k, v, _ in I_SRC_HEAD])}
ihead.update({f"{R}/{c}.lproj/Localizable.strings": ilang(t, True) for c, t in I_LANG.items()})
irepo = {"base": "develop", "branch": I_BRANCH, "remote": "fs-eng/mobile-ios", "files": ibase,
         "commits": [{"message": "TEMPLE-2241 Add a reservation reminder", "files": ihead}]}
I_HEAD = f"{{{{rev:{I_REPO}:{I_BRANCH}}}}}"

def i_report(code):
    f = f"{R}/{code}.lproj/Localizable.strings"
    return ("Read ReservationDetailsView.swift and ReservationReminder.swift: the toggle is a short switch label and the "
            "body is a notification with the ordinance, the temple, and the minutes.\n\n"
            + json.dumps({"written": [{"file": f, "key": "temple.reservation.reminder.toggle"},
                                      {"file": f, "key": "temple.reservation.reminder.body"}], "skipped": []}))

ifx = fixture("A mobile-ios pull request adds a reminder toggle and a notification body with numbered placeholders, "
              "and drops two keys. One translate session runs per language (de, es, fr), and en.lproj and Base are "
              "never translated. The head already holds the translations the sessions report, since a canned "
              "session cannot write files. Prune deletes the footer key nothing uses and keeps "
              "temple.reservation.legacyBadge, which code builds at runtime, and publish commits the removal as a "
              "draft patch.",
              I_REPO, "fs-eng/mobile-ios", 8873, "ios", irepo,
              {"state": "OPEN", "title": "TEMPLE-2241 Add a reservation reminder", "headRefName": I_BRANCH,
               "headRefOid": I_HEAD, "baseRefName": "develop", "isCrossRepository": False},
              {"claude": [{"match": f"Language: {c}\\n", "result": i_report(c)} for c in I_LANG]})
write(f"{D}/ios-reminder-strings.json", ifx)
