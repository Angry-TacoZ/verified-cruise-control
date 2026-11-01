# RSA ADAS reference (Milestone 1 deliverable)

Structured general knowledge extracted from the Road Safety Authority
"Advanced Driver Assistance Systems (ADAS)" guide, September 2026
(21 pages; page numbers below are physical PDF pages — printed
footers read one lower). Machine-readable form:
`backend/audits/rsa_reference.json` (version `2026-09-rsa-v1`),
loader + guidance accessor: `backend/audits/rsa_reference.py`.

Scope: **general knowledge only**. It is not manufacturer equipment
data and never overrides `backend/audits/adas_catalog_v1.json`.
Vehicle-specific conclusions always require applicable manufacturer
documentation; unknown stays unknown. Definitions below are concise
paraphrases; no illustrations are reproduced.

## Cruise family (guide §6.1, p. 13)

- **Conventional cruise** keeps a set speed (throttle; sometimes
  engine braking/brakes on descents). Telltales: speedometer dial +
  needle + arrow, or CRUISE/SET text. Never proves ACC.
- **ACC** adds radar/camera gap-keeping to a preceding vehicle; some
  systems stop and restart. Cues: speedometer dial + car silhouette +
  arrow, or car-with-distance-bars icon. A dedicated
  following-distance control is meaningful support (subject to
  identification + vehicle applicability).
- **ACC Stop & Go** (low-speed stop/resume) is a subset with no
  distinct documented symbol; a following-distance control does not
  establish it.

## Lane family (guide §6.4, p. 16; §3.3, p. 6)

- **LDW (warn-only)**: alerts on unintended drift; tilted car
  crossing dashed lane lines. Never proves intervention.
- **LKA (assist)**: light steering/braking to discourage departure;
  front-view car between dashed lane lines. Never proves centering,
  highway assistance, or hands-free.
- **Centering (active systems)**: continuous lane-centre holding,
  especially motorways. No standalone documented glyph; supervision
  shown via hands-on-wheel alert (wheel + hands + text). Established
  only through manufacturer documentation, never from LKA/LDW icons.
- **Active driving assistance** = LCA + ACC combined, supervised;
  needs both components; never hands-free.

## Collision family (§6.2 p. 14; §3.1–3.2 p. 6; §6.6 p. 18)

- **FCW**: warns (two-cars / starburst motifs; lights, sounds, seat
  vibration). Never proves braking.
- **AEB**: auto-brakes when the driver does not respond (car +
  radar-wave motif). A telltale or visible sensor housing does not
  prove auto-braking; needs documented fitment.
- **Rear cross-traffic warning** vs **reverse emergency braking**:
  warning vs auto-brake while reversing — distinct; warning
  indicators never prove the brake function.

## Speed, parking, blind spot, driver, tyres

- **ISA** (§6.3 p. 15): camera + GPS limits at advisory / supportive
  / intervening levels (roundel display, e.g. circled "70"). A
  display proves display, not active limiting.
- **Parking** (§6.5 p. 17): reversing camera (display only) vs
  proximity sensors (warnings only) vs semi-automatic (system steers;
  driver does speed/braking/gears) vs fully automatic/remote
  (system does all; supervised). Camera alone never proves
  automation; semi vs full resolved by stated responsibilities.
- **Blind spot** (§6.6 p. 18): mirror/cluster light warns (two cars +
  waves motif); steering/braking assist needs documentation.
- **DDAW/driver monitoring** (§6.7 p. 19): take-a-break warnings
  (coffee-cup motif); absence of a telltale proves nothing.
- **TPMS** (§6.8 p. 20): horseshoe + exclamation; informational
  negative control for the pipeline.

## Common symbols (p. 21)

Ten "generally standardised" icons: forward-collision alert
(starburst + car), ACC (speedo + car + arrow), LKA
(steering-wheel-between-dashed-lanes), pedestrian triangle, traffic-sign
"70" roundel, LDW (car + dashed line), collision-mitigating braking
(car + radar waves), blind-spot (two cars + waves), adaptive
headlighting (lamp + arrow), driver-status coffee cup. The same page
carries the explicit disclaimer: symbols, messages, controls, and
operation may differ by manufacturer, model, specification, and
country — always refer to the owner's manual. Standard artwork is a
recognition cue, never a universal icon standard.

## Protections retained

Manufacturer variation is a first-class rule: every entry carries
limitations; loader rejects non-`general` scope; catalog entries are
untouched; `guidance_for()` output is presence-neutral and capped
(600 chars) for prompt use in Milestone 2.
