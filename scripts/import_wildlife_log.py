#!/usr/bin/env python
"""One-time import of the hand-written 2026-season wildlife log into
tags/wildlife.db.

Fidelity model: the FULL markdown goes into `documents` verbatim; structured
rows carry condensed notes plus the qualifiers that matter (individual
confidence, count semantics) and cite the document. When a row seems thin,
the document is the record.

Idempotent-ish: refuses to run if the document is already imported.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB = Path.home() / "trailcam/tags/wildlife.db"
SRC = Path.home() / "trailcam/tags/wildlife-log-source-2026-08-16.md"
MIGRATION = Path(__file__).resolve().parents[1] / "migrations/wildlife/001_wildlife.sql"
DOC = "wildlife-log-source-2026-08-16"

STATIONS = [
    ("Storm Oak", ["CAM1", "CB1"], "tactacam",
     "~1.5 acre clearing off the deck. Spanish clover, mature black oak, young madrone browse. Pink flags mark 07/18 scat piles.", ""),
    ("North Oak", [], "tactacam",
     "North end of hilltop, north of cabin. Cliff edge next to dense cover. On both bear and deer corridors.", "dual-use pinch point; suspected game-trail trailhead"),
    ("Bench", [], "tactacam", "~50-75 yds E of North Oak.", "dual-use, cliff-edge route"),
    ("South Clearing", [], "tactacam",
     "South end, shady = hot-weather thermal refuge.", "multi-species: jackrabbit + bear + buck + doe"),
    ("Crossroads", [], "tactacam", "Between Orchard and North Oak.", ""),
    ("Field of Sticks", [], "tactacam",
     "25 yds W of Crossroads; aimed east over the clearing.", ""),
    ("Orchard", [], "tactacam",
     "~25 yds W of Storm Oak; small valley runs toward North Oak (~50 yds).", ""),
    ("Cabin North", [], "tactacam", "At the cabin, north side.", ""),
    ("Cabin Yard", [], "tactacam", "South side of cabin. 50+ yd open sightline.", ""),
    ("Path", [], "arlo",
     "2K Arlo on the buck corridor between the trailer and North Oak; points south down the path.",
     "spotlight-vs-IR decision pending; night detection unverified"),
    ("Trailer", [], "arlo",
     "Watches the buck entry/exit point in the brush at the trailer. Relocated here 07/25 — captures before then are a DIFFERENT location.", ""),
    ("Garage", [], "arlo", "Open area in front of the cabin garage, ~50m S of Storm Oak.", ""),
    ("Manzanita grove", [], None,
     "1-2 acres at the bottom of the north-end hill. Major summer bear food (berries). NOT huntable (neighbor proximity).",
     "probable bear anchor; food handoff to acorns into deer season"),
    ("Cliff edge", [], None,
     "Steep rim on the north end. Identified 08/08 as an active travel corridor (bear observed traveling it).",
     "two new cameras to be placed here, aimed across the rim"),
    ("deck", [], None, "Cabin deck — direct-visual observation point.", ""),
]

INDIVIDUALS = [
    ("Al", "deer", "2026-07-18",
     "Buck, forked but just barely. The resident; solved the scat mystery.",
     "rack mass vs Ben — only reliable when both in frame or capture is clean",
     "active", "Watched ~4 hrs from deck 07/18. Log rule: ambiguous night IR = 'unidentified buck', never a guess."),
    ("Ben", "deer", "2026-07-20",
     "Buck, bigger rack, more mass/longer tines. Al's bachelor-group partner.",
     "rack mass vs Al", "active",
     "Wider-ranging than Al (8 stations vs 7); more solo appearances. The harder one to pattern."),
    ("Boar", "bear", "2026-06-25",
     "Large, blocky, cinnamon-brown, heavy front, long straight facial profile. Near-weekly since May. Daylight/midday dominant.",
     "color + build; sex unconfirmed. Color alone shown insufficient 08/08.",
     "active", "Confirmed cross-property range: north end, cabin structures, trailer corridor, South Clearing."),
    ("Small bear", "bear", "2026-07-25",
     "Smaller, darker/black; chocolate-brown with tan muzzle in 08/08 color photo. Lean/long-legged, reads young disperser.",
     "size + build; whether 08/05 and 08/08 animals are the same is UNRESOLVED",
     "active", "'Cub' unconfirmed — solo at 21:03 leans yearling/subadult. Safety notes on file (play-structure visit 08/08)."),
    ("Sow", "bear", "2026-07-09",
     "Dark/black adult with at least one cub (cub CONFIRMED on Crossroads video 07/09).",
     "cub presence; classic sow-guarding posture", "active",
     "Legally protected with cub. Only ONE cub clip this season vs plenty in prior years — departure from baseline, watching."),
]

# (date, time, station, species, individual, confidence, count, count_is_visits,
#  source, category, harvest, note)
S = []
def add(*row): S.append(row)

# --- Named bucks -------------------------------------------------------------
add("2026-07-04","22:15","Cabin North","deer","Ben","unconfirmed",2,0,"camera","deer",0,
    "Old footage. Two bucks browsing deerbrush; bigger rack looks like Ben. Earliest deer entry.")
add("2026-07-05","08:42","Garage","deer",None,None,2,0,"camera","deer",0,
    "Old footage. Buck + 1 (possibly Al+Ben, unconfirmed; 2nd may be a doe). Daytime.")
add("2026-07-18",None,"Storm Oak","deer",None,None,1,0,"camera","deer",0,
    "~8 night frames, velvet racks visible; unidentified.")
add("2026-07-18","21:00","deck","deer","Al","confirmed",1,1,"direct_visual","deer",0,
    "~4 hr deck watch to ~04:00. Entered/exited LEFT; fed clover->browse; bedded in the open 15+ min. Unfazed by flashlight/dogs/door.")
add("2026-07-19","00:25","Storm Oak","deer","Al","assumed",1,0,"camera","deer",0,
    "Close-up portrait; same night as deck watch. 70F, E 1.7mph, waxing crescent.")
add("2026-07-20","00:08","Storm Oak","deer","Al","confirmed",2,0,"camera","deer",0,
    "First Ben sighting (with Al). Bachelor group confirmed.")
add("2026-07-20","00:08","Storm Oak","deer","Ben","confirmed",1,0,"camera","deer",0,
    "First Ben capture; heavier rack, behind by the tree.")
add("2026-07-23","23:16","Storm Oak","deer","Al","confirmed",2,1,"camera","deer",0,
    "With Ben, 23:16-05:27, ~6 hrs on station — longest recorded stay. Bright moon, moved anyway.")
add("2026-07-23","23:16","Storm Oak","deer","Ben","confirmed",1,1,"camera","deer",0,
    "With Al, ~6 hr stay (see paired row).")
add("2026-07-25","23:26","Garage","deer","Ben","confirmed",1,0,"camera","deer",0,
    "FIRST Ben without Al. Broadside, alone, ~50m S of Storm Oak. Same night Storm Oak was blank.")
add("2026-07-27","00:01","Storm Oak","deer","Ben","confirmed",1,1,"camera","deer",0,
    "On station until >=03:57. 71F, E 3.3mph, waxing gibbous.")
add("2026-07-27","06:14","Storm Oak","deer","Al","confirmed",1,0,"camera","deer",0,
    "FIRST DAYLIGHT BUCK at Storm Oak. Start of 4-cam morning track. 60F cool morning.")
add("2026-07-27","06:29","Bench","deer","Al","confirmed",1,0,"camera","deer",0,
    "Track leg 2 (Storm Oak 06:14 -> Bench). First daylight multi-cam track on Al.")
add("2026-07-27","06:43","Crossroads","deer","Al","confirmed",2,0,"camera","deer",0,
    "Track leg 3; 06:44 both bucks in frame together, browsing unhurried in daylight.")
add("2026-07-27","06:50","Field of Sticks","deer","Al","confirmed",2,0,"camera","deer",0,
    "Track leg 4 (36 min total). Route bends WEST — wandering browse, not beeline. One movement, 4 captures.")
add("2026-07-27","07:46","Path","deer","Ben","confirmed",1,0,"camera","deer",0,
    "Solo, walking north up the path ~1 hr after pair at Field of Sticks — pair may split as morning wears on.")
add("2026-07-27","21:26","Cabin Yard","deer","Al","confirmed",1,0,"camera","deer",0,
    "Evening, south side — bucks range the cabin structures too.")
add("2026-07-27","22:13","Storm Oak","deer","Al","confirmed",2,0,"camera","deer",0,
    "Al feeding (clean ID); bedded antlered deer center-back very likely Ben BY ELIMINATION. 2nd documented Storm Oak bedding.")
add("2026-07-27","22:13","Storm Oak","deer","Ben","by_elimination",1,0,"camera","deer",0,
    "Bedded, obscured — by elimination, not rack ID.")
add("2026-07-28","01:28","Cabin Yard","deer","Ben","confirmed",1,0,"camera","deer",0,
    "Overnight, south side.")
add("2026-07-30","22:02","Cabin Yard","deer","Al","confirmed",1,0,"camera","deer",0,
    "Back after the 07/28 heat-lull quiet stretch.")
add("2026-07-30","22:04","Cabin Yard","deer","Ben","confirmed",1,0,"camera","deer",0,
    "2 min after Al — pair back together on the cabin-yard round.")
add("2026-07-30","22:49","Storm Oak","deer","Al","confirmed",2,1,"camera","deer",0,
    "Pair ran Cabin Yard -> Storm Oak circuit; stayed until 02:18 (~3.5 hrs). 75F.")
add("2026-07-30","22:49","Storm Oak","deer","Ben","confirmed",1,1,"camera","deer",0,
    "With Al (see paired row).")
add("2026-07-31","06:33","Storm Oak","deer","Al","confirmed",1,0,"camera","deer",0,
    "Best Al portrait yet — reference frame for upper-1/3 fork legal check. 65F sunrise.")
add("2026-07-31","06:37","Trailer","deer","Al","confirmed",1,0,"camera","deer",0,
    "4-min track from Storm Oak, leaving southbound.")
add("2026-07-31","06:38","Orchard","deer","Ben","confirmed",1,0,"camera","deer",0,
    "1 min after Al's Trailer hit, opposite direction — pair SPLIT for the morning exit.")
add("2026-08-01","15:31","South Clearing","deer","Ben","confirmed",1,1,"camera","deer",0,
    "FIRST buck at South Clearing. 93F; stayed ~90 min browsing in shade = thermal refuge, definitive not pass-through.")
add("2026-08-01","19:41","Cabin Yard","deer","Ben","confirmed",1,0,"camera","deer",0,
    "Evening, heading toward the trailer after the South Clearing session.")
add("2026-08-06","00:26","Storm Oak","deer","Al","confirmed",2,1,"camera","deer",0,
    "Pair, 00:26->06:03, ~5h37m — 2nd longest stay. 06:03 exit = EARLIEST legal-light presence (legal ~05:42). 75->71F, pressure 29.95->30.07 rising, SE wind, last quarter, velvet on. Keystone row for the temperature-claim withdrawal.")
add("2026-08-06","00:26","Storm Oak","deer","Ben","confirmed",1,1,"camera","deer",0,
    "With Al (see paired row).")
add("2026-08-10","23:28","Storm Oak","deer","Ben","confirmed",1,0,"camera","deer",0,
    "Solo arrival — first of three consecutive solo-open nights.")
add("2026-08-11","03:11","Storm Oak","deer","Al","confirmed",2,0,"camera","deer",0,
    "Pair together mid-night; still intact, velvet still on as of 08/07.")
add("2026-08-11","05:44","Storm Oak","deer","Ben","confirmed",1,0,"camera","deer",0,
    "Solo at both ends of the night; 23:28->05:44 span 6h16m (discrete captures, not proof of continuous presence). ~1 min short of legal light.")
add("2026-08-11","23:13","Storm Oak","deer","Ben","confirmed",1,0,"camera","deer",0,
    "Third consecutive solo open; arrival creeping earlier (23:28 -> 23:13).")
add("2026-08-12","01:44","Storm Oak","deer","Al","confirmed",1,0,"camera","deer",0,
    "Arrives 2h31m after Ben — pair intact but arriving separately.")
add("2026-08-12","05:26","Storm Oak","deer","Ben","confirmed",1,0,"camera","deer",0,
    "Brackets the night again (23:13->05:26). 05:26 is ~20 min before legal light.")

# --- Bears -------------------------------------------------------------------
add("2026-06-25","12:35","Cabin Yard","bear","Boar","unconfirmed",1,0,"camera","bear",0,
    "Cinnamon, midday, no cub. Earliest bear entry on record.")
add("2026-07-05","04:53","Crossroads","bear",None,None,1,0,"camera","bear",0,"Solo dark bear, no cub.")
add("2026-07-05","23:57","Cabin Yard","bear",None,None,1,0,"camera","bear",0,
    "Solo, HIGH-CONFIDENCE alone (50+ yds clear sightline behind, no trailing cubs).")
add("2026-07-06","06:03","Cabin North","bear",None,None,1,0,"camera","bear",0,
    "Right next to the cabin, north side, dawn. Closest-to-structure sighting.")
add("2026-07-08","03:38","Crossroads","bear",None,None,1,0,"camera","bear",0,"Solo dark bear, overnight.")
add("2026-07-08","20:33","Crossroads","bear",None,None,1,0,"camera","bear",0,
    "Solo dark; markings POSSIBLY differ from the sow (weak read). Multiple dark bears possible.")
add("2026-07-09","21:25","Crossroads","bear","Sow","confirmed",2,0,"camera","bear",0,
    "SOW + CUB, cub confirmed on video. Safety + legal notes in source doc.")
add("2026-07-21","12:04","North Oak","bear","Boar","unconfirmed",1,0,"camera","bear",0,
    "Cinnamon, midday, relaxed; blocky, small ear on broad head — leans mature boar.")
add("2026-07-24","05:47","Bench","bear","Boar","unconfirmed",1,0,"camera","bear",0,
    "First station of multi-cam vector Bench->North Oak (E->W), casual amble pace.")
add("2026-07-24","05:49","North Oak","bear","Boar","unconfirmed",1,0,"camera","bear",0,
    "Clearest body yet; second leg of the vector. One movement, two captures.")
add("2026-07-24","10:38","Bench","bear","Boar","unconfirmed",1,0,"camera","bear",0,
    "Second Bench visit same day — works this corridor more than once a day.")
add("2026-07-25","16:18","South Clearing","bear","Boar","unconfirmed",1,0,"camera","bear",0,
    "First bear at South Clearing. VALIDITY DISPUTED then RESOLVED VALID 08/11 by owner; do not re-raise.")
add("2026-07-25","21:03","Bench","bear","Small bear","unconfirmed",1,0,"camera","bear",0,
    "Second individual — small/dark, solo. 'Cub' unconfirmed; solo at 21:03 leans yearling/subadult.")
add("2026-07-26","10:37","Orchard","bear",None,None,1,0,"camera","bear",0,
    "Reads smaller/darker but not called. First leg of 2-cam track.")
add("2026-07-26","10:38","Path","bear",None,None,1,0,"camera","bear",0,
    "Same bear ~1 min later; circled LEFT, did not trigger Storm Oak. Routes converge on the trailer corridor.")
add("2026-07-31","12:55","Cabin Yard","bear","Boar","confirmed",1,0,"camera","bear",0,
    "Clearest bear photos yet; trailer for scale = genuinely large. BOAR REFERENCE FRAME.")
add("2026-07-31","12:56","Trailer","bear","Boar","confirmed",1,0,"camera","bear",0,
    "Same animal 1 min later, browsing manzanita — the current daylight draw.")
add("2026-08-05","04:37","Path","bear",None,"unconfirmed",1,0,"camera","bear",0,
    "Dark coat CONFIRMED (cinnamon ruled out); large ears, compact — reads young; size is impression only. First-ever night capture on Path; visibly startled by spotlight (flinch, not avoidance).")
add("2026-08-05","04:40","Storm Oak","bear",None,"unconfirmed",1,0,"camera","bear",0,
    "FIRST BEAR EVER AT STORM OAK (buck core area). IR frame, no color; link to 04:37 Path bear rests on time+space only.")
add("2026-08-08","18:24","Bench","bear","Small bear","unconfirmed",1,0,"camera+visual","bear",0,
    "Traveling NORTH along the CLIFF EDGE — owner watched in person. 94F, W 3.8mph. Cliff-edge corridor identified; camera-placement consequence logged.")
add("2026-08-08","18:35","Cabin Yard","bear","Small bear","unconfirmed",1,0,"camera+visual","bear",0,
    "First good color photo: chocolate brown, tan muzzle, lean — young. 30-yd encounter at play structure; textbook handling, no incident. Same-vs-08/05-animal UNRESOLVED.")
add("2026-08-11","03:47","South Clearing","bear",None,None,1,0,"camera","bear",0,
    "Corroborates 07/25 South Clearing entry. Night timing fits the dark-bear cluster; individual unconfirmed.")

# --- Other deer (does) -------------------------------------------------------
add("2026-07-24",None,"deck","deer",None,None,2,0,"direct_visual","deer",0,
    "2 does on Al's left-side entry route; accidentally spooked (visual alert, likely not scented); retreated into left-side timber.")
add("2026-07-25","10:56","North Oak","deer",None,None,4,0,"camera+visual","deer",0,
    "Group of 4: road -> thicket -> path -> past North Oak. First full multi-cam doe-group track. 81F, S 3.3mph.")
add("2026-07-27","10:38","Cabin Yard","deer",None,None,1,0,"camera","deer",0,"Solo doe, mid-morning.")
add("2026-07-30","07:20","Storm Oak","deer",None,None,1,0,"camera","deer",0,
    "Doe using Storm Oak (mostly a buck spot). Same minute as 3 turkeys 07/30 — shared food table.")
add("2026-07-30","07:24","Trailer","deer",None,None,1,0,"camera","deer",0,
    "Same doe 4 min later, entered cover at the buck entry point.")
add("2026-08-11","06:50","South Clearing","deer",None,None,1,0,"camera","deer",0,
    "First doe at South Clearing; station now dual-use (bear 03:47 same night).")
add("2026-08-13","06:17","North Oak","deer",None,None,1,1,"camera","deer",0,
    "FIRST MULTI-CAM DOE TRACK: North Oak 06:17 -> Storm Oak 06:20 -> Bench -> Storm Oak 07:00. Loops, doesn't transit; all daylight. One movement.")

# --- Small game --------------------------------------------------------------
for d,t,st,note in [
    ("2026-07-24","06:04","South Clearing","South Clearing's first-ever hit."),
    ("2026-07-24","10:27","North Oak",""),
    ("2026-07-25","04:30","Storm Oak","Overnight (04:00-05:00); third camera with jackrabbits."),
    ("2026-07-25","13:47","Path","Midday on the corridor; fourth camera."),
    ("2026-07-25","20:31","Trailer","Evening at the buck entry point; fifth camera."),
    ("2026-07-27","05:37","Trailer","Pre-dawn."),
    ("2026-07-27","06:58","South Clearing","Morning."),
    ("2026-07-28","05:43","Trailer","Pre-dawn."),
    ("2026-07-28","07:59","Trailer","Resident jackrabbit working this spot."),
    ("2026-07-31","10:28","South Clearing","Mid-morning."),
    ("2026-08-01","08:42","South Clearing","Morning."),
    ("2026-08-01","08:47","Trailer","Morning."),
    ("2026-08-01","18:50","Trailer","Evening."),
    ("2026-08-02","05:33","Garage","Pre-dawn, cabin-structures area."),
    ("2026-08-05","06:08","Trailer","Direct visual from cabin, first light."),
    ("2026-08-06","08:51","South Clearing","Black ear tips diagnostic. Initially misread as deer (lens-pressed, no scale) — prompted the harvest stalk. hoseID lesson: big bbox = harder classification."),
    ("2026-08-08","06:17","Trailer","Owner sleeping in, no sit. Trailer morning cluster: 05:37/05:43/06:08/06:17 — best sit odds on property."),
    ("2026-08-11","18:52","North Oak","Third evening data point; first evening jackrabbit away from Trailer."),
    ("2026-08-12","16:56","South Clearing","Earliest afternoon jackrabbit on record (~2 hrs before next-earliest); thermal-refuge logic."),
]:
    add(d,t,st,"jackrabbit",None,None,1,0,"direct_visual" if "visual" in note.lower() else "camera","small_game",0,note)
add("2026-08-06","09:20","South Clearing","jackrabbit",None,None,1,0,"direct_visual","small_game",1,
    "FIRST HARVEST OF THE SEASON. Air rifle, ~20 yds, clean kill. Heart+liver shared with daughter; ~2 lb to slow cooker. Carcass-disposal protocol lesson followed 08/08.")
add("2026-08-06","06:30","Storm Oak","skunk",None,None,1,0,"direct_visual","small_game",0,
    "First skunk in the log (3-hr morning sit, ~05:00-08:00; only animal seen). Third mesocarnivore.")

# --- Predators / birds -------------------------------------------------------
add("2026-07-28","08:14","Orchard","coyote",None,None,2,0,"camera","predator",0,
    "FIRST COYOTES in the log; pair, two-cam track Orchard -> Trailer on the shared travel spine.")
add("2026-08-05",None,"deck","gray_fox",None,None,1,0,"direct_visual","predator",0,
    "First gray fox in the log; eyes-on near Storm Oak, no camera frame. Fox-suppresses-jackrabbits hypothesis assessed NOT SUPPORTED.")
add("2026-07-28","09:59","Cabin Yard","turkey",None,None,2,0,"camera","bird",0,
    "First turkeys: 2 hens, unhurried, ~1 hr to South Clearing (10:57). Fall season notes in source doc.")
add("2026-07-30","07:21","Storm Oak","turkey",None,None,3,0,"camera","bird",0,
    "3 hens in the clover clearing, same minute as a doe — shared food table.")

# --- Disturbances ------------------------------------------------------------
add("2026-07-24",None,"deck","human_disturbance",None,None,1,0,"direct_visual","disturbance",0,
    "Spooked 2 does off the deck. Visual alert, likely NOT scented (no stomp/snort/flag). Low cost.")
add("2026-07-01",None,"Cabin Yard","domestic-dog",None,None,1,0,"direct_visual","disturbance",0,
    "RECURRING: neighbor's farm dog transits yard/trailer a couple times weekly. Bucks habituated — ambient background, not pressure. (Date nominal; recurring entry.)")

CLAIMS = [
    ("Heat suppresses/thins deer movement; cool mornings unlock daylight movement (temperature claim 1)",
     "withdrawn", "2026-08-05", "2026-08-07",
     "Built on 3 retrospective points (07/27 60F, 07/31 65F); 08/06-07 warmest morning produced earliest legal-light presence. Reframed by owner: never established, withdrawn for lack of evidence rather than falsified. Blank nights 07/25-26 and 07/28 now UNEXPLAINED. Pre-registered 08/05 test was ruled valid; method lesson: register predictions before data."),
    ("Heat relocates movement to thermal refuge (temperature claim 2)",
     "supported", None, None,
     "Ben, 93F, ~90 min browsing shaded South Clearing 08/01. The only surviving half of the temperature model."),
    ("Wind runs a clean W(day)/E(night) drainage cycle",
     "open", None, None,
     "Weakening: S mid-morning 07/25 + three SE readings across 08/05-08/07. Treat pre-dawn wind as unpredictable for stand planning until more dawn data."),
    ("Barometric pressure predicts movement",
     "open", None, None,
     "Untracked variable; 29.95->30.07 rising during the 08/06-07 early-exit night. On every Reveal frame; start logging it."),
    ("The gray fox is suppressing the jackrabbit population",
     "falsified", None, "2026-08-05",
     "Mass mismatch, omnivorous diet, scale, causal direction (clover flush is bottom-up). Coyote pair is the better candidate; fox plausibly the suppressed party."),
    ("The small-game species is jackrabbit (not brush rabbit)",
     "resolved", None, "2026-08-05",
     "Owner direct visual, repeated close range. CCR T14 s309 applies: year-round, no limits. Do not re-raise."),
    ("The 07/25 16:18 South Clearing bear capture is valid",
     "resolved", None, "2026-08-11",
     "Dispute closed by owner; the Reveal-header-offset correction was wrong and is struck. Independently corroborated 08/11 03:47. Do not re-raise."),
    ("Animals enter/exit the hilltop via the cliff-edge rim rather than walkable routes",
     "open", "2026-08-08", None,
     "Direct observation of a bear traveling the rim 08/08. Best candidate for the missing entry/exit point; if bears use it, deer likely do. New cameras to be placed across the rim."),
    ("Al/Ben behavior differences are patterns rather than noise",
     "open", None, None,
     "Owner null-hypothesis note 08/07: with two bucks over ~3 weeks, much of the 'pattern' may be noise with a story attached. Label confidence honestly at ingest."),
]

OPEN_ITEMS = [
    ("Resolve the 'cub' question: does the 07/25 small bear ever appear with a sow or siblings?", "open"),
    ("Map how many bear individuals total (at least 2, possibly 3+)", "open"),
    ("Confirm boar vs sow on the big cinnamon bear — needs a known-scale frame", "open"),
    ("Confirm no cubs ever for the boar across May-present footage", "open"),
    ("Does the bear weekly rhythm tighten as mast drops (hyperphagia)?", "open"),
    ("Watch for timestamps creeping earlier through September (nocturnal shift signal)", "open"),
    ("Velvet stripping late Aug-Sept: confirm fork in upper 1/3 of beam for legal", "open"),
    ("Bachelor group breakup — which buck holds this ground?", "open"),
    ("Acorn drop on the clearing black oak; which oak the bucks commit to", "open"),
    ("Mast crop assessment: walk the near oaks, check branch weight", "open"),
    ("Correlate captures against temp/wind/pressure/moon — let the data decide", "open"),
    ("DECIDE: Path Arlo spotlight on vs IR-only pre-season", "open"),
    ("Verify Path Arlo night detection with a manual after-dark walk-past", "open"),
    ("Map rough distances between camera stations (turns multi-cam hits into speed/direction)", "open"),
    ("Place both new cameras on the CLIFF EDGE, aimed across the rim line", "open"),
    ("Determine whether the 08/08 small bear is the 08/05 animal or a third individual", "open"),
    ("Log a manual walk-past trigger on each Tactacam at every visit (blank-night detection check)", "open"),
    ("Watch whether Ben keeps out-ranging Al", "open"),
    ("Sit the clearing 3-4 dawns pre-season with a wind checker; log when drainage flips E->W", "open"),
    ("Consider separate AM/PM stand setups; let the wind pick the spot", "open"),
    ("Verify 2026-27 CDFW small-game table + D-3 regs before September/October", "open"),
    ("Ground-truth the suspected top->bottom game trail near North Oak (worn path, tracks, scat, browse)", "open"),
    ("Fix the CB1 location tag", "done"),
    ("Establish whether boar range exceeds the north corridor", "done"),
    ("Resolve the South Clearing bear contradiction", "done"),
]


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.executescript(MIGRATION.read_text())
    if conn.execute("SELECT 1 FROM documents WHERE name=?", (DOC,)).fetchone():
        print("already imported; refusing to double-import")
        return 1
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute("INSERT INTO documents (name, added_at, content) VALUES (?,?,?)",
                 (DOC, now, SRC.read_text()))
    for name, aliases, cam, desc, notes in STATIONS:
        conn.execute("INSERT OR REPLACE INTO stations VALUES (?,?,?,?,?)",
                     (name, json.dumps(aliases), cam, desc, notes))
    for row in INDIVIDUALS:
        conn.execute("INSERT OR REPLACE INTO individuals VALUES (?,?,?,?,?,?,?)", row)
    for (date, time, station, species, indiv, conf, count, civ,
         source, cat, harvest, note) in S:
        conn.execute(
            "INSERT INTO sightings (date, time, station, species, individual,"
            " individual_confidence, count, count_is_visits, source, category,"
            " harvest, notes, doc_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (date, time, station, species, indiv, conf, count, civ, source,
             cat, harvest, note, DOC))
    for claim, status, reg, res, notes in CLAIMS:
        conn.execute("INSERT INTO claims (claim, status, registered_at,"
                     " resolved_at, notes) VALUES (?,?,?,?,?)",
                     (claim, status, reg, res, notes))
    for item, status in OPEN_ITEMS:
        conn.execute("INSERT INTO open_items (item, status, added) VALUES (?,?,?)",
                     (item, status, "2026-08-16"))
    conn.commit()
    print(f"imported: {len(STATIONS)} stations, {len(INDIVIDUALS)} individuals, "
          f"{len(S)} sightings, {len(CLAIMS)} claims, {len(OPEN_ITEMS)} open items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
