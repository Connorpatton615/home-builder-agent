"""dashboard_refresher.py — Project Status Dashboard refresh.

Reads the latest Tracker sheet's Master Schedule tab, computes dashboard
metrics, and writes them to a Dashboard tab on the same sheet. Also applies
visual formatting (status row colors, date formats) to all data tabs.

Read-only on phase data — only writes to Dashboard tab + formatting. Phase
status updates flow through `status_updater.py` instead.

CLI: hb-dashboard

This is the same logic the watcher invokes once a minute. Running it manually
is useful when you want an immediate refresh without waiting for the watcher.
"""

from home_builder_agent.config import DRIVE_FOLDER_PATH
from home_builder_agent.core.auth import get_credentials
from home_builder_agent.integrations import drive, sheets


def main():
    print("Authenticating with Google...")
    creds = get_credentials()
    drive_svc = drive.drive_service(creds)
    sheets_svc = sheets.sheets_service(creds)

    print(f"\nFinding latest Tracker in: {' / '.join(DRIVE_FOLDER_PATH)}")
    tracker = drive.find_latest_tracker(drive_svc, DRIVE_FOLDER_PATH)
    project_name = drive.extract_project_name(tracker["name"])
    print(f"  Found: {tracker['name']}")
    print(f"  Modified: {tracker['modifiedTime']}")
    print(f"  Project: {project_name}")

    print("\nReading Master Schedule tab...")
    phases = sheets.read_master_schedule(sheets_svc, tracker["id"])
    print(f"  {len(phases)} phases loaded")

    print("Reading Order Schedule tab...")
    orders = sheets.read_order_schedule(sheets_svc, tracker["id"])
    print(f"  {len(orders)} orders loaded")

    print("\nComputing dashboard metrics...")
    metrics = sheets.compute_dashboard_metrics(phases, orders=orders)
    print(f"  Health:           {metrics['health_emoji']} {metrics['health']}")
    print(f"  Current stage:    {metrics['current_stage']}")
    print(f"  Current status:   {metrics['current_status']}")
    print(f"  % Complete:       {int(metrics['pct_complete'])}%")
    print(f"  Phases done:      {metrics['n_done_phases']} of "
          f"{metrics['n_total_phases']}")
    print(f"  Issues:           {metrics['n_blocked_phases']} blocked, "
          f"{metrics['n_delayed_phases']} delayed")
    print(f"  Overdue orders:   {len(metrics['overdue_orders'])}")
    print(f"  Next action:      {metrics['next_action']}")

    print("\nEnsuring Dashboard tab exists...")
    dashboard_sheet_id = sheets.ensure_dashboard_tab(sheets_svc, tracker["id"])

    print("\nWriting dashboard to sheet...")
    sheets.write_dashboard(sheets_svc, tracker["id"], dashboard_sheet_id,
                           metrics, project_name)

    print("\nApplying visual formatting (colors, dates, conditional rules)...")
    n = sheets.apply_visual_formatting(sheets_svc, tracker["id"])
    print(f"  {n} formatting requests applied across data tabs.")

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)
    print(f"Sheet:  {tracker['webViewLink']}")
    print("Update Master Schedule statuses → re-run hb-dashboard → Dashboard refreshes.")
    print()


if __name__ == "__main__":
    main()
