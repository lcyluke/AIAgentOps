"""Fleet CLI commands — registered on main app."""
from apex.core.fleet import FleetManager
import typer, json, subprocess
from pathlib import Path

def register_fleet(app: typer.Typer):
    """Add fleet/origin/project commands to the main app."""

    @app.command()
    def fleet_init(
        name: str = typer.Option(..., "--name", "-n"),
        git_repo: str = typer.Option(..., "--repo", "-r"),
    ):
        fm = FleetManager()
        r = fm.init_fleet(name, git_repo)
        typer.echo(f"✅ Fleet '{name}' — Origin ready. Others: apex fleet-join --repo {git_repo}" if r["ok"] else f"❌ {r.get('error')}")

    @app.command()
    def fleet_join(
        git_repo: str = typer.Option(..., "--repo", "-r"),
    ):
        fm = FleetManager()
        r = fm.join_fleet(git_repo)
        typer.echo(f"✅ Joined as worker" if r["ok"] else f"❌ {r.get('error')}")

    @app.command()
    def fleet_status():
        fm = FleetManager()
        r = fm.fleet_status()
        if r["ok"]:
            typer.echo(f"\n⚓ {r['fleet_name']} — {r['total_nodes']} nodes — Origin: {r['origin']}")
            for n in r["nodes"]:
                icon = "👑" if n["role"] == "origin" else "🖥"
                typer.echo(f"  {icon} {n['hostname']:<20} {n['role']:<8} projects:{n['projects']} agents:{n['agents']}")
        else:
            typer.echo(f"❌ {r.get('error')}")

    @app.command()
    def fleet_sync():
        fm = FleetManager()
        r = fm.sync_config()
        typer.echo("✅ Synced" if r["ok"] else f"❌ {r.get('error')}")

    @app.command()
    def origin_status():
        fm = FleetManager()
        is_origin = fm.is_origin()
        typer.echo(f"👑 Origin: {'YES' if is_origin else 'NO — this is a worker'}")

    @app.command()
    def origin_request(
        reason: str = typer.Option("", "--reason", "-r"),
    ):
        fm = FleetManager()
        r = fm.request_origin(reason)
        if r["ok"]:
            typer.echo(f"📋 Request: {r['request_code']}")
            typer.echo(f"   Current Origin: apex origin-approve {r['request_code']}")
        else:
            typer.echo(f"❌ {r.get('error')}")

    @app.command()
    def origin_approve(
        request_code: str = typer.Argument(...),
    ):
        fm = FleetManager()
        r = fm.approve_origin(request_code)
        typer.echo(f"✅ Origin transferred: {r.get('old_origin')} → {r.get('new_origin')}" if r["ok"] else f"❌ {r.get('error')}")

    @app.command()
    def project_init(
        name: str = typer.Argument(...),
        pm_agent: str = typer.Option("pm-default", "--pm", "-p"),
        template: str = typer.Option("webapp", "--template", "-t"),
        cwd: str = typer.Option(".", "--dir", "-d"),
    ):
        project_dir = Path(cwd) / name
        project_dir.mkdir(parents=True, exist_ok=True)
        for sub in ["src", "tests", "docs"]:
            (project_dir / sub).mkdir(exist_ok=True)
        (project_dir / "README.md").write_text(f"# {name}\n")
        subprocess.run(["git", "-C", str(project_dir), "init"], capture_output=True)
        fm = FleetManager()
        fm.register_project(name, pm_agent)
        team = {"webapp": ["pm","frontend","backend","devops"], "data":["engineer","analyst","scientist","ml"]}.get(template, ["architect","coder"])
        typer.echo(f"✅ {name} — PM:{pm_agent} — Team:{','.join(team)}")

    @app.command()
    def project_team(name: str = typer.Argument(...)):
        typer.echo(f"📋 {name}: PM → decompose → assign → agents → report → verify")

    @app.command()
    def project_report(name: str = typer.Argument(...)):
        typer.echo(f"📊 {name}: Active — run apex fleet-status for overview")
