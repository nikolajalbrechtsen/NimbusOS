<!--
  docs/RELEASING.md: publishing NimbusOS.
  What: the one-time GitHub setup, cutting stable and beta releases, rolling
        back a bad release, keeping the repository valid, and what the
        workflows' error messages mean.
  Why:  spec 4.5 (RELEASING.md); docs/PLAN.md §2, NOTE-04, NOTE-33,
        NOTE-37, NOTE-39, NOTE-40, NOTE-41.
-->

# Releasing NimbusOS

Releases are made by pushing a git tag. GitHub Actions does the rest:

| Workflow | When | What |
|---|---|---|
| `ci.yml` | every push and pull request | lint, unit tests, the seven packages with lintian, every package and integration test, the Trixie and Docker checks |
| `release.yml` | a tag `vX.Y.Z` or `vX.Y.Z-beta.N` | build the packages, publish them to the APT repository, build the ISO from the same packages, create the GitHub Release |
| `refresh.yml` | every Monday, and on demand | re-sign the APT repository (it expires 14 days after signing); check Docker's key |

The APT repository lives on the `gh-pages` branch and is served by GitHub
Pages at `https://nikolajalbrechtsen.github.io/NimbusOS/apt/`. Every publish
replaces that branch with one fresh commit, so it never grows. It holds the
last three versions of each package for rollback.

## One-time setup

1. **Make the repository public** (Settings, General, Danger Zone, Change
   visibility). GitHub Pages and anonymous APT access need it.
2. **Create the signing key** and add the secrets `APT_SIGNING_KEY` and
   `APT_SIGNING_PASSPHRASE`, and commit the public key: follow
   [SIGNING.md](SIGNING.md), sections 1 to 4.
3. **Workflow permissions** (Settings, Actions, General, Workflow
   permissions): leave **Read repository contents and packages
   permissions** selected. Each workflow job asks for exactly what it needs
   (`contents: write` to publish, `issues: write` to report a changed Docker
   key), and nothing else gets write access.
4. **Create the repository**: Actions, **refresh**, **Run workflow**. Its
   first run finds no `gh-pages` branch and creates an empty, signed
   repository there, with both suites (`stable` and `beta`) and an empty
   `releases.json`.
5. **Switch on Pages** (Settings, Pages): Source **Deploy from a branch**,
   branch **gh-pages**, folder **/ (root)**, Save. After a minute,
   `https://nikolajalbrechtsen.github.io/NimbusOS/apt/dists/stable/InRelease`
   opens and shows a signed text.
6. **Dependabot** reads `.github/dependabot.yml` by itself. It opens a
   pull request when GitHub releases new versions of the actions the
   workflows use. Merge it when CI passes.

The repository must exist before the first ISO can be built:
`build/build.sh` checks that it answers with a valid signature, because the
image's APT setup points at it (NOTE-04).

## Cutting a release

1. **Write the release notes** in `docs/release-notes/<tag>.md`, for
   example `docs/release-notes/v0.2.0.md`. They become the GitHub Release
   text and the notes the Control Center's Updates page shows. Plain text
   with short paragraphs and `-` lists reads best in both places. If the
   release needs a restart (a new kernel, or Plasma itself), add this line:

   ```
   Restart-Required: yes
   ```

   It isn't shown; it makes the Updates page say "Restart needed".
2. **Commit and push** to `main`, and wait until CI passes.
3. **Tag and push the tag**:

   ```sh
   git tag -a v0.2.0 -m "NimbusOS 0.2.0"
   git push origin v0.2.0
   ```

4. **Watch** Actions, **release**. Each step must pass before the next one
   starts:
   1. **check**: the tag is a release tag, its notes exist, and the signing
      key loads and matches the committed public key.
   2. **debs**: the seven packages, with lintian.
   3. **publish**: they go into the `stable` suite (or `beta`), older
      versions beyond the last three are removed, everything is signed, and
      `gh-pages` is pushed. From this moment, installed systems can update.
   4. **iso**: after GitHub Pages serves the release, the ISO is built from
      the same packages. That takes one to two hours. An ISO over 2 GiB is
      split into parts of 1,900 MiB.
   5. **release**: `SHA256SUMS` is signed and the GitHub Release is created
      with the notes, the download instructions, the signing key's
      fingerprint and the ISO.

Versions come from the tag: `v0.2.0` is the Debian version `0.2.0`, and
`v0.2.1-beta.3` is `0.2.1~beta.3`, which sorts below `0.2.1`, so the final
release always replaces its betas (NOTE-32).

### Beta releases

Tag `vX.Y.Z-beta.N`, for example `v0.3.0-beta.1`. It goes into the `beta`
suite only, and its GitHub Release is marked as a pre-release. Systems on
the Beta channel see both suites and install whichever version is newer;
systems on Stable never see betas. When the beta is good, tag the final
`v0.3.0`.

Switching a system from Beta back to Stable doesn't downgrade anything: it
keeps the beta versions until Stable passes them (NOTE-33). To go back at
once, install the stable versions explicitly, for example
`sudo apt install nimbus-control=0.2.0 nimbusos-desktop=0.2.0`.

## Rolling back a bad release

A published version never changes: publishing different files under a
version that is already published fails. To undo a bad release, publish a
**higher** version that contains the fix or the old code:

1. Revert the bad change on `main` (`git revert <commit>`), or fix it.
2. Write `docs/release-notes/v0.2.1.md` saying what was wrong.
3. Tag `v0.2.1` and push it. Systems update to it like to any release.

Until then, a single system can go back to an earlier version by hand: the
repository keeps the last three versions of each package.

```sh
apt-cache policy nimbus-control                  # the versions available
sudo apt install nimbus-control=0.1.0            # install a specific one
```

Don't delete a tag or a GitHub Release to undo a release: the APT
repository has already published the packages.

## Keeping the repository valid

`InRelease` carries `Valid-Until` 14 days after signing. `refresh.yml`
signs it again every Monday, without changing any package.

**GitHub pauses scheduled workflows** in a repository that has seen no
activity for 60 days (NOTE-40). If refresh stops, the repository expires two
weeks after its last signing, and installed systems show "NimbusOS
repository: has expired" on the Updates page, with a hint that the
repository wasn't re-signed in time. To fix it:

1. Actions, **refresh**: if GitHub shows "This scheduled workflow is
   disabled", choose **Enable workflow**.
2. **Run workflow** once. Systems recover at their next check.

## When Docker's key changes

`refresh.yml` and `ci.yml` check every week, and on every push, that Docker
still serves the key in `build/config/archives/docker.key.chroot`. If not,
refresh opens an issue titled "Docker's APT signing key changed". It never
changes the key itself. To act on it:

1. Read Docker's announcement and confirm the new key's fingerprint from an
   independent source (Docker's documentation).
2. Put the new fingerprint into `NIMBUS_DOCKER_KEY_FPR` in
   `build/nimbus.conf`, then run
   `tools/dev-shell.sh --mount -- tools/fetch-docker-key.sh --write`, which
   accepts the key only if it has that fingerprint.
3. Update the fingerprint comments it names in
   `build/config/archives/docker.list.*`; `tools/fetch-docker-key.sh
   --verify-local` (and `tools/lint.sh`) then pass.
4. Commit, release. The `nimbusos-docker-keyring` package brings the key to
   installed systems.

## When something fails

| Message | Meaning and fix |
|---|---|
| `docs/release-notes/<tag>.md is missing` | Write the notes, commit them, delete the tag (`git push --delete origin <tag>`, `git tag -d <tag>`), and tag the new commit. Nothing was published. |
| `The secret APT_SIGNING_KEY is empty` / `can't sign with APT_SIGNING_PASSPHRASE` | Set the secrets again (SIGNING.md, section 4). |
| `The signing key … is not one of the committed public keys` | The secret holds a different key than `packages/nimbusos-archive-keyring/keys/`. Fix the secret, or follow SIGNING.md, section 6, to introduce a new key. |
| `… is already published with different contents` | The version exists in the repository with other files. Tag a new version. |
| `GitHub Pages didn't serve <tag> within 20 minutes` | Check Settings, Pages (source `gh-pages`, root). The packages are published; re-run the failed jobs once Pages works (Actions, the run, **Re-run failed jobs**). |
| `The NimbusOS APT repository does not answer` (iso, or a local build) | Run refresh once, or check Pages. |
| Free disk space error in **iso** | GitHub's runner had less than 40 GiB free on `/mnt`. Re-run the job; if it persists, use a larger runner for that job. |

Re-running a failed release is safe: publishing the same packages again
changes nothing, and the release step updates an existing GitHub Release
instead of creating a second one.

## Building locally

Local builds are for testing; only `release.yml` publishes.

```sh
tools/dev-shell.sh -- tools/build-debs.sh --install-build-deps --lintian --allow-test-key
tools/dev-shell.sh --privileged -- build/build.sh               # needs the published repository
tools/dev-shell.sh -- tools/lint.sh
```

Packages built with `--allow-test-key` carry a throwaway key and can't be
published or put into an ISO.
