<!--
  catalog/SCHEMA.md: the catalog app format, field by field.
  What: what an app in catalog/ consists of, every manifest.json field, what
        a Compose template may use and what the deploy engine adds, and the
        rules for changing an app.
  Why:  manifest.schema.json (machine-checked) and the Control Center's
        parser (control-center/backend/nimbus_control/manifest.py) implement
        this contract; tests/test_catalog.py checks that all three agree.
-->

# The NimbusOS app catalog

Each app the Control Center offers for one-click installation is a folder in
`catalog/`, installed by the `nimbus-catalog` package to
`/usr/share/nimbus-control/catalog/<id>/`:

| File | What it is |
|---|---|
| `manifest.json` | What the app needs: ports, folders, settings, its web page. This document. |
| `docker-compose.yml.j2` | A Jinja2 template for the app's Compose file ([Templates](#templates)). |
| `icon.svg` | A monogram on the app's category color, not the vendor's logo (PLAN NOTE-28). |

Nothing else may be in an app folder, and nothing in it may be a symbolic link.
The package build (`packages/nimbus-catalog/debian/check-catalog.py`) refuses
a catalog that breaks any rule here, so a bad app never reaches a computer.

## manifest.json

`manifest.schema.json` is the same contract as a JSON Schema (draft 2020-12).
Every object below is closed: an unknown key (a misspelt `share-hint`) is an
error. Booleans are `true`/`false` and numbers are JSON numbers, never
strings.

### Top level

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string `^[a-z0-9][a-z0-9-]{0,62}$` | yes | Equal to the folder name. Also the first install's name. |
| `name` | non-empty string | yes | Shown in the catalog and on the dashboard. |
| `description` | non-empty string | yes | One sentence for the catalog card. |
| `category` | non-empty string | yes | Groups and colors the app (Management, Monitoring, Passwords, Files, Media, Network, Smart home). |
| `template_version` | integer ≥ 1 | yes | Raise it whenever the template or a default changes ([Changing an app](#changing-an-app)). |
| `main_service` | string `^[a-z0-9][a-z0-9_-]{0,62}$` | yes | The Compose service the engine configures; the template must define it. |
| `network_mode` | `"bridge"` (default) or `"host"` | no | `host`: the app shares the computer's network; no ports are published. |
| `ports` | list of [ports](#ports) | no | |
| `volumes` | list of [volumes](#volumes) | no | |
| `env` | list of [variables](#env) | no | |
| `host_paths` | list of [host paths](#host_paths) | no | |
| `webui` | [web page](#webui) or `null` | no | Where Open goes. `null` or absent: no Open button. |
| `single_instance` | boolean | no | The template fixes a container or volume name, so the app can be installed once. A second install is refused. |
| `companions` | [companions](#companions) | no | Containers, volumes and a network the app's own container creates. |
| `notes` | string | no | Shown before installing: first sign-in, quirks, what to do next. |

### ports

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | string `^[a-z][a-z0-9_]{0,31}$` | required | Unique in the app. Templates and `webui.port` refer to it. |
| `container` | integer 1–65535 | required | The port inside the container. |
| `host` | integer 1–65535 | `container` | The default host port. 1-click install takes the next free one if it is in use. |
| `protocol` | `"tcp"` or `"udp"` | `"tcp"` | |
| `bind` | `"all"` or `"lan"` | `"all"` | `lan`: publish on the computer's LAN address only (Pi-hole's DNS, PLAN NOTE-18). |
| `fixed` | boolean | `false` | The host port must not change (DNS on 53, a proxy on 80/443). If it is taken, the plan warns and the install stops at that field. |
| `description` | string | `""` | Shown next to the port in Advanced install. |

### volumes

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | string `^[a-z][a-z0-9_]{0,31}$` | required | Unique in the app. |
| `container` | absolute path, no `:` and no `..` | required | Where it is mounted in the container. |
| `kind` | `"bind"` or `"named"` | `"bind"` | `bind`: a folder on the computer. `named`: a Docker volume. |
| `default` | string | `id` (bind only) | `bind`: a folder inside the app's folder (`/var/lib/nimbus-control/stacks/<name>/`), up to 8 parts of `[A-Za-z0-9._-]`, no `.` or `..`. `named`: required, a Docker volume name `^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$`. |
| `share_hint` | share name | none | Bind only: when a share of this name exists, 1-click install uses its folder instead (Jellyfin's `/media` → the `media` share). |
| `owner` | `{"uid": 0–65534, "gid": 0–65534}` (`gid` defaults to `uid`) | none | The container's user. The folder in the app's folder is handed to it before the first start (privd `stack.prepare_volume`, PLAN NOTE-31). Shares are never changed. |
| `description` | string | `""` | |

Named volumes belong to the install (`<name>_<volume>`), so two installs never
share a database. A template that needs one fixed name declares the volume
itself with `name:` and marks the app `single_instance`.

### env

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | `^[A-Za-z_][A-Za-z0-9_]{0,63}$` | required | Unique in the app. |
| `default` | string or `null` | `null` (empty) | |
| `required` | boolean | `false` | Install refuses an empty value. |
| `secret` | boolean | `false` | Masked in the UI and in template diffs. |
| `generate` | `"password"`, `"hex"`, `"base64"` or `null` | `null` | Made at install: 32 URL-safe characters, 64 hex digits, or 44 base64 characters. |
| `description` | string | `""` | |

### host_paths

The only paths outside the app's folder and the shares that the app may mount
(PLAN NOTE-30), for example the Docker socket.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `host` | absolute path, no `:` and no `..` | required | |
| `container` | absolute path, no `:` and no `..` | required | |
| `read_only` | boolean | `false` | |

### webui

| Field | Type | Default | Meaning |
|---|---|---|---|
| `port` | a port `id` of this app | required | Open uses that port's host port. |
| `path` | `^/[^\s?#]*$` | `"/"` | |
| `scheme` | `"http"` or `"https"` | `"http"` | `https` for apps that serve their page with their own certificate (Portainer, the AIO interface). |
| `secure_context` | boolean | `false` | The page needs a secure context (Vaultwarden's web vault), so Open uses `localhost`, which browsers treat as secure, instead of the computer's name. |

### companions

For an app whose container creates more containers through the Docker socket
(Nextcloud AIO, PLAN NOTE-29). At least one field.

| Field | Type | Meaning |
|---|---|---|
| `containers` | name prefix `^[a-z0-9][a-z0-9_.-]{2,62}[-_]$` | Containers without a Compose label whose names start with it count as part of the app on the dashboard (not in its status), and uninstalling stops them (60 seconds to close their databases) and removes them. |
| `volumes` | name prefix, same form | Kept on uninstall; removed with "Delete data". |
| `network` | Docker network name | Removed on uninstall. |

A prefix must end in `-` or `_`, so it can never be a whole name.

## Templates

`docker-compose.yml.j2` is rendered in a Jinja2 sandbox with strict undefined
variables (PLAN NOTE-30). It sees:

| Variable | Value |
|---|---|
| `stack` | The install's name (`grafana`, `grafana-2`, …). |
| `env` | The resolved variables, by name. |
| `ports` | Host port by port `id`. |
| `host_ip` | The computer's LAN address (empty without a network). |
| `hostname` | The computer's name. |
| `timezone` | The computer's time zone, e.g. `Europe/Copenhagen` (`Etc/UTC` if unknown). |
| `share_gid` | The gid of `nimbus-smb`, the group every share's files belong to, for apps that write into shares. |

Every value goes through the `q` filter, which writes it as a quoted string
(`$` doubled, so Compose doesn't interpolate it): `TZ: {{ timezone | q }}`.
Numbers that must be strings use `{{ share_gid | string | q }}`. Nothing a
user types can add YAML.

Start the file with a `{# … #}` comment that says what the app is, where its
setup comes from (the vendor's install guide), and what was left out and why.

The template defines `services:` with at least `main_service`. For that
service the **engine** then sets, from the install's settings:

- `ports` (bridge mode), or `network_mode: host` without ports or `hostname`;
- `volumes`: each manifest volume, plus the `host_paths`;
- `environment`: the template's values first, then the install's variables
  (which win), sorted;
- `restart` (`unless-stopped` by default) and the project `name:`.

So a template sets only what the user can't: the image, `command`,
`container_name` (with `single_instance`), `init`, `stop_grace_period`,
fixed variables, extra services. In bridge mode the service joins the app's
own network, unless the template says `network_mode: "bridge"` (Docker's
default bridge, as Nextcloud AIO needs). Every bind mount, in any service,
must stay inside the app's folder, a share, or a declared host path.

**Images** are written in full (`docker.io/…`, `ghcr.io/…`) with the tag the
vendor's own install guide uses (`lts`, `stable`, `2`, `latest`), so Update
(pull and recreate) brings the vendor's current release.
`tests/integration/catalog-images.sh` checks that each image and tag exists
for linux/amd64.

## Changing an app

Raise `template_version` whenever the template, a default, a port or a volume
changes. Installed apps are never changed by a catalog update: the Control
Center shows "Template update available" with a diff, and applies it only
when the user asks (PLAN NOTE-60). Never reuse a port or volume `id` for
something else, since installed apps keep their settings by id.

## Checks

- `packages/nimbus-catalog/debian/check-catalog.py` (package build): the
  folder layout, no links, the schema, `id` equals the folder name, and
  `template_version`.
- `control-center/tests/test_catalog.py` (pytest): every app passes the
  schema and the parser, the two agree on a list of broken manifests, each
  template renders for a 1-click install, values go through `q`, binds stay
  in allowed places, and icons are plain SVG.
- `control-center/tests/integration/catalog-images.sh`: every image exists
  for linux/amd64, and `docker compose config` accepts every rendered file.
