# Flatpak manifests

Two manifests live here. Keep them in sync — they should differ only in
the `banter` module's `sources` block.

## `land.rob.banter.json`

The development manifest. `build-all.sh` and local `flatpak-builder`
runs use this one. Its `banter` module points at `path: "../.."` so it
builds from the working tree without needing a tag or push.

## `flathub-land.rob.banter.json`

The Flathub-side manifest. **Not used by `build-all.sh`.** When
submitting to Flathub, copy this file's contents into the
`flathub/land.rob.banter` repository (after the submission PR is
accepted). It pins to a git tag + commit so Flathub's builders pull
exactly the released source.

## Keeping them in sync

When you change the dev manifest (new `add-extensions`, finish-arg, GNOME
runtime bump, new bundled module), make the same change to the Flathub
manifest and bump the `banter` module's `tag` + `commit` to point at the
release you're shipping.

A quick drift check:

```sh
diff -u \
  <(jq 'del(.modules[] | select(.name=="banter") | .sources)' \
       build-aux/flatpak/land.rob.banter.json) \
  <(jq '. as $root | del($root._comment) | del(.modules[] | select(.name=="banter") | .sources)' \
       build-aux/flatpak/flathub-land.rob.banter.json)
```

That strips both manifests down to "everything except the `banter`
sources block + the dev-only `_comment` field" and compares. Empty
diff means the manifests agree on every shared knob.
