# Arvio HA add-ons

Home Assistant OS add-on repository for Arvio lab (`arvio.systems`).

## Install

1. HA → **Settings → Add-ons → Add-on store → ⋮ → Repositories**
2. Add: `https://github.com/nisfikas/arvio-ha-addons`
3. Install **Arvio Agent** → Start with `cloud_url: embedded`

## Surfaces (port 8099)

| URL | Role |
|-----|------|
| `/` | Hub presence + entities |
| `/partner` | Installer claim wizard |
| `/home` | Local light/switch/cover control |

Update local install:

```bash
cd /tmp && wget -O addons.tgz https://github.com/nisfikas/arvio-ha-addons/archive/refs/heads/main.tar.gz
tar -xzf addons.tgz && rm -rf /addons/arvio_agent
cp -a /tmp/arvio-ha-addons-main/arvio_agent /addons/
ha apps reload && ha apps update local_arvio_agent
```
