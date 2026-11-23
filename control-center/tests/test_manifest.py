# =============================================================================
# control-center/tests/test_manifest.py: the catalog contract
# =============================================================================
# What: - the fixture manifests parse, with every default filled in;
#       - each kind of broken manifest is refused with the app and the field
#         named (ids, template_version, ports, volume paths and owners,
#         share_hint, env generators, host_paths, webui); test_catalog.py
#         checks the stricter rules (unknown keys, strings for booleans,
#         webui.scheme, single_instance, companions) against the schema;
#       - Catalog: loads the apps, lists broken ones in .errors without
#         hiding the others, skips symbolic links, picks up a changed or
#         removed app without a restart (NOTE-60), and applies
#         manifest.schema.json when python3-jsonschema is there.
# Why:  deliverable 9's catalog and manifest.schema.json follow this contract;
#       the deploy engine trusts nothing parse_manifest hasn't checked.
# =============================================================================

import json
import os
import shutil

import pytest

from conftest import FIXTURE_CATALOG
from nimbus_control.catalog import Catalog
from nimbus_control.manifest import ManifestError, parse_manifest


def load(app):
    return json.loads((FIXTURE_CATALOG / app / "manifest.json").read_text())


def test_fixture_manifests_parse_with_defaults():
    web = parse_manifest(load("demo-web"), "demo-web")
    assert web["network_mode"] == "bridge" and web["template_version"] == 2 and web["main_service"] == "web"
    assert web["ports"] == [{"id": "http", "container": 80, "host": 8080, "protocol": "tcp", "bind": "all",
                             "fixed": False, "description": "Web page"}]
    config, media = web["volumes"]
    assert config == {"id": "config", "container": "/config", "kind": "bind", "default": "config", "share_hint": None,
                      "owner": {"uid": 472, "gid": 472}, "description": "Settings"}
    assert media["share_hint"] == "Media" and media["owner"] is None
    assert web["env"][1] == {"name": "ADMIN_PASSWORD", "default": None, "required": True, "secret": True,
                             "generate": "password", "description": "First login"}
    assert web["webui"] == {"port": "http", "path": "/", "scheme": "http", "secure_context": False}
    assert web["host_paths"] == []

    dns = parse_manifest(load("demo-dns"), "demo-dns")
    assert [(p["bind"], p["fixed"], p["protocol"]) for p in dns["ports"]] == [("lan", True, "udp"), ("lan", True, "tcp"),
                                                                             ("all", False, "tcp")]
    assert dns["volumes"][0]["kind"] == "named" and dns["volumes"][0]["default"] == "dnsdata"

    host = parse_manifest(load("demo-host"), "demo-host")
    assert host["network_mode"] == "host" and host["notes"] == "" and host["env"] == []
    assert host["host_paths"] == [{"host": "/var/run/docker.sock", "container": "/var/run/docker.sock", "read_only": True}]
    assert host["webui"]["secure_context"] is True


BAD = [
    (lambda m: m.update(id="other"), "must equal the folder name"),
    (lambda m: m.pop("name"), '"name" must be a non-empty string'),
    (lambda m: m.update(template_version="2"), "template_version"),
    (lambda m: m.update(template_version=True), "template_version"),
    (lambda m: m.update(template_version=0), "template_version"),
    (lambda m: m.update(main_service="Web Server"), "main_service"),
    (lambda m: m.update(network_mode="macvlan"), "network_mode"),
    (lambda m: m["ports"].append(dict(m["ports"][0])), "port id http is used twice"),
    (lambda m: m["ports"][0].update(host=70000), "1..65535"),
    (lambda m: m["ports"][0].update(protocol="sctp"), "protocol is tcp or udp"),
    (lambda m: m["ports"][0].update(bind="wan"), "bind is all or lan"),
    (lambda m: m["volumes"][0].update(default="../../etc"), "'.' and '..' are not allowed"),
    (lambda m: m["volumes"][0].update(default="/etc"), "Folders inside an app"),
    (lambda m: m["volumes"][0].update(container="config"), "Container paths are absolute"),
    (lambda m: m["volumes"][0].update(owner={"uid": -1}), "owner needs uid"),
    (lambda m: m["volumes"][0].update(owner={"uid": "472"}), "owner needs uid"),
    (lambda m: m["volumes"][1].update(kind="named", default="media"), "share_hint needs kind bind"),
    (lambda m: m["volumes"][1].update(kind="tmpfs"), "kind is bind or named"),
    (lambda m: m["env"][0].update(name="1TZ"), 'needs a valid "name"'),
    (lambda m: m["env"].append(dict(m["env"][0])), "env TZ is listed twice"),
    (lambda m: m["env"][1].update(generate="uuid"), "generate is one of"),
    (lambda m: m["env"][0].update(default=5), "default is a string"),
    (lambda m: m.update(host_paths=[{"host": "var/run/docker.sock", "container": "/x"}]), 'absolute "host" path'),
    (lambda m: m.update(host_paths=[{"host": "/run/x", "container": "x"}]), "Container paths are absolute"),
    (lambda m: m.update(webui={"port": "https"}), "webui.port must name one of the app's ports"),
    (lambda m: m.update(webui={"port": "http", "path": "admin"}), "webui.path starts with /"),
]


@pytest.mark.parametrize("change, text", BAD)
def test_bad_manifests_are_refused(change, text):
    data = load("demo-web")
    change(data)
    with pytest.raises(ManifestError) as err:
        parse_manifest(data, "demo-web")
    assert text in str(err.value)
    assert str(err.value).startswith("Catalog app 'demo-web': ")


def test_folder_name_must_be_an_app_id():
    data = {**load("demo-web"), "id": "Demo_Web"}
    with pytest.raises(ManifestError):
        parse_manifest(data, "Demo_Web")


# --- the catalog ----------------------------------------------------------------------
@pytest.fixture
def catalog_dir(tmp_path):
    root = tmp_path / "catalog"
    shutil.copytree(FIXTURE_CATALOG, root)
    return root


def test_catalog_lists_good_apps_and_reports_broken_ones(catalog_dir):
    (catalog_dir / "broken").mkdir()
    (catalog_dir / "broken" / "manifest.json").write_text("{not json")
    (catalog_dir / "old").mkdir()
    (catalog_dir / "old" / "manifest.json").write_text(json.dumps({**load("demo-web"), "id": "old", "template_version": 0}))
    (catalog_dir / "notemplate").mkdir()
    (catalog_dir / "notemplate" / "manifest.json").write_text(json.dumps({**load("demo-web"), "id": "notemplate"}))
    os.symlink(catalog_dir / "demo-web", catalog_dir / "linked")
    (catalog_dir / "README").write_text("not an app")
    cat = Catalog(str(catalog_dir))
    assert sorted(cat.apps()) == ["demo-dns", "demo-host", "demo-web"]
    assert len(cat.errors) == 3
    assert any("broken" in e for e in cat.errors) and any("Catalog app 'old'" in e for e in cat.errors)
    assert any("notemplate" in e for e in cat.errors)
    web = cat.get("demo-web")
    assert web["icon"] == str(catalog_dir / "demo-web" / "icon.svg") and "services:" in web["template"]
    assert cat.get("demo-host")["icon"] is None and cat.get("linked") is None


def test_catalog_picks_up_changes_without_a_restart(catalog_dir):
    cat = Catalog(str(catalog_dir))
    assert cat.get("demo-web")["manifest"]["template_version"] == 2
    data = load("demo-web")
    data["template_version"] = 3
    (catalog_dir / "demo-web" / "manifest.json").write_text(json.dumps(data, indent=4))
    assert cat.get("demo-web")["manifest"]["template_version"] == 3
    shutil.rmtree(catalog_dir / "demo-dns")
    assert cat.get("demo-dns") is None and sorted(cat.apps()) == ["demo-host", "demo-web"]


def test_missing_catalog_is_empty(tmp_path):
    cat = Catalog(str(tmp_path / "nothing"))
    assert cat.apps() == {} and cat.errors == []


def test_catalog_applies_the_json_schema(catalog_dir):
    pytest.importorskip("jsonschema")
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
              "properties": {"category": {"enum": ["Media", "Network"]}}}
    (catalog_dir / "manifest.schema.json").write_text(json.dumps(schema))
    cat = Catalog(str(catalog_dir))
    assert sorted(cat.apps()) == ["demo-dns", "demo-web"]
    assert len(cat.errors) == 1 and cat.errors[0].startswith("Catalog app 'demo-host': schema: ")
