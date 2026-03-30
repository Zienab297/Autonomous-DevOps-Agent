"""
core/interactive_cli.py
------------------------
Interactive CLI for querying all persisted DevOps data in real time.

Works with BOTH the SQLite DatabaseManager and the PostgreSQL
PostgreSQLDatabaseManager — any object that exposes the same query API.

Usage:
    from core.interactive_cli import InteractiveCLI
    cli = InteractiveCLI(db=orchestrator.db, project_id="my-project")
    cli.run()

Or via devops.py menu option [2]:
    devops → [2] Open chat agent / [4] Query history
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ── ANSI colours ──────────────────────────────────────────────────────────────
_R  = "\033[0m"
_B  = "\033[1m"
_D  = "\033[2m"
_CY = "\033[36m"
_GR = "\033[32m"
_YL = "\033[33m"
_RD = "\033[31m"

_SEV = {
    "critical": f"{_B}\033[31m",
    "high"    : f"{_B}\033[31m",
    "medium"  : f"{_B}\033[33m",
    "low"     : f"{_B}\033[32m",
}

_STA = {
    "open"         : f"{_YL}",
    "investigating": f"{_YL}",
    "remediating"  : f"{_YL}",
    "resolved"     : f"{_GR}",
    "failed"       : f"{_RD}",
    "success"      : f"{_GR}",
    "pending"      : f"{_D}",
    "running"      : f"{_CY}",
}


def _c(text: str, colour: str) -> str:
    return f"{colour}{text}{_R}"


def _ts(ts_str: Optional[str]) -> str:
    if not ts_str:
        return "—"
    try:
        dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts_str)[:19]


def _trunc(text: Optional[str], n: int = 60) -> str:
    if not text:
        return "—"
    s = str(text)
    return s[:n] + "…" if len(s) > n else s


# ── Printer helpers ───────────────────────────────────────────────────────────

class _Table:
    """Simple fixed-width ASCII table printer."""

    def __init__(self, headers: List[str], widths: List[int]):
        self.headers = headers
        self.widths  = widths

    def _row(self, cells: List[str]) -> str:
        parts = []
        for cell, w in zip(cells, self.widths):
            s = str(cell) if cell is not None else "—"
            # strip ANSI for width calc
            import re
            plain = re.sub(r"\033\[[0-9;]*m", "", s)
            pad   = max(0, w - len(plain))
            parts.append(s + " " * pad)
        return "  " + "  ".join(parts)

    def print_header(self):
        print()
        print(self._row([f"{_B}{h}{_R}" for h in self.headers]))
        print("  " + "  ".join("─" * w for w in self.widths))

    def print_row(self, cells: List[str]):
        print(self._row(cells))


# ── InteractiveCLI ────────────────────────────────────────────────────────────

class InteractiveCLI:
    """
    Interactive query interface for the DevOps Agent history database.

    Compatible with both DatabaseManager (SQLite) and
    PostgreSQLDatabaseManager (PostgreSQL) — any object that has:
        get_all_incidents(), get_active_incidents(),
        get_solutions_for_incident(), get_actions_for_incident(),
        get_all_deployments(), get_events(), get_alerts_for_incident()
    """

    def __init__(self, db, project_id: str = ""):
        self.db         = db
        self.project_id = project_id or getattr(db, "project_id", getattr(db, "project", "?"))

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self) -> None:
        """Main loop — shows menu, dispatches to handlers."""
        print(f"\n{'═'*60}")
        print(f"  {_B}{_CY}DevOps History — Interactive Query Mode{_R}")
        print(f"  Project: {_B}{self.project_id}{_R}")
        print(f"{'═'*60}")

        while True:
            self._print_menu()
            try:
                choice = input("  Choose [1-8]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n  Exiting interactive mode.")
                break

            if   choice == "1": self._show_incidents()
            elif choice == "2": self._show_event_logs()
            elif choice == "3": self._show_solutions()
            elif choice == "4": self._show_actions()
            elif choice == "5": self._show_deployments()
            elif choice == "6": self._show_summary()
            elif choice == "7": self._show_full_incident_report()
            elif choice == "8":
                print(f"\n  {_D}Exiting interactive mode.{_R}\n")
                break
            else:
                print(f"  {_YL}Invalid choice — enter 1 to 8.{_R}")

    # ── Menu ──────────────────────────────────────────────────────────────────

    def _print_menu(self) -> None:
        print(f"\n{'─'*60}")
        print(f"  {_B}[1]{_R}  Recent incidents")
        print(f"  {_B}[2]{_R}  Event logs")
        print(f"  {_B}[3]{_R}  Solutions")
        print(f"  {_B}[4]{_R}  Remediation actions")
        print(f"  {_B}[5]{_R}  Deployments")
        print(f"  {_B}[6]{_R}  Summary")
        print(f"  {_B}[7]{_R}  Full Incident Report  (complete flow per incident)")
        print(f"  {_B}[8]{_R}  Exit")
        print(f"{'─'*60}")

    # ── 1. Incidents ──────────────────────────────────────────────────────────

    def _show_incidents(self) -> None:
        filter_opt = self._ask_filter("service name (or Enter for all)")

        try:
            rows: List[Dict] = self.db.get_all_incidents()
        except Exception as e:
            print(f"  {_RD}DB error: {e}{_R}")
            return

        if filter_opt:
            rows = [r for r in rows if filter_opt.lower() in str(r.get("service", "")).lower()]

        if not rows:
            print(f"  {_D}No incidents found.{_R}")
            return

        t = _Table(
            headers=["ID",        "Service",    "Severity", "Status",   "Description",     "Created"],
            widths  =[18,         22,           10,         14,         35,                 19],
        )
        t.print_header()
        for r in rows[:50]:
            sev = str(r.get("severity", ""))
            sta = str(r.get("status",   ""))
            t.print_row([
                _trunc(str(r.get("id", r.get("incident_id", ""))), 18),
                _trunc(str(r.get("service", "")), 22),
                _c(sev.upper(), _SEV.get(sev, "")),
                _c(sta,         _STA.get(sta, "")),
                _trunc(str(r.get("description", "")), 35),
                _ts(r.get("created_at")),
            ])

        print(f"\n  {_D}Showing {min(len(rows), 50)} of {len(rows)} incidents.{_R}")
        self._offer_detail("incident", rows)

    # ── 2. Event logs ─────────────────────────────────────────────────────────

    def _show_event_logs(self) -> None:
        filter_opt = self._ask_filter("event type (or Enter for all)")
        try:
            rows = self.db.get_events(event_type=filter_opt or None, limit=100)
        except Exception as e:
            print(f"  {_RD}DB error: {e}{_R}")
            return

        if not rows:
            print(f"  {_D}No events found.{_R}")
            return

        t = _Table(
            headers=["Type",                      "Source",    "Incident",  "Time"],
            widths  =[35,                          20,          18,          19],
        )
        t.print_header()
        for r in rows[:100]:
            t.print_row([
                _trunc(str(r.get("type", r.get("event_type", ""))), 35),
                _trunc(str(r.get("source", "")), 20),
                _trunc(str(r.get("incident_id", "") or ""), 18),
                _ts(r.get("created_at")),
            ])
        print(f"\n  {_D}Showing {min(len(rows), 100)} events.{_R}")

    # ── 3. Solutions ──────────────────────────────────────────────────────────

    def _show_solutions(self) -> None:
        inc_id = self._ask_filter("incident ID (or Enter for recent all)")
        try:
            if inc_id:
                rows = self.db.get_solutions_for_incident(inc_id)
            else:
                # Try list_recent if available
                if hasattr(self.db, "_run"):
                    from repositories.solution_repo import SolutionRepository
                    from db.session import get_session
                    with get_session() as s:
                        items = SolutionRepository(s).list_recent(self.project_id, limit=30)
                    rows = [_orm_to_dict(i) for i in items]
                else:
                    all_inc = self.db.get_all_incidents()
                    rows = []
                    for inc in all_inc[:10]:
                        iid = inc.get("id") or inc.get("incident_id", "")
                        rows.extend(self.db.get_solutions_for_incident(iid))
        except Exception as e:
            print(f"  {_RD}DB error: {e}{_R}")
            return

        if not rows:
            print(f"  {_D}No solutions found.{_R}")
            return

        t = _Table(
            headers=["ID", "Incident",   "Source",     "Confidence", "Preview",            "Created"],
            widths  =[6,   18,           16,           10,           40,                   19],
        )
        t.print_header()
        for r in rows[:30]:
            conf = r.get("confidence")
            conf_str = f"{float(conf):.2f}" if conf is not None else "—"
            t.print_row([
                str(r.get("id", "")),
                _trunc(str(r.get("incident_id", "")), 18),
                _trunc(str(r.get("source", "")), 16),
                conf_str,
                _trunc(str(r.get("content", r.get("healing_prompt", ""))), 40),
                _ts(r.get("created_at")),
            ])
        print(f"\n  {_D}Showing {min(len(rows), 30)} solutions.{_R}")

    # ── 4. Actions ────────────────────────────────────────────────────────────

    def _show_actions(self) -> None:
        inc_id = self._ask_filter("incident ID (or Enter for recent all)")
        try:
            if inc_id:
                rows = self.db.get_actions_for_incident(inc_id)
            else:
                if hasattr(self.db, "_run"):
                    from repositories.action_repo import ActionRepository
                    from db.session import get_session
                    with get_session() as s:
                        items = ActionRepository(s).list_recent(self.project_id, limit=30)
                    rows = [_orm_to_dict(i) for i in items]
                else:
                    all_inc = self.db.get_all_incidents()
                    rows = []
                    for inc in all_inc[:10]:
                        iid = inc.get("id") or inc.get("incident_id", "")
                        rows.extend(self.db.get_actions_for_incident(iid))
        except Exception as e:
            print(f"  {_RD}DB error: {e}{_R}")
            return

        if not rows:
            print(f"  {_D}No actions found.{_R}")
            return

        t = _Table(
            headers=["ID", "Incident",   "Status",  "Command",                    "Created"],
            widths  =[6,   18,           12,        45,                           19],
        )
        t.print_header()
        for r in rows[:30]:
            sta = str(r.get("status", ""))
            t.print_row([
                str(r.get("id", r.get("action_id", ""))),
                _trunc(str(r.get("incident_id", "")), 18),
                _c(sta, _STA.get(sta, "")),
                _trunc(str(r.get("command", "")), 45),
                _ts(r.get("created_at")),
            ])
        print(f"\n  {_D}Showing {min(len(rows), 30)} actions.{_R}")

    # ── 5. Deployments ────────────────────────────────────────────────────────

    def _show_deployments(self) -> None:
        filter_opt = self._ask_filter("service name (or Enter for all)")
        try:
            if filter_opt:
                rows = self.db.get_deployments_for_service(filter_opt)
            else:
                rows = self.db.get_all_deployments()
        except Exception as e:
            print(f"  {_RD}DB error: {e}{_R}")
            return

        if not rows:
            print(f"  {_D}No deployments found.{_R}")
            return

        t = _Table(
            headers=["ID",          "Service",    "Branch",     "Status",   "Pipeline URL",      "Created"],
            widths  =[18,           22,           18,           12,         35,                  19],
        )
        t.print_header()
        for r in rows[:30]:
            sta = str(r.get("status", ""))
            t.print_row([
                _trunc(str(r.get("id", r.get("deployment_id", ""))), 18),
                _trunc(str(r.get("service", "")), 22),
                _trunc(str(r.get("branch", "")), 18),
                _c(sta, _STA.get(sta, "")),
                _trunc(str(r.get("pipeline_url", "") or ""), 35),
                _ts(r.get("created_at")),
            ])
        print(f"\n  {_D}Showing {min(len(rows), 30)} deployments.{_R}")

    # ── 6. Summary ────────────────────────────────────────────────────────────

    def _show_summary(self) -> None:
        try:
            if hasattr(self.db, "get_summary"):
                s = self.db.get_summary()
            else:
                s = {}

            incidents   = self.db.get_all_incidents()
            deployments = self.db.get_all_deployments()

            print(f"\n{'═'*55}")
            print(f"  {_B}Project:{_R} {self.project_id}")
            print(f"{'─'*55}")
            print(f"  Incidents    : {_B}{len(incidents)}{_R}  "
                  f"({_YL}{sum(1 for i in incidents if i.get('status') == 'open')} open{_R} / "
                  f"{_GR}{sum(1 for i in incidents if i.get('status') == 'resolved')} resolved{_R})")
            print(f"  Deployments  : {_B}{len(deployments)}{_R}  "
                  f"({_GR}{sum(1 for d in deployments if d.get('status') == 'success')} succeeded{_R} / "
                  f"{_RD}{sum(1 for d in deployments if d.get('status') in ('failed','failure'))} failed{_R})")
            print(f"{'═'*55}\n")
        except Exception as e:
            print(f"  {_RD}DB error: {e}{_R}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _ask_filter(self, prompt: str) -> str:
        try:
            val = input(f"  Filter by {prompt}: ").strip()
            return val
        except (EOFError, KeyboardInterrupt):
            return ""

    # ── 7. Full Incident Report ──────────────────────────────────────────────

    def _show_full_incident_report(self) -> None:
        """
        Prints a complete report per incident:
        metadata → timeline → solutions → actions → outcome.
        """
        W = 70

        try:
            all_incidents: List[Dict] = self.db.get_all_incidents()
        except Exception as e:
            print(f"  {_RD}DB error: {e}{_R}")
            return

        if not all_incidents:
            print(f"  {_D}No incidents found.{_R}")
            return

        filter_svc = self._ask_filter("service name to filter (or Enter for all)")
        if filter_svc:
            all_incidents = [
                r for r in all_incidents
                if filter_svc.lower() in str(r.get("service", "")).lower()
            ]

        if not all_incidents:
            print(f"  {_D}No incidents match that filter.{_R}")
            return

        print(f"\n  {_B}{len(all_incidents)} incident(s) found.{_R}")
        print(f"  {_D}[A] show ALL  or enter a specific Incident ID{_R}")
        try:
            pick = input("  Choice [A / incident-id]: ").strip()
        except (EOFError, KeyboardInterrupt):
            pick = "A"

        if pick.upper() != "A" and pick:
            all_incidents = [
                r for r in all_incidents
                if str(r.get("id", r.get("incident_id", ""))).upper() == pick.upper()
            ]
            if not all_incidents:
                print(f"  {_YL}Incident not found.{_R}")
                return

        for idx, inc in enumerate(all_incidents, 1):
            iid     = str(inc.get("id") or inc.get("incident_id", "?"))
            svc     = str(inc.get("service", ""))
            sev     = str(inc.get("severity", ""))
            sta     = str(inc.get("status", ""))
            desc    = str(inc.get("description", ""))
            created = _ts(inc.get("created_at"))
            updated = _ts(inc.get("updated_at"))
            sev_col = _SEV.get(sev.lower(), "")
            sta_col = _STA.get(sta.lower(), "")

            print("\n" + "=" * W)
            print(f"  {_B}{_CY}INCIDENT {idx}/{len(all_incidents)}{_R}  {_B}{iid}{_R}")
            print("=" * W)
            print(f"  {_B}Service  :{_R} {svc}")
            print(f"  {_B}Severity :{_R} {_c(sev.upper(), sev_col)}")
            print(f"  {_B}Status   :{_R} {_c(sta, sta_col)}")
            print(f"  {_B}Created  :{_R} {created}")
            print(f"  {_B}Updated  :{_R} {updated}")
            print()
            print(f"  {_B}Description:{_R}")
            words    = desc.split()
            buf_line = "    "
            buf_col  = 4
            for word in words:
                if buf_col + len(word) + 1 > W - 2:
                    print(buf_line)
                    buf_line, buf_col = "    " + word + " ", 4 + len(word) + 1
                else:
                    buf_line += word + " "
                    buf_col  += len(word) + 1
            if buf_line.strip():
                print(buf_line)

            # ── Timeline ──────────────────────────────────────────────────
            print("\n  " + "-" * W)
            print(f"  {_B}TIMELINE  (events linked to this incident){_R}")
            print("  " + "-" * W)
            try:
                events = self.db.get_events(incident_id=iid, limit=50)
            except Exception:
                events = []
            if events:
                for ev in reversed(events):
                    ev_type = str(ev.get("type", ""))
                    ev_src  = str(ev.get("source", ""))
                    ev_ts   = _ts(ev.get("created_at"))
                    if "incident" in ev_type.lower():
                        tc = _RD
                    elif "complete" in ev_type.lower() or "resolved" in ev_type.lower():
                        tc = _GR
                    elif "failed" in ev_type.lower():
                        tc = _RD
                    elif "scaffold" in ev_type.lower() or "deploy" in ev_type.lower():
                        tc = _CY
                    else:
                        tc = _D
                    print(f"  {_D}{ev_ts}{_R}  {_c(ev_type, tc)}")
                    if ev_src:
                        print(f"  {' ' * 19}  {_D}from: {ev_src}{_R}")
            else:
                print(f"  {_D}No events linked to this incident.{_R}")

            # ── Solutions ─────────────────────────────────────────────────
            print("\n  " + "-" * W)
            print(f"  {_B}SOLUTIONS GENERATED{_R}")
            print("  " + "-" * W)
            try:
                solutions = self.db.get_solutions_for_incident(iid)
            except Exception:
                solutions = []
            if solutions:
                for si, sol in enumerate(solutions, 1):
                    src      = str(sol.get("source", ""))
                    conf     = sol.get("confidence")
                    conf_str = f"{float(conf)*100:.0f}%" if conf is not None else "—"
                    conf_col = _GR if float(conf or 0) >= 0.7 else _YL
                    sol_text = str(sol.get("content") or sol.get("healing_prompt") or "")
                    print(f"\n  {_B}Solution {si}:{_R}")
                    print(f"    Source     : {_c(src, _CY)}")
                    print(f"    Confidence : {_c(conf_str, conf_col)}")
                    print(f"    Created    : {_ts(sol.get('created_at'))}")
                    print(f"\n    {_B}Content:{_R}")
                    for sol_line in sol_text.splitlines():
                        if sol_line.strip():
                            print(f"      {sol_line[:115]}")
                        else:
                            print()
            else:
                print(f"  {_D}No solutions recorded for this incident.{_R}")
                # Show manual instructions stored in action output
                try:
                    acts_all = self.db.get_actions_for_incident(iid)
                    for ia in acts_all:
                        out = str(ia.get("output") or "")
                        if len(out) > 200:
                            print(f"\n  {_YL}Manual instructions from remediation action:{_R}")
                            for ln in out.splitlines()[:50]:
                                print(f"      {ln[:115]}")
                            if len(out.splitlines()) > 50:
                                print(f"      {_D}... ({len(out.splitlines())-50} more lines){_R}")
                            break
                except Exception:
                    pass

            # ── Remediation actions ───────────────────────────────────────
            print("\n  " + "-" * W)
            print(f"  {_B}REMEDIATION ACTIONS{_R}")
            print("  " + "-" * W)
            try:
                actions = self.db.get_actions_for_incident(iid)
            except Exception:
                actions = []
            if actions:
                for ai, act in enumerate(actions, 1):
                    act_sta = str(act.get("status", ""))
                    act_cmd = str(act.get("command") or "")
                    act_out = str(act.get("output") or "")
                    act_err = str(act.get("error") or "")
                    act_ts  = _ts(act.get("created_at"))
                    print(f"\n  {_B}Action {ai}:{_R}")
                    print(f"    Status  : {_c(act_sta, _STA.get(act_sta, ''))}")
                    print(f"    Time    : {act_ts}")
                    if act_cmd:
                        print(f"    Command : {_trunc(act_cmd, 80)}")
                    if act_out:
                        print(f"    Output  :")
                        for ln in act_out.splitlines()[:10]:
                            print(f"      {ln[:110]}")
                        extra = len(act_out.splitlines()) - 10
                        if extra > 0:
                            print(f"      {_D}... ({extra} more lines){_R}")
                    if act_err:
                        print(f"    {_RD}Error   : {_trunc(act_err, 100)}{_R}")
            else:
                print(f"  {_D}No actions recorded for this incident.{_R}")

            # ── Outcome ───────────────────────────────────────────────────
            print("\n  " + "-" * W)
            print(f"  {_B}OUTCOME{_R}")
            print("  " + "-" * W)
            if sta.lower() == "resolved":
                print(f"  {_GR}{_B}RESOLVED{_R} — incident was automatically remediated.")
            elif sta.lower() == "failed":
                print(f"  {_RD}{_B}FAILED{_R} — remediation was attempted but unsuccessful.")
            elif sta.lower() in ("manual_required", "instructions_only"):
                print(f"  {_YL}{_B}MANUAL ACTION REQUIRED{_R}")
                print(f"  {_D}Follow the manual instructions above to resolve this incident.{_R}")
            else:
                print(f"  {_YL}{_B}{sta.upper()}{_R} — incident is still being tracked.")

            print("\n" + "=" * W)

            if idx < len(all_incidents):
                try:
                    cont = input("  Press Enter for next incident, or 'q' to stop: ").strip().lower()
                    if cont == "q":
                        break
                except (EOFError, KeyboardInterrupt):
                    break

    
    def _offer_detail(self, kind: str, rows: List[Dict]) -> None:
        try:
            choice = input(f"\n  Enter {kind} ID for full detail (or Enter to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not choice:
            return

        row = next(
            (r for r in rows
             if str(r.get("id", r.get("incident_id", ""))) == choice),
            None,
        )
        if not row:
            print(f"  {_YL}Not found.{_R}")
            return

        iid = row.get("id") or row.get("incident_id", choice)
        print(f"\n{'─'*60}")
        print(f"  {_B}Incident:{_R} {iid}")
        print(f"  Service : {row.get('service', '')}")
        print(f"  Severity: {row.get('severity', '')}")
        print(f"  Status  : {row.get('status', '')}")
        print(f"  Created : {_ts(row.get('created_at'))}")
        print(f"  Desc    : {row.get('description', '')}")

        try:
            solutions = self.db.get_solutions_for_incident(iid)
            if solutions:
                print(f"\n  {_B}Solutions ({len(solutions)}):{_R}")
                for sol in solutions:
                    conf = sol.get("confidence")
                    print(f"    [{sol.get('source', '')}] "
                          f"confidence={f'{float(conf):.2f}' if conf is not None else '—'} — "
                          f"{_trunc(str(sol.get('content', sol.get('healing_prompt', ''))), 80)}")

            actions = self.db.get_actions_for_incident(iid)
            if actions:
                print(f"\n  {_B}Actions ({len(actions)}):{_R}")
                for act in actions:
                    print(f"    [{act.get('status', '')}] {_trunc(str(act.get('command', '')), 70)}")
        except Exception:
            pass

        print(f"{'─'*60}")


# ── ORM → dict helper ─────────────────────────────────────────────────────────

def _orm_to_dict(row) -> Dict:
    if row is None:
        return {}
    result = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name, None)
        result[col.name] = str(val) if hasattr(val, "isoformat") else val
    return result