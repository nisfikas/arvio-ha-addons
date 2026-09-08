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
