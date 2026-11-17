# NimbusOS archive public keys

This directory holds the public OpenPGP key(s) that sign the NimbusOS APT
repository, as ASCII-armored files named `nimbusos-<year>.asc`.

- Every `*.asc` file here goes into
  `/usr/share/keyrings/nimbusos-archive-keyring.gpg`. During a key rotation the
  old and the new key are both here, so installed systems trust both until the
  switch is complete (see `docs/SIGNING.md`).
- Only **public** keys belong here. The private key lives only in the GitHub
  Actions secret `APT_SIGNING_KEY`.
- `tools/build-debs.sh` refuses to build without a key here (CI test builds use
  `--allow-test-key`), and `build/build.sh` checks that the built package trusts
  exactly the keys in this directory.
