# Arvio HA add-ons

## Surfaces (port 8099)

| URL | Role |
|-----|------|
| `/` | Hub presence |
| `/partner` | Claim wizard |
| `/home` | Local control |
| `/remote` | Remote command API (relay protocol) |

Options: `cloud_url: embedded`, optional `relay_url` + `relay_token` for outbound relay (ADR-003).

```bash
cd /tmp && wget -O addons.tgz https://github.com/nisfikas/arvio-ha-addons/archive/refs/heads/main.tar.gz
tar -xzf addons.tgz && rm -rf /addons/arvio_agent
cp -a /tmp/arvio-ha-addons-main/arvio_agent /addons/
ha apps reload
ha apps stop local_arvio_agent
ha apps uninstall local_arvio_agent
ha apps install local_arvio_agent
ha apps start local_arvio_agent
```
