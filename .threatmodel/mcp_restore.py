#!/usr/bin/env python3
# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Restore this threat model into the threat-modeling MCP server.

The awslabs threat-modeling-mcp-server keeps its model in memory only and has
no import action, so every new session starts empty. This script rebuilds the
server state from the committed <base>.extended.json snapshot by replaying it
through the server's own MCP tools (in-process, the same validation as a live
session), then either serves MCP over stdio or verifies the round trip.

It must run inside the server's environment, so launch it through uvx:

    uvx --from git+https://github.com/awslabs/threat-modeling-mcp-server.git \
        python .threatmodel/mcp_restore.py serve --project "$PWD"

Modes:
  serve   Restore, select the project, then run the MCP server on stdio.
          Point the Kiro MCP config for threat-modeling-mcp-server at this.
          Restore messages go to stderr; stdout is the MCP channel.
  verify  Restore, export under a temporary -roundtrip name (moved out of
          the repo and deleted afterwards), and compare it with the committed
          .md / .tc.json / .extended.json. Exit 1 on any difference. Run it
          after any change, and never hand-edit the model files: make the
          change through the MCP tools and re-export instead.

Code validation (phase 7.5) findings are session-only: the server does not
export them, so a restored model reports phase 7.5 as not done.

Ids are positional: the server issues T001, T002, ... in add order and never
accepts an explicit id, so records are replayed in id order. Assets (A001...)
and assumptions (A011...) share the "A" counter, so assets go first.

After changing the model in a live session, call export_threat_model with
output_path "pqc-iot-workshop-threat-model", then run add_tc_metadata.py, so
the snapshot (and the next restore) carries the change.
"""
import argparse
import asyncio
import contextlib
import difflib
import io
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import add_tc_metadata  # noqa: E402  (sibling module in .threatmodel/)

HERE = Path(__file__).resolve().parent
BASE = "pqc-iot-workshop-threat-model"


def log(msg):
    print(f"[mcp_restore] {msg}", file=sys.stderr, flush=True)


def strip(rec, *drop):
    """Copy a record without its id, the given keys, and None values."""
    return {k: v for k, v in rec.items() if k != "id" and k not in drop and v is not None}


def num(rid):
    return int(re.sub(r"\D", "", rid))


class Restorer:
    def __init__(self, mcp, snap):
        self.mcp, self.s = mcp, snap

    async def call(self, tool, **args):
        res = await self.mcp.call_tool(tool, args)
        blocks = res[0] if isinstance(res, tuple) else res
        text = "\n".join(getattr(b, "text", str(b)) for b in blocks)
        if re.search(r"(?im)^\s*(\*\*)?(error|failed|invalid)\b|validation error", text):
            raise RuntimeError(f"{tool}({args.get('action')}/{args.get('section')}) failed:\n{text[:1500]}")
        return text

    async def add_in_order(self, tool, section, records, drop=(), prefix=None, listing=None):
        """Batch-add records in id order and check the server issued the same ids."""
        recs = sorted(records, key=lambda r: num(r["id"]))
        if not recs:
            return
        kw = {"section": section} if section else {}
        await self.call(tool, action="add", items=[strip(r, *drop) for r in recs], **kw)
        if listing:
            got = await listing()
            missing = [r["id"] for r in recs if r["id"] not in got]
            if missing:
                raise RuntimeError(f"{tool}/{section}: ids not issued as expected: {missing[:5]}")

    async def restore(self, project):
        s = self.s
        await self.call("manage_workflow", action="set_project", directory=str(project))

        # Phase 1: business, software, data assets, personas, NFRs in one set.
        feats = dict(s["businessContext"]["features"])
        geo = feats.pop("geographic_profile", {}) or {}
        business = {"description": s["businessContext"]["description"], **feats, **geo}
        personas = sorted(s["userPersonas"], key=lambda r: num(r["id"]))
        await self.call("manage_system_context", action="set", section="all", values={
            "business": business,
            "software": strip(s["softwareProfile"]),
            "data_assets": [strip(r) for r in sorted(s["dataAssetProfiles"], key=lambda r: num(r["id"]))],
            "user_personas": [strip(r, "is_relevant") for r in personas],
            "nfrs": s["nonFunctionalRequirements"],
        })
        not_rel = [{"id": p["id"], "is_relevant": False} for p in personas if p.get("is_relevant") is False]
        if not_rel:
            await self.call("manage_system_context", action="update", section="user_personas", items=not_rel)

        # Phase 2: architecture.
        await self.add_in_order("manage_architecture", "components", s["components"])
        await self.add_in_order("manage_architecture", "data_stores", s["dataStores"])
        await self.add_in_order("manage_architecture", "connections", s["connections"])

        # Phase 3: threat actors. The server preloads a default library, so
        # update ids that already exist and add the rest in order.
        listing = await self.call("manage_threat_actors", action="list")
        existing = set(re.findall(r"^## (TA\d+):", listing, re.M))
        actors = sorted(s["threatActors"], key=lambda r: num(r["id"]))
        upd = [{"id": a["id"], **strip(a, "reviewed")} for a in actors if a["id"] in existing]
        new = [strip(a, "reviewed") for a in actors if a["id"] not in existing]
        if upd:
            await self.call("manage_threat_actors", action="update", items=upd)
        if new:
            await self.call("manage_threat_actors", action="add", items=new)
        extra = sorted(existing - {a["id"] for a in actors})
        if extra:
            await self.call("manage_threat_actors", action="delete", item_ids=extra)

        # Phase 4: trust zones (+ nodes), crossing points (+ connections), boundaries.
        zones = sorted(s["trustZones"], key=lambda r: num(r["id"]))
        await self.call("manage_trust_boundaries", action="add", section="zones",
                        items=[strip(z, "contained_nodes") for z in zones])
        zl = [{"zone_id": z["id"], "node_id": n} for z in zones for n in z.get("contained_nodes", [])]
        if zl:
            await self.call("manage_trust_boundaries", action="link", section="zones", items=zl)
        cps = sorted(s["crossingPoints"], key=lambda r: num(r["id"]))
        await self.call("manage_trust_boundaries", action="add", section="crossing_points",
                        items=[strip(c, "connection_ids") for c in cps])
        cl = [{"crossing_point_id": c["id"], "connection_id": n} for c in cps for n in c.get("connection_ids", [])]
        if cl:
            await self.call("manage_trust_boundaries", action="link", section="crossing_points", items=cl)
        tbs = sorted(s["trustBoundaries"], key=lambda r: num(r["id"]))
        await self.call("manage_trust_boundaries", action="add", section="boundaries", items=[
            {**strip(b, "crossing_points"), "crossing_point_ids": b.get("crossing_points", [])} for b in tbs])

        # Phase 5: assets, then assumptions (shared "A" counter), then flows.
        await self.add_in_order("manage_asset_flows", "assets", s["assets"])
        await self.add_in_order("manage_assumptions", None, s["assumptions"])
        await self.add_in_order("manage_asset_flows", "flows", s["flows"])

        # Phase 6-8: threats, mitigations, links, residual risk.
        threats = sorted(s["threats"], key=lambda r: num(r["id"]))
        await self.call("manage_threats", action="add", section="threats", items=[{
            "threat_source": t["threatSource"], "prerequisites": t["prerequisites"],
            "threat_action": t["threatAction"], "threat_impact": t["threatImpact"],
            "category": t.get("category"), "severity": t.get("severity"),
            "likelihood": t.get("likelihood"),
            "affected_components": t.get("affected_components", []),
            "affected_assets": t.get("impactedAssets", []), "tags": t.get("tags", []),
        } for t in threats])
        st = [{"id": t["id"], "status": t["status"]} for t in threats if t.get("status") != "threatIdentified"]
        if st:
            await self.call("manage_threats", action="update", section="threats", items=st)
        mits = sorted(s["mitigations"], key=lambda r: num(r["id"]))
        await self.call("manage_threats", action="add", section="mitigations", items=[
            strip(m, "numericId", "displayOrder") for m in mits])
        links = [{"mitigation_id": l["mitigationId"], "threat_id": l["linkedId"]} for l in s["mitigationLinks"]]
        if links:
            await self.call("manage_threats", action="link", section="mitigations", items=links)
        rra = [{"threat_id": r["threat_id"], "decision": r["decision"],
                "residual_severity": r.get("residual_severity"),
                "residual_likelihood": r.get("residual_likelihood"),
                "rationale": r["rationale"]} for r in s["residualRiskAssessments"] if r.get("is_current", True)]
        if rra:
            await self.call("manage_threats", action="assess", section="threats",
                            items=[{k: v for k, v in r.items() if v is not None} for r in rra])
        log(f"restored {len(threats)} threats, {len(mits)} mitigations, "
            f"{len(s['components'])} components, {len(s['connections'])} connections")


def load_server():
    try:
        from threat_modeling_mcp_server.server import mcp  # noqa: WPS433
    except ImportError:
        sys.exit("[mcp_restore] run this inside the server env: uvx --from "
                 "git+https://github.com/awslabs/threat-modeling-mcp-server.git python " + __file__)
    return mcp


def normalise(path):
    """Text of an export with volatile values (timestamps, export paths) masked."""
    text = Path(path).read_text(encoding="utf-8")
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?", "<ts>", text)
    return text.replace(BASE + "-roundtrip", BASE)


def verify(snapshot, project):
    """Restore against the real project (so phase 7.5 applicability matches),
    export under a -roundtrip base name, compare, then delete that export."""
    mcp = load_server()
    snap = json.loads(Path(snapshot).read_text(encoding="utf-8"))
    out = Path(project) / ".threatmodel"
    tmp = Path(tempfile.mkdtemp(prefix="tm-roundtrip-"))
    rt = [out / f"{BASE}-roundtrip{e}" for e in (".tc.json", ".md", ".extended.json")]
    try:
        asyncio.run(Restorer(mcp, snap).restore(Path(project)))
        asyncio.run(Restorer(mcp, snap).call(
            "export_threat_model", output_path=BASE + "-roundtrip", include_extended_data=True))
        for f in rt:  # move the export out of the repo before anything can fail
            shutil.move(str(f), str(tmp / f.name))
        out = tmp
        # In-process (no subprocess): add Priority/STRIDE to the round-trip export.
        with contextlib.redirect_stdout(io.StringIO()):
            add_tc_metadata.apply_metadata(str(out), BASE + "-roundtrip")
        bad = 0
        for ext in (".tc.json", ".md", ".extended.json"):
            a = normalise(HERE / f"{BASE}{ext}").splitlines()
            b = normalise(out / f"{BASE}-roundtrip{ext}").splitlines()
            diff = list(difflib.unified_diff(a, b, f"committed{ext}", f"restored{ext}", n=1, lineterm=""))
            print(f"{ext:15} {'identical' if not diff else f'{len(diff)} diff lines'}")
            if diff:
                bad += 1
                print("\n".join(d[:240] for d in diff[:60]))
        return 1 if bad else 0
    finally:
        for f in rt:
            f.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)


def serve(snapshot, project):
    mcp = load_server()
    snap_path = Path(snapshot)
    if snap_path.exists():
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
        try:
            asyncio.run(Restorer(mcp, snap).restore(Path(project)))
        except Exception as exc:  # serve anyway; the session can still rebuild by hand
            log(f"restore FAILED, serving an empty model: {exc}")
    else:
        log(f"no snapshot at {snap_path}; serving an empty model")
    mcp.run()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["serve", "verify"])
    ap.add_argument("--snapshot", default=str(HERE / f"{BASE}.extended.json"))
    ap.add_argument("--project", default=str(HERE.parent))
    a = ap.parse_args()
    sys.exit(verify(a.snapshot, a.project) if a.mode == "verify" else serve(a.snapshot, a.project))


if __name__ == "__main__":
    main()
