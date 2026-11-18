"""Lists every installed Python distribution with its declared licence and flags anything not permissive."""
import json
import re
import sys
from importlib import metadata

PERMISSIVE = re.compile(r"\b(MIT|BSD|Apache|ISC|PSF|Python Software Foundation|Zlib|Unlicense|CC0|HPND|MIT-CMU|"
                        r"Public Domain|PIL|Historical Permission|0BSD|BlueOak|UPL)\b", re.I)
RESTRICTIVE = re.compile(r"\b(AGPL|GPL|LGPL|MPL|Mozilla|SSPL|EUPL|CDDL|EPL|Commons Clause|RSAL|Affero)\b", re.I)


def licence_of(dist) -> str:
    meta = dist.metadata
    expr = meta.get("License-Expression") or ""
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    field = (meta.get("License") or "").strip()
    if len(field) > 80:  # some packages paste the whole licence text
        field = field.splitlines()[0][:80]
    return "; ".join(x for x in [expr, ", ".join(classifiers), field] if x) or "UNKNOWN"


rows = []
for dist in metadata.distributions():
    name = dist.metadata["Name"]
    lic = licence_of(dist)
    if RESTRICTIVE.search(lic) and not re.search(r"\b(or|OR)\b.*\b(MIT|BSD|Apache)\b|\b(MIT|BSD|Apache)\b.*\b(or|OR)\b", lic):
        status = "REVIEW"
    elif PERMISSIVE.search(lic):
        status = "ok"
    else:
        status = "UNKNOWN"
    rows.append({"package": name, "version": dist.version, "license": lic, "status": status})
rows.sort(key=lambda r: (r["status"] == "ok", r["package"].lower()))
if "--json" in sys.argv:
    print(json.dumps(rows, indent=1))
else:
    for r in rows:
        if r["status"] != "ok":
            print(f"{r['status']:8s} {r['package']:32s} {r['version']:14s} {r['license'][:110]}")
    print(f"-- {len(rows)} packages, {sum(r['status'] == 'ok' for r in rows)} clearly permissive")
