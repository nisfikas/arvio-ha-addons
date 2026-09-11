# LAB-RPI — ανάπτυξη σε υπάρχον HAOS (Raspberry Pi)

**Lab target τώρα:** Raspberry Pi με **HAOS + Supervisor**.  
**Εμπορικό SKU αργότερα:** TMP **103284** (F0-1 flash/BIOS/24h μόνο).

Το Pi **δεν** είναι το προϊόν. Είναι αρκετό για Agent, enroll, local UI, claim με 6-digit.

## Τι μένει για το 103284

- BIOS UEFI / Secure Boot off / AC Recovery
- Factory imaging με Provisioner
- 24h soak στο συγκεκριμένο hardware
- Θερμοκρασίες / θόρυβος / 8GB+128GB όρια

## 1. Cloud API στο LAN σου

Στο Mac (ίδιο δίκτυο με το Pi):

```bash
cd /Users/nicksfikas/arvio
npm install
npm run start -w @arvio/api
```

Ακούει `http://0.0.0.0:8787`. Από άλλο μηχάνημα: `http://<IP-Mac>:8787/health`.

Firewall: επίτρεψε θύρα **8787/tcp**.

## 2. Εγκατάσταση Agent στο Pi

Εύκολη διαδρομή (Samba add-ons):

1. Settings → Add-ons → Samba share (ή File editor)
2. Αντίγραψε τον φάκελο `addons/arvio_agent` στο share **`addons/`** του HAOS  
   (τοπικά add-ons: `/addons/arvio_agent/`)
3. Settings → Add-ons → ⋮ → Check for updates / refresh
4. Εμφανίζεται **Arvio Agent** στα Local add-ons
5. Configuration:
   - `cloud_url`: `http://<IP-Mac>:8787`  (όχι `localhost` — αυτό είναι το Pi)
   - `serial`: π.χ. `rpi-lab-1`
6. Start · άνοιξε Web UI (port **8099**)

Εναλλακτικά: Git repo ως add-on repository όταν το repo είναι remote.

## 3. Έλεγχος

| Έλεγχος | Πού |
|---------|-----|
| API health | `curl http://<Mac>:8787/health` |
| Agent health | `http://<Pi-IP>:8099/health` |
| Presence code | Τοπικό UI Agent |
| HA entities | Ίδιο UI — lights/switches |

## 4. Μετά το 103284

Επανάλαβε F0-1 στο OptiPlex. Το ίδιο add-on εγκαθίσταται και εκεί (`amd64`).

## 5. Agent 0.1.20 — Home model, εντολές, push

Συμβόλαιο: `packages/domain/src/home-model.ts` · προδιαγραφή §9.1 + addendum §14 στο `docs/plans/2026-09-10-home-redesign-design.md`.
Όλα τα timestamps ISO-8601 UTC (`…Z`). Άγνωστα πεδία αγνοούνται, ποτέ σφάλμα. Έκδοση σε τρία σημεία: `config.yaml`, label `io.hass.version` στο `Dockerfile`, `AGENT_VERSION` στο `agent.py`.

### 5.1 `arvio.model` → `HubModelPayload`

Εντολή relay/cloud `{ action: "arvio.model", payload: { offset?, limit? } }` (default `limit` 300, max 600, `offset` ≥ 0).
Το `arvio.list_entities` μένει ως έχει (με το όριο 120)· το `arvio.model` **δεν** έχει όριο 120 — σελιδοποιεί μόνο τα `entities` (`offset/limit/total`)· `floors/areas/devices/scenarios` επιστρέφουν πάντα πλήρη.

| Πεδίο | Πηγή |
|---|---|
| `floors[]` | `config/floor_registry/list` (HA ≥ 2024.4· αλλιώς `[]`) — `floor_id, name, level, icon, aliases` (`aliases` όπως στο registry, αλλιώς `[]`) |
| `areas[]` | `config/area_registry/list` — `area_id, name, floor_id (μόνο αν υπάρχει στο floors), icon, picture, aliases, temperature_entity_id, humidity_entity_id` |
| `devices[]` | `config/device_registry/list` — `device_id, name (name_by_user ?? name), area_id` |
| `entities[]` | `GET /api/states` + `config/entity_registry/list_for_display` (fallback `config/entity_registry/list`) — `area_id = entity.area_id ?? device.area_id ?? null`, `floor_id` από το area, `ec` → μέσω του `entity_categories` map της απάντησης, `hb` (hidden) → εκτός |
| `scenarios[]` | automations με `id` `arvio_*` (+ `GET /api/config/automation/config/{id}`) |
| `snapshot_version` | μονότονος μετρητής στο `/data/hub.json`· ανεβαίνει σε **κάθε** registry event του HA (§5.5) και όταν αλλάξει το fingerprint των registries σε ανάγνωση |
| `ha_version`, `timezone` | `GET /api/config` (`version`, `time_zone`) |
| `sun` | `sun.sun` attributes `next_rising` / `next_setting` |

Οι κλήσεις `config/*_registry/list*` είναι εσωτερικές του HA frontend αλλά σταθερές (σημειωμένες `# TODO confirm` στον κώδικα).

**Φίλτρα οντοτήτων (v1):** domains `light, switch, cover, climate, lock, alarm_control_panel, scene, script, sensor, binary_sensor` και (0.1.21) `media_player` με `device_class speaker|tv|receiver|null` (§6.1)·
`sensor` μόνο `device_class` `temperature|humidity`· `binary_sensor` μόνο `door|window|opening|garage_door`·
`script` **μόνο με πρόθεμα `script.arvio_`** (§14.2 — όχι labels· τα `labels` απλώς περνούν στα attrs)· `entity_category` `config|diagnostic` **εξαιρούνται** (το πεδίο επιστρέφει πάντα `null`)· κρυφές (`hb` / `hidden_by`) και απενεργοποιημένες (`disabled_by`) εξαιρούνται.
Κάθε οντότητα φέρει και `supported_features` (εκτός contract, αγνοείται από αυστηρούς αναγνώστες· χρειάζεται για τα κουμπιά συναγερμού §14.3).
Τα `attrs` είναι το τυπωμένο υποσύνολο `EntityAttrs` (light: `brightness_pct` 0–100, `color_temp_kelvin` — από mireds αν το HA είναι < 2022.12· climate: `hvac_mode` = state, `hvac_action`· cover: `current_position`· lock: μόνο `changed_by` — τα `jammed|opening|open` είναι **states**, δεν υπάρχει `is_jammed`· alarm: `code_arm_required`, `code_format`, και `arming_time`/`delay_time` **μόνο αν** τα αναφέρει ο πίνακας· scene: `scene_entity_ids`· script: `labels`· sensor: `value`, `unit_of_measurement`).

**`scenarios[].show_in_home`:** ο εγκαταστάτης το δηλώνει στο `description` του automation ως JSON
`{"arvio": {"show_in_home": true}}` (ολόκληρο ή ενσωματωμένο σε κείμενο). Εναλλακτικά `variables.arvio_show_in_home: true`. Default `false`.
`action_entity_ids` = κάθε `entity_id` που αναφέρεται στο δέντρο ενεργειών (best-effort).

### 5.2 Υπηρεσίες (allowlist στον agent)

`light.turn_on {brightness|brightness_pct|rgb_color|hs_color|color_temp_kelvin}` (mireds αυτόματα σε HA < 2022.12) / `turn_off` / `toggle` (μόνο για το παλιό Partner path· το Home στέλνει ρητά `turn_on`/`turn_off`),
`switch.turn_on|turn_off|toggle`, `cover.open_cover|close_cover|stop_cover|set_cover_position {position}`,
`climate.turn_on|turn_off|set_temperature|set_hvac_mode|set_fan_mode|set_preset_mode|set_swing_mode`,
`scene.turn_on`, `script.turn_on`, `lock.lock|unlock|open {code?}`,
`alarm_control_panel.alarm_arm_home|alarm_arm_away|alarm_arm_night|alarm_disarm {code?}`,
`arvio.model`, `arvio.batch`, `arvio.trigger_scenario`, `arvio.list_*`, `arvio.*_scenario`, `arvio.pairing_status`, `arvio.zigbee_permit`, `backup.create`, `agent.update`, και (0.1.21) τα `MEDIA_ACTIONS` της §6.5 (`media_player.*`, `music_assistant.*`, `arvio.media_art|media_browse|media_search`).
Οτιδήποτε άλλο → ack `ok:false`, `error: "unknown action: <action>"`, `error_code: "action_not_allowed"` (το «unknown action» είναι αυτό που ψάχνει το cloud για να πέσει στο fallback του).

**LAN allowlist** (`LAN_ALLOWED_ACTIONS`, μόνο για τη χωρίς-auth διαδρομή στο :8099 — βλ. §5.4/5): `light.turn_on|turn_off`, `switch.turn_on|turn_off`, `cover.open_cover|close_cover|stop_cover|set_cover_position`, `climate.set_temperature|set_hvac_mode|set_fan_mode|set_preset_mode|set_swing_mode|turn_on|turn_off`, `scene.turn_on`, `script.turn_on` **μόνο** για `script.arvio_*`, τα read-only `arvio.list_entities`, `arvio.pairing_status`, και (0.1.21) τα `MEDIA_PLAYER_ACTIONS` + `MUSIC_ASSISTANT_ACTIONS` της §6 (transport, ένταση, ομάδες, ουρά — δεν είναι επικίνδυνα). Τα `arvio.media_art|browse|search` (εξώφυλλα, βιβλιοθήκη, playlists = προσωπικά δεδομένα) είναι **μόνο cloud** όσο η LAN διαδρομή είναι χωρίς auth. Είναι allowlist, όχι denylist: ό,τι άλλο υπάρχει στο relay allowlist (lock/alarm, `arvio.model`, `arvio.batch`, `arvio.*` εγγραφές, `backup.*`, `agent.*`, `*.toggle`, κάθε `target`) → `403 lan_forbidden`.

**`target`** `{ entity_ids[] | entity_id | area_id | floor_id }` (στο command ή στο `payload.target`) μόνο για
`light.turn_on|turn_off`, `switch.turn_on|turn_off`, `cover.open_cover|close_cover`:
- HA ≥ 2024.4: native `area_id` / `floor_id` στα service data (τα `entity_ids` συνοδεύουν).
- Παλαιότερο HA: ο agent λύνει όροφος → areas → entities και στέλνει `entity_id: [...]`.
- Πάντα: `affected_entity_ids[]` από τα registries (area οντότητας ?? area συσκευής, χωρίς config/diagnostic/hidden) ώστε το app να μετρά «Ν από Μ». Κενό αποτέλεσμα → `error: "target matched no entities"`.

**`arvio.batch`** `{ payload: { group_id, calls: [ { command_id?, idempotency_key?, action, entity_id?, target?, payload?, expires_at?, confirm_dangerous? } ] } }` (≤ 60 κλήσεις)
→ `data: { command_id, ok, group_id, results: HubCommandAck[], affected_entity_ids, error: null | "partial_failure", hub_ts }` — το cloud διαβάζει `[…]` ή `{results:[…]}`.
Κάθε κλήση περνά τους ίδιους ελέγχους (allowlist, dangerous, dedupe ανά `command_id`)· κληρονομεί το `expires_at` της γονικής εντολής εκτός αν φέρει δικό της (το νωρίτερο ισχύει)· `confirm_dangerous` της κλήσης ισοδυναμεί με `payload.confirm_dangerous`· ένθετο `arvio.batch` απορρίπτεται. Χωρίς `command_id` → `"{parent_id}:{index}"`.
Ενέργειες ασφαλείας (`HOME_SECURITY_ACTIONS`: lock/alarm) **δεν** τρέχουν μέσα σε batch (§14.3): η κλήση απορρίπτεται με `error: "security_in_batch"`, `error_code: "security_in_batch"`, ακόμη κι αν φέρει `confirm_dangerous` — δεν εκτελείται και δεν μπαίνει στο LRU.

### 5.3 Ack (`HubCommandAck`)

Το `data` κάθε αποτελέσματος (`POST /v1/hub/results`):
`{ command_id, ok, state_snapshot, affected_entity_ids[], ha_context_id, hub_ts, error, error_code? }` **συν** τα παλιά flat πεδία (`state, brightness, …`) για παλαιότερους αναγνώστες.
- `command_id`: το `payload.client_command_id` αν υπάρχει (το uuid του Home app), αλλιώς το id του relay. Το `command_id` στο σώμα του `POST /v1/hub/results` είναι πάντα του relay.
- `error_code` (συμβόλαιο `HubCommandAck.error_code?: string|null`): **κάθε** ack με `ok:false` φέρει κωδικό· τα επιτυχή δεν το έχουν. Τιμές: `action_not_allowed`, `lan_forbidden`, `expired` (και για μη αναγνώσιμο `expires_at`), `cancelled`, `confirm_required`, `target_not_supported`, `security_in_batch`, `duplicate_in_flight`, `state_not_confirmed`, `partial_failure` (batch), `invalid_command` (κακό αίτημα: π.χ. `entity_id required`, `target matched no entities`, `calls[] required`, `batch too large`), `execution_failed` (σφάλμα HA / εκτέλεσης), `volume_max_reached` (0.1.21, §6.5).
- `state_snapshot`: μετά το wait-for-state (≤ 2 s) — `{ entity_id, state, last_changed, attrs }` για μία οντότητα, `{ entities: { id: {state, attrs} } }` για target.
- **Χωρίς «αναμενόμενη» κατάσταση (§14.3):** το fallback του 0.1.13 αφαιρέθηκε. Αν το HA δεν επιβεβαιώσει την αλλαγή μέσα στο wait, το ack είναι `ok:false`, `error:"state_not_confirmed"` και το snapshot δείχνει ό,τι βλέπει το HA. Stateless υπηρεσίες (scene/script/stop_cover/set_*) δεν περιμένουν κατάσταση.
- `ha_context_id`: η κλήση γίνεται με το τεκμηριωμένο WS `call_service` (το result φέρει πάντα `context.id`, ώστε και το αργό `state_changed` μιας Zigbee συσκευής να χαρτογραφείται στην εντολή)· fallback στο `POST /api/services/…` όταν δεν ανοίγει WS (εκεί το context έρχεται από τα states που άλλαξαν — `null` αν δεν άλλαξε τίποτα). Ο agent κρατά `ha_context_id → command_id` (LRU 500) για το `command_id` στα push events.
- **Δύο κοινόχρηστες HA websocket συνδέσεις** (`SharedHaWs`, καθεμία με δικό της lock, lazy άνοιγμα, `ping` μετά από 30 s αδράνειας, drop + reopen σε σφάλμα): το **command channel** (`ha_ws_shared_command`) μεταφέρει **μόνο** τα `call_service` που αλλάζουν κατάσταση (φώτα, κλειδαριές, players)· το **query channel** (`ha_ws_query_command`) τα read-only `media_player/browse_media`, `media_player/search_media` και τα `call_service … return_response: true` (`music_assistant.search`, `get_queue`). Ένα αργό browse/search Spotify ή Music Assistant (έως το timeout 20 s του socket) καθυστερεί έτσι μόνο άλλα queries, ποτέ μια εντολή· `HaWsUnavailable` (δεν άνοιξε socket, τίποτα δεν στάλθηκε) → REST fallback όπως πριν.

### 5.4 Ασφάλεια εντολών (§6.9, §9.4)

Στο `handle_command` (relay WS, long-poll **και** LAN):
1. **Dedupe:** LRU 500 σε `command_id`, `idempotency_key` και `payload.client_command_id`· διπλή εντολή → το ίδιο ack με `duplicate: true`, χωρίς νέα εκτέλεση. Ταυτόχρονη παράδοση (WS + long-poll) → η δεύτερη περιμένει την πρώτη.
2. **Λήξη / ακύρωση:** `status: "expired"`, `expires_at` (relay) ή `payload.expires_at` (cloud, από το TTL) στο παρελθόν → `error: "expired"`, καμία εκτέλεση, δεν μπαίνει στο LRU (η επανάληψη θέλει νέο id). `expires_at` που **δεν** διαβάζεται (όχι ISO-8601) → επίσης `expired` (`unparsable expires_at`): μια προθεσμία που δεν καταλαβαίνουμε δεν αγνοείται. `status: "cancelled"` → `error_code: "cancelled"`, καμία εκτέλεση. Κενό / απόν `expires_at` είναι εντάξει.
3. **Allowlist** (§5.2).
4. **`confirm_dangerous`:** για `lock.unlock`, `lock.open`, `alarm_control_panel.alarm_disarm` απαιτείται `payload.confirm_dangerous === true` — το θέτει **μόνο το cloud** αφού επαληθεύσει το `confirm_token` (PIN / slide). Αλλιώς `error: "confirm_required"`. Ο κωδικός συσκευής (`payload.code`) εγχέεται επίσης από το cloud (`entity_secrets`), ποτέ από το Home.
5. **LAN** (`POST /v1/hubs/{id}/commands`, `/api/service/{domain}/{service}` χωρίς auth): ρητό **allowlist** (§5.2 «LAN allowlist») — ό,τι δεν είναι εκεί (lock/alarm, `arvio.model`, `arvio.batch`, `arvio.*` εγγραφές, `backup.*`, `agent.*`, `script.turn_on` εκτός `script.arvio_*`, κάθε `target`) → `403 lan_forbidden`· το `payload` δεν προωθείται, άρα το `confirm_dangerous` δεν μπορεί να τεθεί τοπικά (εξαίρεση 0.1.21: για τα `MEDIA_ACTIONS` περνούν **μόνο** τα `LAN_MEDIA_PAYLOAD_KEYS` — ένταση, πηγή, ουρά, query· ποτέ `confirm_dangerous` / `code` / `target`, βλ. §6.6). Ο LAN server απαντά **μόνο same-origin**: κανένα `Access-Control-Allow-Origin` (ούτε στο `OPTIONS`, που επιστρέφει σκέτο `204` + `Allow`), ώστε σελίδα άλλου origin να μην μπορεί να οδηγήσει το hub μέσω browser· τα σερβιριζόμενα `/`, `/home`, `/remote`, `/partner` καλούν σχετικά paths και δουλεύουν όπως πριν. Τα endpoints κωδικού παρουσίας / claim δεν αλλάζουν εδώ — ξεχωριστή εργασία «LAN hardening: HA ingress».

### 5.5 Push στο relay WS (`/v1/hub/ws`)

Ο agent κρατά μόνιμη HA websocket με `subscribe_events` για `state_changed` και `area_registry_updated`, `floor_registry_updated`, `entity_registry_updated`, `device_registry_updated` (ονόματα events εσωτερικά του HA frontend, σταθερά — `TODO confirm`). Επανασύνδεση με backoff + jitter 1 → 60 s (`Backoff`, κοινό με το relay loop: κάθε αποτυχία register/WS/long-poll διπλασιάζει την αναμονή, ένα `hello` από το relay τη μηδενίζει ξανά στο 1 s). Αν το relay WS δεν είναι ανοιχτό, τα μηνύματα **απορρίπτονται** (καμία ουρά). Ο handler εντολών δεν μπλοκάρει ποτέ από το push.

Registry cache: αμετάβλητο snapshot (`RegistrySnapshot`) που αντικαθίσταται ατομικά σε κάθε reload — κανένα αντίγραφο λεξικών ανά `state_changed`. Τα reloads σειριοποιούνται (ένα κάθε φορά· δεύτερος καλών περιμένει το εν εξελίξει και παίρνει το αποτέλεσμά του, χωρίς δεύτερο round-trip στο HA). Μέχρι να φορτωθεί το πρώτο snapshot τα `state_changed` **δεν** προωθούνται (δεν ξέρουμε ακόμη τι είναι hidden/config)· μόλις φορτωθεί, στέλνεται ένα `{ type:"model" }` ώστε οι clients να ξαναφέρουν το μοντέλο.

Wait-for-state μετά από service call: ένα REST read ανά οντότητα (`GET /states/{id}`) όταν οι επηρεαζόμενες είναι ≤ 8 (ένα `/states` dump μόνο για μεγαλύτερα groups) και μετά, όσο η HA websocket είναι ζωντανή, αναμονή σε **events** (`state_changed` → `note_state_change`) αντί για polling κάθε 150 ms· χωρίς ζωντανό feed, polling REST ανά 150 ms όπως πριν.

| Μήνυμα | Πότε |
|---|---|
| `{ type:"state", hub_id, entity_id, state, attrs, last_changed, context:{id,parent_id,user_id}, command_id }` | `state_changed` **μόνο** εκτεθειμένων οντοτήτων (ίδια φίλτρα με το `arvio.model`)· coalescing ≤ 1 μήνυμα / 1 s ανά οντότητα (το τελευταίο κερδίζει)· `media_player` που άλλαξε **μόνο** σε `media_position`/`media_position_updated_at` **δεν** στέλνεται (§6.7)· `command_id` όταν `context.id` ή `parent_id` ταιριάζει σε εντολή Arvio (§10.5) — το id που εκτίθεται είναι το `client_command_id` αν η εντολή το είχε |
| `{ type:"model", hub_id, snapshot_version }` | μετά από **κάθε** registry event (debounce 2 s): ξαναφορτώνονται τα registries, το `snapshot_version` ανεβαίνει και στέλνεται (ή αμέσως, αν το HA δεν απαντά) |
| `{ type:"heartbeat", hub_id, snapshot_version, agent_version, ts }` | κάθε 30 s + αμέσως μετά το άνοιγμα του WS |
| `{ type:"ping" }` | κάθε 20 s (υπάρχον) |

Το `/health` επιστρέφει επιπλέον `snapshot_version`, `ha_version` και (0.1.21) `ma_available`.

Το cloud heartbeat (`POST /v1/hubs/{hub_id}/heartbeat`, κάθε 30 s μαζί με τον presence code) στέλνει επίσης `snapshot_version`: το cloud (`home-model.ts` `hubSnapshotVersion`) σερβίρει το `site_model_cache` χωρίς να ρωτήσει τον hub όσο η τιμή ταιριάζει με την cached. Agents χωρίς το πεδίο → «άγνωστο» → κανονικό fetch.

### 5.6 Tests

```bash
cd addons/arvio_agent && python3 -m unittest discover -s tests -v
```
Μόνο stdlib `unittest`· fixtures χωρίς HA (`ARVIO_DATA_DIR` για το `/data`)· καλύπτουν builder (με/χωρίς floors, area από συσκευή, config/hidden εκτός, paging, show_in_home), target/batch, expiry/dedupe/confirm, χωρίς-fallback ack, push και LAN· `tests/test_media.py` (0.1.21) καλύπτει το μοντέλο μουσικής, `ma_available`, `media_art` (με/χωρίς Pillow, cache), browse/search, allowlists, `volume_max`, push χωρίς position-only, ownership σεναρίων και τα version pins.

## 6. Agent 0.1.21 — Μουσική (§15 του σχεδίου, Φάση 2b)

Music Assistant (MA) ως «μηχανή μουσικής» **και** native έλεγχος κάθε `media_player` (Cast, Sonos, Apple TV, Spotify, MA players). Χωρίς MA το app δουλεύει σε **«έλεγχο μόνο»**. Τα πεδία μουσικής **δεν** υπάρχουν ακόμη στο `home-model.ts` (το προσθέτει το cloud stream)· αυστηροί αναγνώστες τα αγνοούν. Έκδοση σε τρία σημεία όπως πριν (`config.yaml`, `Dockerfile` label, `AGENT_VERSION`) — ελέγχεται από test.

### 6.1 Μοντέλο (`arvio.model`, push, `arvio.list_entities`)

- Domain `media_player` μπαίνει στα `HOME_ENTITY_DOMAINS`. Εκτίθεται όταν `device_class` ∈ `speaker | tv | receiver | null` (`HOME_MEDIA_DEVICE_CLASSES`)· άλλο `device_class` → εκτός. Ίδιοι κανόνες hidden/config/disabled με τα υπόλοιπα domains.
- `attrs` (υποσύνολο): `media_title, media_artist, media_album_name, app_name, media_content_id, media_content_type, media_duration, media_position, media_position_updated_at, volume_level (0–1), is_volume_muted, source, source_list[], group_members[], shuffle, repeat, supported_features, platform, art_hash`.
  - `platform` = το `pl` του entity registry (`list_for_display`) ή `platform` (`list`): `"music_assistant"`, `"cast"`, `"sonos"`, `"spotify"`, `"apple_tv"`, … — MA players = `platform == "music_assistant"`. Μπαίνει και στο registry fingerprint.
  - `art_hash` = `sha1(media_content_id + entity_picture)`, `null` χωρίς εικόνα. Το `entity_picture` (φέρει το token του HA) **δεν** εκτίθεται ποτέ. Όταν αλλάξει το `art_hash` σε push/model, το app ξαναζητά `arvio.media_art`.
- `capabilities` για media (από `supported_features`, bits `MEDIA_FEATURE`): `volume, volume_step, mute, next_previous, play_media, source, browse, search, grouping, shuffle, repeat, power`.
- Ρίζα μοντέλου: `ma_available: true|false` και `ma_config_entry_id` — υπάρχει μη απενεργοποιημένο config entry με `domain: music_assistant` (ίδια ανάγνωση `GET /api/config/config_entries/entry` με το ZHA· `config_entries()` / `config_entry_for_domain()`). Ανανεώνεται σε κάθε `arvio.model`· cache `MA_INFO`.
- `arvio.list_entities` (παλιό, όριο 120) περιλαμβάνει πλέον `media_player` με τα ίδια attrs σε flat μορφή.

### 6.2 `arvio.media_art {entity_id}` → `{ entity_id, art_hash, mime, data_base64, reason, cached? }`

1. `GET /states/{entity_id}` → `entity_picture` + `media_content_id` → `art_hash`. Χωρίς εικόνα: `art_hash: null, reason: "no_picture"`.
2. LRU **50** ανά `art_hash` (`MEDIA_ART_CACHE`): cache hit → `cached: true`, καμία λήψη. Αποτυχία λήψης (`reason: "fetch_failed: …"`) **δεν** μπαίνει στο cache· `too_large` μπαίνει (ντετερμινιστικό).
3. Λήψη: `/api/media_player_proxy/…?token=` (και κάθε `/api/…`) μέσω του Supervisor core proxy `http://supervisor/core/api/…` με `Authorization: Bearer $SUPERVISOR_TOKEN`· απόλυτα `http(s)://` απευθείας· άλλα σχετικά paths (`/local/…`) → `fetch_failed: unsupported_picture`. Όριο ανάγνωσης 5 MB.
4. Μέγεθος: με **Pillow** → thumbnail ≤ **256 px**, JPEG ποιότητα **80**, και κατέβασμα ποιότητας 80 → 60 → 40 μέχρι να χωρέσει στο **σκληρό όριο 40 KB** (`MEDIA_ART_WIRE_MAX_BYTES`, σε bytes πριν το base64)· αλλιώς `reason: "too_large"`. **Χωρίς Pillow** → επιστρέφεται το πρωτότυπο μόνο αν ≤ 60 KB (`MEDIA_ART_NO_PILLOW_MAX_BYTES`) **και** ≤ 40 KB (το όριο καλωδίου ισχύει και εδώ), αλλιώς `{ data_base64: null, reason: "too_large" }`· `mime` από το Content-Type ή sniffing (jpeg/png/gif/webp).
5. Dockerfile: `pip3 install pillow==11.1.0 || apk add py3-pillow` (amd64/aarch64 wheels· armv7 από το Alpine package). Ο agent τρέχει και χωρίς Pillow.

### 6.3 `arvio.media_browse {entity_id, media_content_id?, media_content_type?}`

HA WS `media_player/browse_media` (τεκμηριωμένο, στο **query channel** — §5.3, δεν κρατά το lock των εντολών) → `{ ok, entity_id, title, media_content_id, media_content_type, media_class, thumbnail, items[], total, truncated }`.
`items[]` (**cap 200**, `MEDIA_BROWSE_MAX_ITEMS`): `{ title, media_content_id, media_content_type, media_class, can_play, can_expand, thumbnail, thumbnail_hash }` — `thumbnail` **μόνο** όταν είναι απόλυτο `https://` που φορτώνει απευθείας το κινητό, αλλιώς `null`· `thumbnail_hash` = sha1 του αρχικού thumbnail (και του proxy URL) ώστε το app να κλειδώνει το δικό του cache· `children_media_class` και λοιπά πεδία του HA αφαιρούνται. Χωρίς HA websocket → `execution_failed`.

### 6.4 `arvio.media_search {entity_id, query, media_type?, media_content_id?, media_content_type?}` → `{ ok, entity_id, query, source, items[], reason? }`

Δρομολόγηση με τη σειρά:
1. Ο player έχει `SEARCH_MEDIA` (4194304) → HA WS `media_player/search_media {entity_id, search_query, media_filter_classes?, media_content_id?, media_content_type?}` (τεκμηριωμένο, query channel)· το `media_type` του payload γίνεται **`media_filter_classes: [media_type]`** (φίλτρο κλάσης, π.χ. `album`), ενώ τα προαιρετικά `media_content_id`/`media_content_type` είναι ο κόμβος browse **μέσα στον οποίο** ψάχνει η integration και περνούν αμετάβλητα (κενό `media_content_id` δεν στέλνεται)· `source: "ha"`, items όπως στο browse (από το `result[]`).
2. Αλλιώς, αν `ma_available` → WS `call_service` με `return_response: true` (query channel) για `music_assistant.search {config_entry_id: ma_config_entry_id, name: query, limit: 10, media_type?: [media_type]}` (REST fallback `POST /api/services/music_assistant/search?return_response` → `service_response`)· `source: "music_assistant"`. Αντιστοίχιση `artists/albums/tracks/playlists/radio` → items με `media_content_id` = MA `uri`, `media_content_type` = `media_class` = MA `media_type` (`artist|album|track|playlist|radio`), `can_play: true`, `can_expand` για artist/album/playlist, `thumbnail` από το `image` με τον ίδιο https κανόνα, συν `artist` (ονόματα με «·») και `album`.
3. Αλλιώς `{ items: [], reason: "search_unavailable", source: null }`.

### 6.5 Υπηρεσίες (relay: `MEDIA_ACTIONS` · LAN: μόνο `MEDIA_PLAYER_ACTIONS` + `MUSIC_ASSISTANT_ACTIONS`)

`media_player.media_play | media_pause | media_play_pause | media_stop | media_next_track | media_previous_track | volume_set {volume_level 0–1} | volume_mute {is_volume_muted} | volume_up | volume_down | select_source {source} | join {group_members[]} | unjoin | play_media {media_content_id, media_content_type, enqueue?: add|next|play|replace} | turn_on | turn_off | shuffle_set {shuffle} | repeat_set {repeat: off|all|one}`,
`music_assistant.play_media {media_id | media_id[], media_type?, artist?, album?, enqueue?: play|replace|next|replace_next|add, radio_mode?}` (target = ο player στο `entity_id`), `music_assistant.transfer_queue {source_player?, auto_play?}`, `music_assistant.get_queue` (WS `call_service` + `return_response: true` → η απάντηση στο ack ως `data.queue`).
Καμία δεν είναι dangerous/security, καμία δεν δέχεται `target`, όλες θέλουν `entity_id` (`media_player.*`). Ό,τι δεν απαριθμείται (`media_seek`, `*.toggle`, `play_announcement`) → `action_not_allowed`.

**`volume_max`** (το cloud το περνά από το `entity_meta.volume_max` / `night_volume_max`, 0–1):
- `volume_set`: `volume_level` κόβεται σε `[0, 1]` και μετά σε `≤ volume_max` (`clamp_volume`).
- `volume_up`: διαβάζεται το τρέχον `volume_level`· ήδη ≥ `volume_max` (ή άγνωστο) → **δεν** στέλνεται τίποτα, ack `ok:false`, `error`/`error_code: "volume_max_reached"`, `volume_max` στο ack· ένα βήμα (0,1) θα το ξεπερνούσε → στέλνεται `volume_set` στο `volume_max` (`clamped_to_volume_max: true`)· αλλιώς κανονικό `volume_up`. Χωρίς `volume_max` δεν γίνεται ανάγνωση.

**Wait-for-state:** `media_play → playing|buffering`, `media_pause → paused`, `media_stop → όχι playing`, `turn_on → όχι off/standby`, `turn_off → off|standby`. `volume_set`, `volume_mute`, `select_source`, `shuffle_set`, `repeat_set` επιβεβαιώνονται από την **αλλαγή attribute** (`expected_attrs`, ένταση με ανοχή ±0,02), όχι από το state· αν το HA δεν το επιβεβαιώσει μέσα στα 2 s → `state_not_confirmed` όπως στα φώτα. `media_play_pause`, next/previous, join/unjoin, play_media, `music_assistant.*` είναι stateless (ένα snapshot read, χωρίς αναμονή). Το `state_snapshot.attrs` των media φέρει και το `platform` από το registry cache.

### 6.6 LAN

Από το `:8099` χωρίς auth περνούν μόνο τα `media_player.*` και `music_assistant.*` (transport/ένταση/ομάδες)· τα `arvio.media_art|browse|search` απαντούν `403 lan_forbidden`. Μόνο εδώ προωθείται `payload`, φιλτραρισμένο στα `LAN_MEDIA_PAYLOAD_KEYS` (`volume_level, volume_max, is_volume_muted, source, group_members, media_content_id, media_content_type, enqueue, shuffle, repeat, media_id, media_type, artist, album, radio_mode, source_player, auto_play, query, client_command_id`) — από `body.payload` ή flat στο body. `confirm_dangerous`, `code`, `target`, `expires_at` δεν περνούν ποτέ· lock/alarm και τα υπόλοιπα μένουν `403 lan_forbidden`.

### 6.7 Push

Τα `state_changed` των `media_player` περνούν από τον ίδιο coalescer (≤ 1 μήνυμα / 1 s ανά οντότητα, το τελευταίο κερδίζει) με τα `attrs` της §6.1 (μαζί με `art_hash`). Εξαίρεση: όταν η μόνη διαφορά old→new είναι `media_position` / `media_position_updated_at` (ίδιο state, ίδια υπόλοιπα attrs), **δεν** στέλνεται τίποτα — το app υπολογίζει τη θέση από `media_position + media_position_updated_at`. Αλλαγή εξωφύλλου (νέο `entity_picture`/`cache=`) αλλάζει το `art_hash` και στέλνεται.

### 6.8 Σενάρια (διόρθωση ελέγχου)

`arvio.trigger_scenario` και `arvio.set_scenario_enabled` απαιτούν `id` `arvio_*` (όπως το `delete_scenario`). Όταν δίνεται ρητό `entity_id` automation, επαληθεύεται μέσω `GET /states/{entity_id}` ότι `attributes.id == id`· αλλιώς `invalid_command` («automation entity_id does not belong to scenario»). Χωρίς `entity_id` η αντιστοίχιση γίνεται όπως πριν από το `/states`.
