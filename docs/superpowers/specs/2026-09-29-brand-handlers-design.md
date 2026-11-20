# Brand-isolated CEC handlers (v1.6.0) — design

**Status:** draft. Items marked *proposed* were not yet confirmed; everything else was decided in chat on 2026-09-29.
**Prerequisite:** v1.5.1 (direct `/dev/cec0` transport, Home-button fix) tested on the owner's 2019 Samsung and published.

## Problem

v1.5.0 added LG support by changing code every brand shares (the handshake parser gained `.upper()`). That quietly changed what Samsung TVs received, and nothing caught it: the repo had no tests and no CI. The goal is that a change for one TV brand cannot alter another brand's behaviour.

## Decisions

1. **One file, clear seams.** Everything stays in `cec_bridge.py`, because the OTA updater only downloads five named files. Inside it: a brand-neutral core plus one handler class per brand. Splitting into modules waits for an updater rework (known debt).
2. **One message parser.** Frames are decoded once into sender / destination / opcode / operands, and replies are built from bytes. No text matching.
3. **Core (every TV)** does only standard audio-system behaviour:
   - volume and mute keys go to the Sonos worker; every other key, including power keys, is ignored
   - System Audio Mode request and status, and audio status, are answered to whichever device asked; the keepalive lives here
   - power status is always "on"; the bridge's address and vendor ID are re-announced when the TV announces its own; brand detection happens here
   - ARC requests (`C3`/`C4`) and audio-format queries (`A4`) are declined: the standard answer for a device with no audio input
   - unsupported directed messages get a Feature Abort
   - there is no code path that can send `<Active Source>`, `<Image View On>` or `<Text View On>`
   - *new, from research:* re-assert System Audio Mode when the TV powers on (its `<Report Physical Address>` broadcast); pause the keepalive after the TV broadcasts `<Standby>` until it speaks again
4. **Brand-owned messages** (the seam, listed in one place): ARC (`C3`, `C4`), vendor commands (`89`, `A0`), vendor remote buttons (`8A`, `8B`), Menu Request (`8D`). `C0` is dropped from the request list, since TVs never send it. The core never checks the brand; handlers cannot answer or change core messages.
5. **Handlers**
   - `SamsungTv`: answers `A0 00:00:F0 23` with `A0 00:00:F0 24 00 80`, as libCEC did. Nothing else; declining ARC is standard, not a Samsung quirk.
   - `LgTv`: stays silent on Menu Request, as libCEC's LG handler did. Declines ARC by default. **Admin switch** "LG TVs: use the old ARC reply" restores v1.5.1's replies: `C1` to `C3`; `C2` to `C4` plus a System Audio Mode re-assert after 1 s.
   - `GenericTv` (identified brands without quirks, e.g. Sony, TCL): core only. This changes v1.5.x, which faked ARC acceptance for them.
   - `UnidentifiedTv`: core only, and asks the TV for its vendor ID when the TV speaks (at most every 10 s).
6. **Deliberately not carried over from libCEC:** impersonating LG's vendor ID, LG SimpLink emulation (acting as an "HDD recorder", which is also what grabbed inputs), and libCEC's LG power-status workaround (written for Kodi players).
7. **LG switch mechanics:** stored in `config.json` (default off); a checkbox on the Settings tab; saving restarts the bridge. The settings endpoint changes to update only the fields it receives. *(proposed)*
8. **Tests:** one golden conversation (exact frames in, exact frames out) per case — Samsung (built from a real capture of the owner's TV), LG default, LG with the switch on, Generic, and late identification. A behaviour change in any brand fails that brand's test.
9. **CI:** a GitHub Actions workflow runs `python3 -m unittest discover tests` on every push and pull request.
10. **Diagnostics:** an optional "CEC traffic log" that records every frame in and out, visible on the admin log page, so LG owners can send real traces. *(proposed)*
11. **Release:** v1.6.0 ships as a GitHub pre-release (`/releases/latest` — what "Update Now" uses — skips pre-releases). Testers install it with `POST /api/admin/update {"version": "1.6.0"}`. It is promoted after it checks out on the Samsung; LG is labelled unverified in the notes.
12. **cec-client fallback:** drop it in v1.6.0 *(proposed)*. With handlers answering everything, libCEC running alongside would double-answer and can grab the input again. If `/dev/cec0` is missing, log how to enable the standard `vc4-kms-v3d` driver.

## Research summary

Three research passes (2026-09-29). Links come from those reports; not every one was re-checked.

**Reference behaviour (Android AOSP audio system, v4l-utils `cec-follower`)**
- A device that can't do ARC answers `C3`/`C4`/`A4` with Feature Abort "unrecognized opcode"; a real ARC device replies `C0` (then the TV confirms with `C1`) and `C5` (TV confirms `C2`).
- `70` with a physical address → broadcast `72 01`; `70` without → `72 00`. `7D` → `7E`. `71` → `7A`; CEC 2.0 expects a `7A` after every volume key (at most every 500 ms).
- AOSP has no brand code at all: capability flags plus behaviour that works on every TV. libCEC's per-vendor handlers are where vendor impersonation and an LG input hijack came from (libCEC issues #307, #454).
- Sources: AOSP `HdmiCecLocalDeviceAudioSystem.java`, `ArcInitiationActionFromAvr.java`; `v4l-utils/utils/cec-follower/cec-processing.cpp`.

**LG**
- Three open-source audio-system emulators tested on LG TVs (2016–2019) got volume keys with System Audio Mode alone, ignoring or declining ARC (tsowell/avr-hdmi-cec-volume, lautis/cec-alsa-volume, twalther/phantom-hdmi-cec). No public evidence for 2020+ models.
- The `C3 → C1` reply matches johnboiles/esphome-hdmi-cec's fake-ARC example (tested on TCL). Similar replies misbehaved on Sony (kept re-requesting) and a Samsung S90C (switched to the device, reverted after ~10 s).
- LG forces Sound Out to HDMI ARC when an ARC device is present, and 2017+ models can't output ARC and optical together — real ARC would likely silence a Sonos on optical.
- The contributor's "LG drops ARC every few minutes" is most likely a handshake that never completed, or libCEC reporting the Pi as in standby.
- Non-LG audio systems (Yamaha, Onkyo, Sonos) work with LG TVs for basic volume; LG vendor impersonation is not needed.
- LG sends `05:89:01` (SimpLink init) to audio systems too; an Onkyo Feature-Aborted it and the TV simply repeated it every ~8–16 s.

**Samsung**
- About 10–15 s after power-on the TV re-interrogates address 5 (`83, 8C, 46, A0 …23, 9F, 8F, 7D`, then `70`, then `A4`) and re-sends unanswered queries every ~5 s. A project that didn't re-answer after a power cycle lost volume keys.
- Volume forwarding depends on answering `7D` with "on" (a Yamaha answering "off" got no keys).
- "Samsung drops the device if ARC is accepted" is most likely caused by the off-spec `C1` reply, not ARC itself.
- `A0 00:00:F0 23` goes to every new device; Feature Abort also works (Pioneer, NVIDIA Shield).
- Some 2017+ Samsungs power on when polled (libCEC #424) — hence pausing the keepalive in standby.
- Never set `CEC_LOG_ADDRS_FL_ALLOW_RC_PASSTHRU`: it maps power keys to KEY_POWER, which could shut the Pi down.

## Open questions only a real LG TV can answer (per model year, especially 2020+)

1. Does volume pass with ARC declined, under Sound Out "Optical" and "HDMI ARC"?
2. With the Pi on a non-ARC input, does LG still switch Sound Out to HDMI ARC?
3. Does optical survive a full `C0` ARC handshake?
4. Does answering or ignoring `89:01` affect System Audio Mode?
5. Does eARC on/off change anything? Does Apple TV offer HDMI volume with System Audio Mode alone?

## Out of scope (noted)

- Honouring `70` without an operand (System Audio Mode off, e.g. when the user picks TV speakers): today the bridge always answers "on" and the keepalive re-asserts it. Pre-existing; separate fix.
- Brand detection from the TV's EDID at boot.
- An updater that downloads a file manifest (needed before splitting into modules).
- Merging queued volume steps after a Sonos stall.
- Confirming the Home-button trigger: a capture, or powering the Pi off and pressing Home, rules out the Fire TV's Equipment Control switching inputs by infrared.
