# Host/test isolation notes — 2026-11-24

Read-only preflight on Dell before tests:

- Docker context: `default`, Unix socket `/var/run/docker.sock`; `DOCKER_HOST` and `DOCKER_CONTEXT` were unset.
- Daemon: Dell host daemon; `DefaultAddressPools=null`; 29 networks. Network/subnet and IPv4 route inventory was inspected without mutation. The test-only `10.252.0.0/16` pool did not overlap listed routes or Docker network subnets.
- Resources: 122 GiB disk free, 17 GiB RAM available.
- Rootless daemon prerequisites: rootless Docker wrapper/rootlesskit present, but `slirp4netns`, `pasta`, `newuidmap`, and `newgidmap` unavailable; `unshare -Ur` failed with `Operation not permitted`. No root escalation or system configuration change attempted.
- Remote fallback: `nec1` showed healthy in the existing fleet inventory, but shell `ssh nec1` failed DNS resolution and no local SSH alias/configured address was present. No new Terminal MCP session and no remote credentials were used.
- Cleanup verification after successful suite: 29 Docker networks; zero network/container names beginning `wm-`. No global Docker cleanup was run.

The solution uses only test-owned Compose networks/subnets created through the repository's regular runtime path and cleaned by the existing sandbox lifecycle.
