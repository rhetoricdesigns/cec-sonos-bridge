# Spec: LG Wi-Fi volume ("LG mode")

Status: ready to build (written 2026-10-01). Branch: `lg-wifi-volume`. Target release:
**v1.6.0**. That release also includes the unreleased v1.5.4 already on `main`.

## 0. Rules for the building session

- **The owner is asleep while this is built. Don't ask questions.** Make sensible
  decisions and record each one in section 12, "Decisions made during the build".
- **Git:**
  - Work only on the branch `lg-wifi-volume`.
  - Commit early and often, and push after every meaningful step
    (`git push -u origin lg-wifi-volume`). The container can be reclaimed at any time.
  - Don't push to `main`. Don't create pull requests, tags or releases. A supervising
    session reviews the branch, merges it and prepares the release.
- **The owner isn't a programmer.** Everything they see (admin panel text, README,
  release notes) must be plain English with exact menu paths.
- **Match the existing code:** its style, comment density, naming, and plain-English
  log messages. Tests use `unittest` in the style of `tests/test_cec_bridge.py`.
- **Done means:**
  - every acceptance criterion in section 9 is met
  - `python3 -m unittest discover tests` passes
  - the branch is pushed
  - section 12 is filled in
  - your final message holds the owner summary and reviewer notes described in
    section 11.

## 1. Background

The project is a Raspberry Pi Zero 2 W "CEC-Sonos Bridge":
- It plugs into a TV's HDMI port and joins HDMI-CEC as an Audio System (logical
  address 5).
- It receives the TV remote's volume keys and changes a Sonos speaker over Wi-Fi with
  SoCo.
- It can't receive or play audio.

There are two installs:

- **Samsung room: works and must not change.** Samsung TV, Fire TV, bridge on a non-ARC
  port. Samsung forwards volume keys over CEC to an audio system on any port.
- **LG room: doesn't work.** LG webOS TV (model and webOS version unknown), Apple TV 4K,
  and a **Sonos Ray connected to the TV by optical cable**. The bridge is on a non-ARC
  HDMI port. Pressing volume does nothing to the Ray.

**Hard requirement:** the bridge must control the Ray **over Wi-Fi**. Nothing may rely
on infrared reaching the Ray. Its IR sensor is weak and unreliable, which is why the
bridge exists. So no LG Device Connector, no Universal Control soundbar setup, no Sonos
remote learning and no IR repeaters.

Why HDMI-CEC can't do it on this LG (research summary):

1. **The LG gives its volume keys only to a sound device on its ARC port.** It sends its
   remote's volume keys over CEC only to a sound device on its ARC port, with Sound Out
   set to HDMI ARC (SimpLink). The bridge isn't on that port, so it gets nothing.
2. **Moving the bridge to the ARC port breaks the Ray.** Most LG TVs send sound to one
   output at a time, and SimpLink keeps switching back to the ARC device. The Ray on
   optical would go silent.
3. **With Sound Out on ARC, webOS reports volume as -1**, because the external device
   owns the volume.

So the bridge must follow the LG's **own** volume over the LG's network API, while
Sound Out stays **Optical**.

## 2. Goal

When LG mode is on, the bridge keeps the Ray's volume and mute in step with the LG TV's
own volume and mute. It does this by following the TV over the TV's local network API
(the webOS "second screen" WebSocket API, also called SSAP).

- **LG remote:** the TV's volume number changes, the TV reports it, and the bridge sets
  the Ray to the same number.
- **Apple TV remote:** with Settings > Remotes and Devices > Volume Control set to **TV
  via IR**, it controls the LG's own volume through the LG's IR sensor (not the Ray's).
  It then follows the same path.
- If the Apple TV is instead set to **HDMI**, its volume keys reach the bridge over CEC
  (the existing path). In LG mode, the bridge then pushes the Ray's new level to the TV
  so the two stay in step (see 4.3).

Target response time: the Ray matches within about half a second of the TV's number
changing.

## 3. Non-goals and constraints

- **No change to HDMI-CEC behaviour.** With LG mode off (the default), the bridge must
  behave exactly as it does today. Don't touch:
  - ARC handling
  - System Audio Mode
  - the HDMI hold
  - the hand-back or the splash logic.

  The known CEC bugs in section 10 are out of scope.
- **No new runtime dependencies and no new shipped `.py` files.** This is critical:
  - The over-the-air updater (`web_server.py`) downloads only `UPDATE_FILES`:
    `startup.py`, `ap_mode.py`, `cec_bridge.py`, `web_server.py` and
    `splash_screen.py`. It fetches them from
    `raw.githubusercontent.com/<repo>/v<version>/` and installs no packages.
  - The first update to v1.6.0 runs the **old** updater, so a new module or pip package
    would never arrive.
  - So put the follower in `cec_bridge.py` and the admin UI in `web_server.py`.
  - Use only the Python standard library, plus packages already installed (`soco`,
    `qrcode`, `PIL`, and SoCo's own dependencies such as `requests`).
  - Test-only helpers may live in `tests/`.
- **No infrared**, and no control of TV power or inputs.
- **Pi Zero 2 W:**
  - No busy loops; use blocking reads with timeouts.
  - One extra daemon thread is fine.
  - Don't log every step while a button is held.

## 4. Behaviour

### 4.1 Settings and pairing (admin panel)

Add an "LG TV" section to the admin panel. It can be a card on the Settings tab or a new
tab; choose and record your choice. It contains:

- **"Follow LG TV volume"** toggle. Off by default. Saved in config. Takes effect within
  a few seconds, with no restart needed.
- **"Find my TV"** button. Runs SSDP discovery and lists the LG TVs found (name and IP).
  Also offer a manual IP field.
- **"Pair with TV"** button. Connects and starts pairing, and shows: "Look at your TV and
  choose Allow." On success, save the client key and show "Paired with <name>".
  Optionally show a toast on the TV, such as "Sonos Bridge connected". On rejection or
  timeout, show a plain-English reason.
- **"Forget TV"** (inline confirm, no `confirm()` dialogs). Clears the host and the
  client key.
- **Live status,** polled about once a second while visible:
  - Connected / not connected, with the reason: TV off or unreachable, not paired, pairing
    refused, connection refused.
  - The TV volume and Ray volume (for example "TV 23 → Ray 23"), mute, and the time of the
    last change.
  - The TV's sound output, when reported ("Optical", "HDMI ARC", "TV speakers", ...).
  - **Hints:**
    - If the TV reports volume -1 or sound output is HDMI ARC: "Your TV is sending
      sound over HDMI ARC. For the Ray, set Sound Out to Optical (Settings > Sound > Sound
      Out)."
    - If the sound output is TV speakers: the same hint.
    - If pairing fails repeatedly: "On the TV, turn on LG Connect Apps / TV On With Mobile
      (the name depends on the TV's age)."
- **A short tip** about the Apple TV setting: Settings > Remotes and Devices > Volume
  Control > **TV via IR**.

It must work at phone width, in the existing visual style.

### 4.2 Following the TV

- **TV volume changes to N** (0–100): set the Ray's volume to N (1:1).
- **TV mute changes:** set the Ray's mute to match.
- **Ignore no-op events.** If the reported level and mute already match what the Ray has
  (for example, the echo of a `setVolume` we sent), do nothing. This prevents feedback
  loops.
- **Volume -1 or missing:** don't touch the Ray, and show the hint from 4.1.
- **Holding a button sends many events. Collapse them:**
  - Keep a single "latest target" (volume, mute).
  - At most one apply-latest-target job may be queued on the existing `sonos_queue` at a
    time.
  - The job applies whatever the latest target is when it runs.
- **Use exact-level Sonos setters** (new): `speaker.volume = n` and `speaker.mute = b`.
  Like `handle_volume` and `handle_mute`, they update `current_volume` and `is_muted`
  under `volume_lock` and call `report_audio_status()`.
- **Logging:** log to the main log, with one line per settled change (for example after
  about 1 s of quiet), such as `LG TV volume 23 -> Sonos 23`. Also log connection state
  changes. Don't log every event.

### 4.3 Keeping the TV's number in step

- **On (re)connect:** read the Ray's current volume and mute, and set the TV to them
  (`setVolume` / `setMute`). Then follow the TV. This way, turning the TV on never makes
  the Ray jump.
- **SHOULD:** when CEC volume keys change the Ray (the Apple TV in HDMI mode, or a Samsung
  path that's somehow active), push the Ray's new level to the TV if LG mode is
  connected.
- **COULD (only if time allows):** while connected, poll the Ray about every 5 s. If its
  volume or mute changed from elsewhere (the Sonos app), push it to the TV. Otherwise,
  record this as a known limitation.

### 4.4 Connection lifecycle

- The follower thread starts with the bridge. It idles while LG mode is off or no TV is
  set.
- **Connecting:**
  - Try `wss://<host>:3001` first. Use a self-signed certificate: `check_hostname=False`
    and `CERT_NONE`.
  - Fall back to `ws://<host>:3000`.
  - Remember which one worked, in config.
- **Register** with the stored client key. If the TV asks for pairing while no pairing
  was requested from the admin panel, don't prompt on the TV repeatedly. Report "not
  paired" and wait for the owner to press Pair.
- **Subscribe** to `ssap://audio/getVolume`.
- **When the TV turns off,** the socket closes. Reconnect with backoff: 5 s, doubling up
  to 60 s. Reset the backoff after a successful connection.
- **Send a WebSocket ping** about every 30 s, and treat a missing pong (about 10 s) as
  disconnected.
- **If the IP stops answering for a long time,** optionally re-run discovery and match
  the TV by name or UUID. Otherwise, record this as a known limitation.

## 5. Technical design

### 5.1 Where things go

- **`cec_bridge.py`:**
  - a small stdlib WebSocket client
  - the SSAP client
  - SSDP discovery
  - the follower thread
  - the exact-level Sonos setters
  - config helpers.
- **`web_server.py`:** the admin UI section and the JSON endpoints. Suggested endpoints:
  - `GET /api/lg/status`
  - `POST /api/lg/find`
  - `POST /api/lg/pair` `{host}`
  - `POST /api/lg/enable` `{enabled}`
  - `POST /api/lg/forget`
- **Watch out for this:** `startup.py` runs the bridge with
  `os.execv(... 'cec_bridge.py')`, so `cec_bridge` runs as **`__main__`**.
  `web_server` is imported by `cec_bridge.start_web_server()` and runs in a thread in
  the same process.
  - **Don't** `import cec_bridge` from `web_server`. That would load a second copy with
    separate globals.
  - Instead, have `cec_bridge` hand an object to `web_server` before `run_server()`. For
    example, set `web_server.lg_follower = ...` or pass it as an argument.
  - The endpoints must cope with it being absent (web server running without the bridge)
    by answering "LG mode not available".

### 5.2 WebSocket client (RFC 6455, standard library only)

- **Handshake:**
  - HTTP/1.1 GET `/` with `Upgrade: websocket`, `Connection: Upgrade`,
    `Sec-WebSocket-Version: 13`, and `Sec-WebSocket-Key` (base64 of 16 random bytes).
  - Check that `Sec-WebSocket-Accept` = base64(SHA-1(key +
    "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")).
- **Frames:**
  - Client frames are always masked with a random 4-byte mask.
  - Support payload lengths of 7, 16 and 64 bits.
  - Handle text (1), continuation (0, reassemble), close (8: reply with close), ping (9:
    reply pong), and pong (10).
  - Ignore binary.
- **TLS:** `ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)` with `check_hostname = False` and
  `verify_mode = ssl.CERT_NONE`.
- **Timeouts** on connect and read. The read loop must notice `stop` requests within
  about a second.

### 5.3 SSAP (webOS second-screen API)

Verify the details against `aiowebostv` (Home Assistant's library,
https://github.com/home-assistant-libs/aiowebostv: `aiowebostv/handshake.py`,
`webos_client.py`, `endpoints.py`) and `lgtv2` (https://github.com/hobbyquaker/lgtv2).

- **Register:**
  - Send
    `{"type":"register","id":"register_0","payload":{"forcePairing":false,"pairingType":"PROMPT","manifest":{...},"client-key":"<saved key, omit if none>"}}`.
  - **Copy the manifest verbatim from aiowebostv's `handshake.py`**; don't retype it.
    Put the source URL, commit and licence in a comment and keep its attribution. The
    project is MIT; check that the licence is compatible.
  - Responses:
    - `{"type":"response","id":"register_0","payload":{"pairingType":"PROMPT",...}}`:
      the TV is showing the prompt. Wait up to about 60 s.
    - `{"type":"registered","payload":{"client-key":"..."}}`: paired. Save the key.
    - `{"type":"error",...}`: refused or failed. Report it.
- **Requests:** `{"type":"request","id":"<n>","uri":"ssap://...","payload":{...}}`.
  Responses come back with the same `id`.
- **Subscribe:** `{"type":"subscribe","id":"volume","uri":"ssap://audio/getVolume"}`.
  Events arrive as `type: "response"` with that `id`.
- **Volume payloads (handle both):**
  - Older webOS: `{"volume":12,"muted":false,"scenario":"mastervolume_tv_speaker",...}`.
    Some versions use `mute` instead of `muted`.
  - Newer webOS:
    `{"volumeStatus":{"volume":12,"muteStatus":false,"soundOutput":"external_optical","adjustVolume":true,"maxVolume":100,...},...}`.
  - Read volume as `payload.get("volumeStatus", payload).get("volume")`. Read mute from
    `muteStatus`, `muted` or `mute`.
  - Read the sound output from `volumeStatus.soundOutput`, or infer it from `scenario`
    when present (for example `..._ext_speaker_arc`).
- **Other calls:**
  - `ssap://audio/setVolume` `{"volume":N}`
  - `ssap://audio/setMute` `{"mute":true|false}`
  - `ssap://system.notifications/createToast` `{"message":"..."}` (optional)

### 5.4 Discovery (SSDP)

- Send an M-SEARCH to `239.255.255.250:1900` with
  `ST: urn:lge-com:service:webos-second-screen:1`, `MAN: "ssdp:discover"` and `MX: 2`.
  Collect replies for about 3 s.
- The TV's IP is the reply's source address. Use `SERVER`/`LOCATION` headers if helpful.
  Optionally fetch `LOCATION` for a friendly name; it's fine to show the IP only.
- Also test against a canned SSDP reply in the unit tests.

### 5.5 Config

Add this to `/opt/cec-sonos-bridge/config.json`:

```
"lg_tv": {"enabled": false, "host": "", "name": "", "client_key": "", "secure": null}
```

- Writes must be **atomic** (temp file + `os.replace`) and **merge**: read, update only
  `lg_tv`, then write.
- `web_server.save_config()` also writes this file. Guard against races with a lock, or
  by always re-reading before writing. Make sure saving the other settings keeps
  `lg_tv`.

### 5.6 Interaction with existing code

- Sonos calls go through the existing `sonos_queue` / `sonos_worker`.
- Existing CEC handlers (`handle_volume` ±2, `handle_mute`) stay as they are. Only add
  the "push to TV if LG connected" hook (4.3).
- Don't change the startup order in ways that delay CEC.

## 6. Tests (required)

Write a new `tests/test_lg_mode.py`, plus helpers in `tests/`. Use **a fake LG TV**: a
real local WebSocket server written with the standard library in the test helpers,
speaking SSAP. It must support:

- Registration flows:
  - already paired (immediate `registered`)
  - prompt then accept (key issued)
  - prompt then reject (`error`).
- `subscribe` `getVolume`: send an initial state, then events on demand from the test,
  for example `tv.press_volume(24)` and `tv.set_mute(True)`.
- Both payload formats (old and new), and volume -1.
- `setVolume` / `setMute` requests, which update the fake's state and echo an event like
  a real TV.
- Dropping the connection ("TV turned off") and accepting again ("TV on").
- Optionally TLS. Make a self-signed certificate with the `openssl` CLI at test time;
  skip if it isn't available.

Cover at least:

- WebSocket framing: masking, payload lengths 125, 126 and 65,536, ping/pong, close, and
  fragmented messages. Reject a wrong `Sec-WebSocket-Accept`.
- Pairing: the key is saved to config, the merge keeps other keys, and the write is
  atomic.
- Following: a TV volume or mute event sets the fake Sonos to the same value. No-op
  events cause no Sonos call. -1 is ignored and the status hint is set.
- Collapsing events: a burst of 20 volume events leads to at most a few Sonos calls,
  ending at the last value.
- Connect-time sync sets the TV to the Ray's level.
- The CEC volume hook pushes the new level to the TV when connected.
- Reconnecting with backoff after a drop. Stopping cleanly.
- Discovery parses canned SSDP replies.
- Web endpoints: status JSON, enable/disable, pair, forget, and "not available" when the
  follower is absent.
- **With LG mode off, nothing changes:** all existing tests pass unchanged, and no thread
  activity touches Sonos.

Fake Sonos: patch `soco.SoCo` the way existing tests do, or wrap the Sonos access so it's
easy to fake.

Keep the full suite fast, with no real sleeps longer than a fraction of a second; use
injectable clocks and timeouts. Run `python3 -m unittest discover tests` before every
push.

## 7. Docs and version

- **Version 1.6.0:**
  - `version.json`: `version`, `updated` set to the build date, and a plain-English
    `changelog`.
  - `cec_bridge.py`: the docstring header `CEC-Sonos Bridge v1.6.0`, a new
    "Key improvements over v1.5.4" section, and the version strings on the lines
    containing `Sonos Bridge v1.5.4 starting`, `CEC-Sonos Bridge v1.5.4 Active` and
    `CEC-Sonos Bridge v1.5.4 starting...`. Grep for `1.5.4`.
- **README:** add a plain-English "LG TVs" section covering:
  - the bridge goes on a non-ARC port
  - LG Sound Out is Optical (or whatever the speaker uses)
  - remove any Device Connector soundbar setup
  - admin panel: LG TV > Find my TV > Pair, then accept on the TV, then turn on Follow LG
    TV volume
  - Apple TV Volume Control set to TV via IR
  - troubleshooting with the status hints.

  Also fix the LG advice in the `cec_bridge.py` docstring, which currently says "LG: use
  the ARC-labelled HDMI port". With a speaker on optical, the bridge must **not** be on
  the LG's ARC port.

## 8. Morning test plan for the owner (put this in your final summary)

1. Publish the release. The supervising session provides a link.
2. Update **only the LG-room bridge** first: admin panel, then Updates, then Update Now.
   If both bridges are on the same Wi-Fi, the LG one may be at `sonosbridge-2.local`.
3. On the LG:
   - Set Settings > Sound > Sound Out to **Optical**.
   - Remove any soundbar set up under Device Connector or Universal Control.
4. In the admin panel's LG TV section: Find my TV, then Pair, then choose **Allow** on the
   TV, then turn on Follow LG TV volume.
5. Press volume on the LG remote. The status should show "TV N → Ray N" and the Ray
   should change.
6. On the Apple TV, set Settings > Remotes and Devices > Volume Control to **TV via IR**,
   then press volume on the Apple TV remote.
7. If all is good, update the Samsung-room bridge too. Nothing changes there.
8. If something's wrong, screenshot the LG TV section and send it.

## 9. Acceptance criteria

1. With LG mode off (the default): all existing tests pass unchanged, CEC code paths are
   untouched, and nothing in the follower touches Sonos or the network beyond idling.
2. Against the fake TV:
   - pairing works and persists the key
   - volume and mute mirror 1:1 within one queue cycle
   - no-op and -1 events are handled as in 4.2
   - events are collapsed
   - connect-time sync works
   - reconnecting works
   - both payload formats work.
3. The admin panel section works at phone width. It shows live status and the hints, in
   plain English.
4. No new runtime dependencies and no new shipped `.py` files. `UPDATE_FILES` is
   unchanged.
5. The version is bumped to 1.6.0 everywhere, the README section is written, and the
   whole test suite passes.
6. Section 12 is filled in. The final message has the owner summary (section 8) and the
   reviewer notes.

## 10. Out of scope: known issues, don't fix now

- **ARC replies are backwards.** In `handle_cec_handshake`, `<Request ARC Initiation>`
  (C3) gets `50:C1` and `<Request ARC Termination>` (C4) gets `50:C2`. The correct
  replies are `<Initiate ARC>` `50:C0` and `<Terminate ARC>` `50:C5`. The code also
  treats C0 as a request.
- **"System Audio Mode off" is answered with On.** `<System Audio Mode Request>` without
  an operand means off, but the bridge always broadcasts `5F:72:01`.
- **The setup wizard's ARC warning differs from the old LG advice.** The new README fixes
  the advice. Leave the wizard text unless it contradicts the new README.

## 11. Final message format

1. **Owner summary,** in plain English, at most about 15 lines: what was built, how to
   test it (section 8), and anything they should know.
2. **Reviewer notes:**
   - the commit list
   - files changed
   - test count before and after
   - design decisions (pointing to section 12)
   - anything not done or uncertain, and risks.
   - Include specifically: anything that could affect the Samsung room.

## 12. Decisions made during the build

**Admin panel**

- **A new "LG TV" tab**, next to Updates / Rollback / Settings, rather than a card. Status
  is polled once a second only while that tab is open and the page is visible. To fit four
  tabs at phone width, the tabs' side padding went from 12px to 2px and their text from
  16px to 15px (checked in Chromium at 320px and 375px: no sideways scrolling).
- **The status poll isn't logged.** `WebHandler.log_message` skips `/api/lg/status`,
  which would otherwise add a line a second to the main log.
- **Pairing doesn't block the web server**, which handles one request at a time.
  `POST /api/lg/pair` only starts it. The follower thread does the pairing, and the tab
  shows its progress from `status()['pairing']` ("Look at your TV and choose Allow.",
  "Paired with ...", or the reason it failed). "Find my TV" does block it for about 3 s,
  plus up to 2 s per TV to read its name.
- **The tab says "Sonos", not "Ray"** ("TV 23 → Sonos 23", "For your Sonos, set Sound Out
  to Optical..."), because the bridge works with any Sonos speaker. Log lines read
  `LG TV volume 23 -> Sonos 23`.
- **The TV-speakers hint has its own wording** ("Your TV says it's playing sound through
  its own speakers. If your Sonos isn't playing the TV's sound, set Sound Out to
  Optical..."), since "sending sound over HDMI ARC" would be wrong. It only suggests,
  because older TVs may name their volume scenario after the TV speakers whatever Sound
  Out says (changed in review).
- **The "LG Connect Apps" hint** appears after 2 failed pairings in a row, or while the
  TV refuses connections. Its menu path is hedged ("look under Settings > General, or
  Settings > Network"), because it moves between webOS versions and the TV's model is
  unknown.
- **"Pair with TV" and "Follow LG TV volume" are separate**, as in the spec's test plan:
  pairing doesn't turn following on.
- **"Forget TV" clears host, name, client key and the remembered wss/ws choice**, and
  leaves the "Follow" switch as it was. With no TV it just waits.
- **TV names from discovery and the manual IP** go into the page with `textContent`
  only. The IP must look like an IP address or host name (`is_valid_tv_host`), and the
  name has control characters removed and is cut to 60 characters.

**Following the TV**

- **Exact-level setter:** one function, `set_sonos_level(speaker_ip, volume, muted)`. It
  only sets what differs from our copy of the speaker's level (`current_volume` /
  `is_muted`), then updates them under `volume_lock` and calls `report_audio_status()`.
  It compares against our copy instead of asking the speaker, to save a network round
  trip on every step.
- **Echoes:** besides "the speaker already has it", a report counts as our own echo when
  each field either didn't change or matches a level we sent the TV in the last 2 s.
  This stops a stale echo pulling the speaker back: CEC up, up quickly sends the TV
  32 and 34, and the TV's late "32" must not set the speaker to 32. A matched echo is
  forgotten once seen, so a real unmute right after connecting is still followed.
- **Connect-time sync goes through the Sonos queue.** A job reads the speaker
  (`read_sonos_level`), and the follower waits up to 5 s for it, then sends `setVolume` /
  `setMute`, then subscribes. If the speaker can't be read, the TV keeps its own level
  and the bridge just follows it. The first volume report after a sync is ignored,
  because it can still carry the TV's old level. The TV reports again once it has
  taken the new one.
- **If the TV doesn't say whether it's muted,** the speaker's mute is left alone.
- **TV volumes above 100 are capped at 100.** -1 or a missing volume leaves the speaker
  alone. -1 shows the ARC hint, as does a sound output containing "arc". Sound output
  is read from `volumeStatus.soundOutput`, else `soundOutput`, else `scenario`.
- **Logging:** one `LG TV volume N -> Sonos N` line once the volume has been still for
  1 s. Connection states are logged only when the message is new, so a TV that's off
  overnight costs two lines ("Lost the TV", "Can't reach the TV"), not one a minute.
  "Connecting..." is never logged. With LG mode off, the follower logs nothing at all.

**Connection**

- **Order:** wss://3001 first, then ws://3000, or the one that worked last time first
  (saved as `secure`). When both fail, the reported error prefers the one that isn't
  "connection refused", since most TVs only open one of the two ports.
- **No "hello" or `getSystemInfo` before registering** (aiowebostv sends them; lgtv2
  doesn't). Only the registration from aiowebostv's `handshake.py` is sent.
- **The TV asks for pairing while nobody pressed Pair** (it forgot the key): the bridge
  closes the connection, shows "The TV has forgotten the bridge. Press Pair...", and
  stops trying until Pair is pressed, the TV is forgotten, or Follow is turned off and on
  again. Turning Follow on allows one more try with the saved key, so the TV asks at most
  once per press of the switch.
- **A registration that times out** (10 s with no answer) counts as "TV not answering":
  the bridge retries with backoff and doesn't ask for pairing.
- **Waking up:** the follower waits on an `Event` while idle. While connected, it
  `select()`s on the TV socket and a socket pair that's poked on stop, on settings
  changes and on CEC changes to send. So stop and settings changes take effect at
  once, without busy loops. The read loop also looks up every second for pings and for
  logging settled changes.
- **Only the follower thread writes to the TV socket.** The CEC hook
  (`tell_lg_tv` → `speaker_changed`) puts the level in a one-slot outbox and pokes the
  thread. Python's `ssl` sockets aren't safe for a read in one thread and a write in
  another.
- **Thread start:** the follower object is created in `main()` before the web server
  thread, so `web_server.lg_follower` can be set before `run_server()`. Its thread starts
  in `run_bridge()` after the Sonos worker. It needs the worker for the connect-time sync
  and doesn't delay CEC. Creating it and starting it can't raise
  (`create_lg_follower` / `start_lg_follower` catch everything), so LG mode can never
  stop the bridge starting.
- **Config lock:** `cec_bridge.start_web_server()` hands `web_server` both
  `lg_follower` and `config_lock`. `web_server.save_config()` takes the lock, re-reads
  the file, keeps its `lg_tv` as it is on disk, and writes atomically (temp file and
  `os.replace`). `save_lg_settings()` takes the same lock, re-reads, changes only `lg_tv`
  and writes atomically with `fsync`. It refuses to overwrite a `config.json` it can't
  parse.

**Licence:** aiowebostv is Apache 2.0, and copying its registration manifest into an
MIT project is fine as long as attribution is kept. The comment above
`LG_REGISTRATION_PAYLOAD` gives the source URL at commit
`f52c91bfe6c8ff1cd2639f59db8aa408320abe69`, the licence and the copyright holders. The
README credits it too. The current manifest has no `signed` block (older lgtv2-style
manifests did). It was copied as it is.

**Tests:** `tests/fake_lg_tv.py` is a real WebSocket and SSAP server on 127.0.0.1 (TLS
optional, with a certificate made by the `openssl` command, skipped without it). It
checks that client frames are masked. `tests/test_lg_mode.py` has 89 tests. The follower
tests run a real follower thread against the fake TV and a fake `soco` module. Timeouts
are class attributes that the tests shrink (retry 0.05 s, read loop 0.05 s, and so on).
The whole suite takes about 7 s.

**Known limitations (not built)**

- **Changes made in the Sonos app aren't pushed to the TV** (the "COULD" in 4.3). The TV's
  number catches up the next time the bridge connects or a CEC key is pressed. Meanwhile
  the next LG remote press sets the Sonos to the TV's number + 1.
- **No re-discovery when the TV's IP address changes** (4.4, optional). The tab keeps
  saying "Can't reach the TV". The README says to Find and Pair again, or give the TV a
  fixed address in the router.
- **Re-running the Wi-Fi setup in hotspot mode (`ap_mode.py`) rewrites `config.json`
  without `lg_tv`**, so the TV must be paired again afterwards. `ap_mode.py` wasn't
  touched, to keep the change small. Factory reset deletes it on purpose.
- **Pressing volume on the TV in the second it takes to connect** is overridden by the
  connect-time sync (the TV takes the Sonos's level).
- **Not tried on a real LG TV.** Behaviour comes from aiowebostv and lgtv2 and is tested
  against the fake TV only.

**Review (supervising session, 2026-10-01).** I read the whole diff against `main`. The
review found no blocking problems. Checked:
- `UPDATE_FILES` is unchanged, with no new shipped files or dependencies (standard
  library only).
- `web_server` doesn't import `cec_bridge`.
- With LG mode off, the only change on the HDMI-CEC side is `tell_lg_tv()` after
  `handle_volume`/`handle_mute`, which returns at once unless connected.
- Config writes are atomic and keep the other keys. The version is 1.6.0 everywhere.
- The LG tests passed 12 times in a row, with no flaky timing.

Changed: the TV-speakers hint wording (see above).

## Appendix: sources

- lgtv2: ports (2023+ firmware accepts only wss on 3001; before 2018 only ws on 3000),
  the pairing prompt, and ARC reporting -1. https://github.com/hobbyquaker/lgtv2
- aiowebostv: handshake, endpoints, and volume parsing.
  https://github.com/home-assistant-libs/aiowebostv
- webos-sonos-overlay: Sonos kept in step with LG volume (needs a rooted TV and ARC; a
  different approach). https://github.com/deten/webos-sonos-overlay
- LG manual, SimpLink and HDMI ARC:
  https://gscs-manual.lge.com/PNZ/MR9GHA/English/content7.html
- LG sends sound to one output at a time:
  https://forum.hearingtracker.com/t/can-you-use-tv-connector-and-your-external-sound-bar-simultaneously/109566/10
- LG keeps switching back to the ARC device:
  https://fr.community.sonos.com/home-theater-228993/arc-sl-lg-c2-keeps-switching-back-to-arc-sl-when-trying-to-switch-to-other-sound-outputs-like-tv-speakers-6868990
- Apple TV volume control options:
  https://support.apple.com/guide/tv/control-your-tv-and-volume-atvbbe2477c9/tvos
- Linux CEC helpers (correct ARC replies):
  https://github.com/torvalds/linux/blob/master/include/uapi/linux/cec-funcs.h
