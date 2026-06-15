# Per-machine deployment — agent prompt

Hand this prompt to an AI coding agent (Claude Code, etc.) running **on the target machine**, once
the `predpep:local` image is loaded there (or a `predpep-local.tgz` tarball is present). It deploys
one predPEP node, with the **browser UI active by default** (the UI is baked into the image and
served at `/`), and self-verifies.

See the [README "Distributing the image"](../README.md#distributing-the-image-deploying-machine-by-machine)
section for how to copy the image over first, and [INTEGRATION.md](INTEGRATION.md) for the
controller-side control API.

---

```text
Deploy a predPEP compute node on THIS machine. The image `predpep:local` is a CPU-only
peptide-design backend that ALSO serves a browser UI at `/` — the UI is baked in; keep it
active (it's the default, no flag needed). Steps:

1. Verify Docker is installed and the daemon is up (`docker info`). If not, stop and tell me.
2. Ensure the image exists: `docker image inspect predpep:local`. If missing, look for
   `predpep-local.tgz` / `predpep-local.tar.gz` in the cwd or my home dir and `docker load < <file>`.
   If neither image nor tarball is found, stop and tell me how to provide it.
3. If a container named `predpep_app` already exists, this is a redeploy: `docker rm -f predpep_app`.
   This is SAFE — all jobs live on the `predpep_data` volume, which is NOT removed and re-attaches.
4. Launch:
     docker run -d --name predpep_app \
       -v predpep_data:/tmp/pepspec \
       --log-opt max-size=10m --log-opt max-file=3 \
       --pids-limit 4096 \
       -p 6363:6363 \
       --restart unless-stopped \
       predpep:local
   Leave PREDPEP_CORE_BUDGET unset so it auto-uses this machine's core count. (Optional later:
   `-e PREDPEP_CORE_BUDGET=N`, `-e PREDPEP_MEMORY=32g`.)
5. Wait for boot WITHOUT hammering: `sleep 12`, then `curl -fsS --max-time 6 http://localhost:6363/health`.
   The gevent worker occasionally wedges on boot — if that curl fails, run `docker restart predpep_app`,
   `sleep 12`, and re-check. Do NOT poll /health in a tight 1/sec loop; it can wedge a booting worker.
6. Verify all of:
   - `/health` -> {"service":"predpep-node","status":"ok"}
   - UI is live: `curl -fsS http://localhost:6363/ | grep -i '<title>'` shows "Space Peptides".
   - `/state` returns capacity JSON — note `core_budget`.
   - `docker inspect --format '{{.State.Health.Status}}' predpep_app` is `healthy`
     (can take up to ~90s to flip from `starting`).
7. Report: container status, this machine's primary IP (`hostname -I | awk '{print $1}'`),
   the UI URL `http://<IP>:6363/`, the API base `http://<IP>:6363`, and `core_budget`.
   Note the browser needs internet (NGL/Plotly load from CDNs); the backend does not.

Constraints: there is NO authentication yet — do not open the firewall or expose port 6363 to
untrusted networks without my OK (trusted/LAN only). Never delete the `predpep_data` volume.
If any step fails, paste the exact error and stop.
```
