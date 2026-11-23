<!--
  docs/SIGNING.md: the NimbusOS archive signing key.
  What: how the key is generated offline, where its public and private
        halves go, how it is backed up, how to rotate it without breaking
        installed systems, and what to do if it leaks.
  Why:  spec 4.5 (SIGNING.md) and ground rule 6: the private key exists only
        in GitHub Actions secrets; the repository holds only the public key.
-->

# The NimbusOS signing key

One OpenPGP key signs everything NimbusOS publishes:

| Signed | Checked by |
|---|---|
| The APT repository's `InRelease` and `Release.gpg` | apt on every installed system, against `/usr/share/keyrings/nimbusos-archive-keyring.gpg` (the `Signed-By` of `nimbusos.sources`) |
| `releases.json.sig`, the release notes | `nimbus-update`, with gpgv against the same keyring |
| `SHA256SUMS.gpg`, next to each ISO | people downloading the ISO (README) |

The **public** key is committed in `packages/nimbusos-archive-keyring/keys/`
and shipped in the `nimbusos-archive-keyring` package. GitHub Pages also
serves a copy at `apt/nimbusos-archive-keyring.asc`. The **private** key
exists only in two places: the GitHub Actions secret `APT_SIGNING_KEY`, and
your offline backups. It is never committed, never on a build machine's
disk, and never in a log.

Until a public key is committed, CI builds the packages with a throwaway
key and says so in a warning. Packages built that way can't be published:
`release.yml`, `build/build.sh` and `tools/import-signing-key.sh` refuse
them (docs/PLAN.md NOTE-44).

## 1. Generate the key (once, offline)

Use a computer that isn't connected to a network, for example a Debian or
NimbusOS live session started from USB with networking switched off. Work in
a fresh, temporary keyring:

```sh
export GNUPGHOME="$(mktemp -d)"
gpg --quick-generate-key "NimbusOS Archive Signing Key 2026" ed25519 sign 3y
```

- **ed25519** is small and fast, and apt's verifier (sqv in Debian 13)
  accepts it.
- **sign** makes a signing-only key; nothing is ever encrypted to it.
- **3y**: it expires in three years. Extend it well before then (section 5)
  or rotate (section 6). An expired key makes apt reject the repository.
- The user ID has no e-mail address on purpose: the key belongs to the
  project.

gpg asks for a passphrase. Choose a long one: it becomes the secret
`APT_SIGNING_PASSPHRASE`.

Note the fingerprint:

```sh
FPR="$(gpg --with-colons --list-secret-keys | awk -F: '$1 == "fpr" { print $10; exit }')"
echo "$FPR"
```

## 2. Export the three pieces

```sh
gpg --armor --export "$FPR"             > nimbusos-2026.asc         # public: goes into the repository
gpg --armor --export-secret-keys "$FPR" > nimbusos-2026-secret.asc  # private: goes into the GitHub secret
cp "$GNUPGHOME/openpgp-revocs.d/$FPR.rev" nimbusos-2026-revoke.asc  # revocation certificate: backups only
```

The public file's name follows `nimbusos-<year>.asc`; every `*.asc` in the
keys folder goes into the keyring package.

## 3. Back up the private key and the revocation certificate

Copy `nimbusos-2026-secret.asc` and `nimbusos-2026-revoke.asc` to two
separate offline media (for example two USB sticks, or paper with
`paperkey`), and store them apart. Write the passphrase down separately.
Anyone with the private key and the passphrase can publish packages that
every NimbusOS system installs.

## 4. Put the halves in place

On your normal computer, with only `nimbusos-2026.asc` and
`nimbusos-2026-secret.asc` copied over:

1. **The public key into the repository**:

   ```sh
   cp nimbusos-2026.asc packages/nimbusos-archive-keyring/keys/
   gpg --show-keys packages/nimbusos-archive-keyring/keys/nimbusos-2026.asc   # check the fingerprint
   git add packages/nimbusos-archive-keyring/keys/nimbusos-2026.asc
   git commit -m "keyring: add the 2026 archive signing key"
   git push
   ```

2. **The private key and passphrase as secrets** (repository Settings,
   Secrets and variables, Actions, New repository secret; or with the
   GitHub CLI):

   ```sh
   gh secret set APT_SIGNING_KEY < nimbusos-2026-secret.asc
   gh secret set APT_SIGNING_PASSPHRASE          # gh asks for the value
   ```

3. Delete the private key from this computer: `shred -u nimbusos-2026-secret.asc`
   (on Windows, delete it and empty the recycle bin).

Then run the refresh workflow once (docs/RELEASING.md, one-time setup). Its
first step, `tools/import-signing-key.sh`, loads the secret and checks that
the passphrase works and that the key matches the committed public key. A
mistake stops it there, with a message, before anything is published.

## 5. Extend the expiry date

Do this at least six months before the key expires. It changes only the
expiry date, so nothing that was signed needs signing again.

1. On the offline computer, import the private key from a backup, extend it,
   and export the public key again:

   ```sh
   export GNUPGHOME="$(mktemp -d)"
   gpg --import nimbusos-2026-secret.asc
   gpg --quick-set-expire "$FPR" 3y
   gpg --armor --export "$FPR" > nimbusos-2026.asc
   gpg --armor --export-secret-keys "$FPR" > nimbusos-2026-secret.asc   # the updated backup
   ```

2. Replace the backups, commit the new `nimbusos-2026.asc` over the old one,
   update the secret `APT_SIGNING_KEY`, and publish a release. Installed
   systems get the new expiry date with the `nimbusos-archive-keyring`
   update.

## 6. Rotate to a new key without breaking installed systems

An installed system trusts only the keys in its installed keyring package.
So the new key must reach every system *before* anything is signed with it.

1. **Generate the new key** as in sections 1 to 3 (for example
   `NimbusOS Archive Signing Key 2028`, file `nimbusos-2028.asc`).
2. **Ship it, still signing with the old key.** Commit `nimbusos-2028.asc`
   *next to* `nimbusos-2026.asc` and publish a release. The keyring package
   now trusts both keys. Leave the secrets alone, so this release is signed
   with the old key, which installed systems already trust.
3. **Wait** until systems have that release: at least a few weeks. Systems
   on Notify only or Off update only when their owners do.
4. **Switch the secrets** to the new key (`APT_SIGNING_KEY`,
   `APT_SIGNING_PASSPHRASE`) and run the refresh workflow. From now on
   everything is signed with the new key, and every updated system already
   trusts it.
5. **Retire the old key** one or two releases later: delete
   `nimbusos-2026.asc` from the keys folder and publish a release.

A system that missed step 2 (for example one that was off for months)
rejects the repository after step 4. It can be fixed by hand, by
downloading the new keyring package straight from the repository's pool
over HTTPS and installing it:

```sh
curl -fsSLO https://nikolajalbrechtsen.github.io/NimbusOS/apt/pool/stable/nimbusos-archive-keyring/nimbusos-archive-keyring_<version>_all.deb
sudo apt install ./nimbusos-archive-keyring_<version>_all.deb
```

## 7. If the private key leaks

Installed systems can verify only signatures from the keys they already
trust, so the way out still runs through the leaked key, as fast as possible:

1. **Generate a new key** (sections 1 to 3) and commit its public half next
   to the old one.
2. **Publish a release at once**, still signed with the old key: installed
   systems can verify nothing else, and this release brings them the new key.
3. **Switch the secrets** to the new key straight away (no waiting period
   this time) and run the refresh workflow.
4. **Remove the old public key** from the keys folder and publish another
   release. Then publish the revocation certificate where people checking
   ISOs will see it: import it (`gpg --import nimbusos-2026-revoke.asc`) and
   attach the revoked public key (`gpg --armor --export "$FPR"`) to an
   announcement on the project page.
5. **Tell users** which releases were signed before the leak and which
   after, and how to install the new keyring by hand (end of section 6) if
   their system missed step 2.

This can't fully protect systems that don't update in time: whoever holds
the leaked key can sign packages they would accept. That is why the private
key stays offline, and in one secret.
