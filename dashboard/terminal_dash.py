"""
dashboard/terminal_dash.py - Rich terminal dashboard.
"""
from datetime import datetime
from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()


def _jobs_panel(db_path):
    from database import db
    stats = db.get_job_stats(db_path)
    t = Table(box=box.SIMPLE, show_header=False, padding=(0,1))
    t.add_column("", style="dim")
    t.add_column("", style="bold green")
    t.add_row("Total jobs", str(stats["total"]))
    t.add_row("Added today", str(stats["today"]))
    for s in stats.get("by_source", []):
        t.add_row(f"  {s['source']}", str(s["n"]))
    return Panel(t, title="[bold cyan]Jobs Found", border_style="cyan")


def _outreach_panel(db_path):
    from database import db
    stats = db.get_outreach_stats(db_path)
    contact_stats = db.get_contact_stats(db_path)
    t = Table(box=box.SIMPLE, show_header=False, padding=(0,1))
    t.add_column("", style="dim")
    t.add_column("", style="bold yellow")
    t.add_row("Contacts", str(contact_stats["total"]))
    t.add_row("High confidence", str(contact_stats["high_confidence"]))
    if stats:
        for s in stats:
            t.add_row(s["status"], str(s["n"]))
    else:
        t.add_row("No outreach yet", "-")
    return Panel(t, title="[bold yellow]Outreach CRM", border_style="yellow")


def _recent_jobs_table(db_path, limit=15):
    from database import db
    conn = db.get_connection(db_path)
    rows = conn.execute(
        """
        SELECT
            j.title,j.company,j.location,j.source,j.keyword_matched,j.found_at,
            c.email AS contact_email,
            c.confidence_score AS contact_score
        FROM jobs j
        LEFT JOIN contacts c ON c.id = (
            SELECT c2.id
            FROM contacts c2
            WHERE c2.opportunity_id = j.id
            ORDER BY c2.confidence_score DESC, c2.last_seen_at DESC
            LIMIT 1
        )
        ORDER BY j.found_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    t = Table(title="[bold]Recent Leads", box=box.ROUNDED, header_style="bold magenta")
    t.add_column("Title",   style="white",  max_width=30)
    t.add_column("Company", style="cyan",   max_width=22)
    t.add_column("Location",style="dim",    max_width=14)
    t.add_column("Source",  style="green",  max_width=13)
    t.add_column("Contact", style="yellow", max_width=28)
    t.add_column("Score",   style="bold",   justify="right", max_width=5)
    t.add_column("Found",   style="dim",    max_width=16)
    for r in rows:
        score = str(r["contact_score"]) if r["contact_score"] is not None else "-"
        t.add_row(r["title"], r["company"], r["location"] or "-",
                  r["source"], r["contact_email"] or "-", score,
                  (r["found_at"] or "")[:16])
    return t


def _contacts_table(db_path, min_confidence=0, limit=15):
    from database import db
    rows = db.get_contacts(db_path, min_confidence=min_confidence, limit=limit)
    t = Table(
        title=f"[bold]Public Hiring Contacts (confidence >= {min_confidence})",
        box=box.ROUNDED,
        header_style="bold green",
    )
    t.add_column("Score", style="bold", justify="right", max_width=5)
    t.add_column("Company", style="cyan", max_width=22)
    t.add_column("Email", style="yellow", max_width=30)
    t.add_column("Source", style="green", max_width=14)
    t.add_column("Opportunity", style="white", max_width=35)
    for r in rows:
        t.add_row(
            str(r["confidence_score"]),
            r["company_name"] or "-",
            r["email"] or "-",
            r["source_type"] or "-",
            (r.get("opportunity_title") or "-")[:35],
        )
    if not rows:
        t.add_row("-", "No contacts yet", "-", "-", "-")
    return t


def _hr_table(db_path):
    from database import db
    conn = db.get_connection(db_path)
    rows = conn.execute(
        "SELECT name,company,designation,email,added_at FROM hr_contacts ORDER BY added_at DESC LIMIT 15"
    ).fetchall()
    conn.close()
    t = Table(title="[bold]HR Contacts", box=box.ROUNDED, header_style="bold magenta")
    t.add_column("Name",        style="white",  max_width=22)
    t.add_column("Company",     style="cyan",   max_width=25)
    t.add_column("Designation", style="yellow", max_width=25)
    t.add_column("Email",       style="green",  max_width=28)
    for r in rows:
        t.add_row(r["name"], r["company"] or "-", r["designation"] or "-", r["email"] or "-")
    return t


def _placement_intel_table(db_path):
    from database import db
    conn = db.get_connection(db_path)
    rows = conn.execute(
        "SELECT institute,company,role,profile_type,ctc_lpa,year "
        "FROM placement_data ORDER BY ctc_lpa DESC LIMIT 20"
    ).fetchall()
    conn.close()
    t = Table(title="[bold]Placement Intel (from PDFs)", box=box.ROUNDED, header_style="bold blue")
    t.add_column("Institute", style="cyan",   max_width=12)
    t.add_column("Company",   style="white",  max_width=25)
    t.add_column("Role",      style="yellow", max_width=30)
    t.add_column("Profile",   style="green",  max_width=12)
    t.add_column("CTC (LPA)", style="bold",   max_width=10)
    t.add_column("Year",      style="dim",    max_width=8)
    for r in rows:
        ctc = f"{r['ctc_lpa']:.1f}" if r["ctc_lpa"] else "-"
        t.add_row(r["institute"], r["company"], r["role"], r["profile_type"], ctc, r["year"] or "-")
    return t


def show(db_path):
    console.clear()
    now = datetime.now().strftime("%d %b %Y  %H:%M")
    console.print(Panel(
        Text(f"DoHSS IITM  Placement Intelligence Pipeline  |  {now}", justify="center", style="bold white"),
        border_style="bright_blue"
    ))
    console.print(Columns([_jobs_panel(db_path), _outreach_panel(db_path)], expand=True))
    console.print()
    console.print(_recent_jobs_table(db_path))
    console.print()
    console.print(_contacts_table(db_path, min_confidence=0))
    console.print()
    console.print(_hr_table(db_path))
    console.print()
    console.print(_placement_intel_table(db_path))
    console.print()
    console.print("[dim]Commands: python main.py run | fetch-now | enrich-contacts | contacts [min_confidence] | dashboard | add-hr | test-telegram[/dim]")
