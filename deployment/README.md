# Git Deployment

Push the tested commit to `codex/camoufox`. On the existing server, clone/fetch
the repository into `/opt/turb-gpt-free-register/releases/<full-commit>`,
check out that exact commit detached, then run:

```sh
python3 deployment/server_rollout.py --deployment-root /opt/turb-gpt-free-register
```

The Dockerfile extends the server's existing
`turb-gpt-free-register:20261126-roxy` desktop image. Keep that base image;
it contains the installed Roxy desktop and its Linux dependencies.
The new image installs the compatible Playwright and Camoufox dependencies,
downloads the paired Camoufox binary, GeoIP data and fingerprint model, and
labels the image with the Git revision.

Before changing the service, the rollout runs targeted tests and real headless
and desktop Firefox smoke checks inside isolated containers. Those checks use a
local test proxy and page, with no registration or mailbox purchase.

The script waits for registration jobs to finish, stops the existing container,
backs up changed source, `.env`, Remail state and a consistent SQLite snapshot,
then installs only Git-tracked application files from the server checkout.
Existing runtime paths and Roxy home mounts stay in place. It reuses the existing
Compose configuration with an image-only override, checks container health and
source hashes, and restores the old source/image if rollout fails.

Verify an existing release with the same command plus `--verify-only`.
The selected registration driver is read from the existing `.env`.
