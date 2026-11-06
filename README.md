# Arvio HA add-ons

## Surfaces (port 8099)

| URL | Role |
|-----|------|
| `/` | Hub page: state and serial. The presence code only while the hub has no owner |
| `/panel` | Wall panel |

Anyone on the network reaches this port, so commands and device names need a paired panel or a signed-in app. Neither sends its key: each request signs a one-time code from `GET /v1/lan/nonce` (`X-Arvio-Lan-Nonce`, `X-Arvio-Lan-Sig`, and `X-Arvio-Lan-Id` for the app). A box with `cloud_url: embedded` has nobody to ask: its page lists and controls the devices, doors excepted. See `docs/ADR/017-lan-path-needs-a-caller.md` in the arvio repo.

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
