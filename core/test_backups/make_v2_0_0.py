r"""Make the PyRun 2.0.0 backups in this folder (used by core/test_backup_from_2_0.py).

Runs inside the released image, never in this codebase:

    docker run --rm -e ENGINE=sqlite -e SECRET_KEY=ci-not-a-real-secret-key \
        -e ENCRYPTION_KEY=QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE= -e DEBUG=True \
        -v "$PWD/core/test_backups:/scratch" --entrypoint sh hasanaboulhasan/pyrun:2.0.0 \
        -c "python manage.py migrate --noinput && python manage.py shell < /scratch/make_v2_0_0.py"

For Postgres add ``-e ENGINE=postgres -e DATABASE_URL=postgres://...`` pointing at a
postgres:16 container on the same docker network. The backup is written to
/scratch/v2_0_0_<ENGINE>.json. No real credentials: secret values are fixed
fixture strings encrypted with the CI ENCRYPTION_KEY (above), the webhook tokens
are fixed strings and the password is a throwaway one.
"""
import os
from datetime import timedelta

from django.utils import timezone

from core.brain import sync as brain_sync
from core.brain.service import BrainService
from core.models import (
    DataStore, DataStoreEntry, Environment, GlobalSettings, Run, Script, ScriptSchedule, Secret, User,
    Workspace,
)
from core.services.backup_service import BackupService

engine = os.environ["ENGINE"]
gs = GlobalSettings.get_settings()
gs.instance_name = f"t47 fixture ({engine})"
gs.setup_completed = True
gs.save()

admin = User.objects.create_superuser(username="admin@example.com", email="admin@example.com", password="t47-throwaway-password")
ws = Workspace.get_default()
env = Environment.objects.create(
    name="default", is_default=True, python_version="3.12", workspace=ws, created_by=admin,
    requirements="requests==2.32.3\n",
)

scripts = []
for i, (name, code) in enumerate([
    ("Hello", "print('hello from 2.0.0')\n"),
    ("Reads a secret", "import os\nprint(len(os.environ.get('API_KEY', '')))\n"),
    ("Weekly report", "print('report')\n"),
], start=1):
    s = Script.objects.create(
        name=name, code=code, environment=env, workspace=ws, created_by=admin, timeout_seconds=300,
        webhook_token=f"fixture-webhook-token-{i}",
    )
    scripts.append(s)

modes = [
    {"run_mode": "interval", "interval_minutes": 15},
    {"run_mode": "daily", "daily_times": ["09:00", "17:30"], "timezone": "UTC"},
    {"run_mode": "weekly", "weekly_days": [0, 3], "weekly_times": ["08:15"], "timezone": "UTC"},
]
for s, mode in zip(scripts, modes):
    ScriptSchedule.objects.update_or_create(script=s, defaults={"workspace": ws, "is_active": True, **mode})

api_key = Secret(key="API_KEY", workspace=ws, created_by=admin, description="fixture value")
api_key.set_value("fixture-not-a-real-key")
api_key.save()
gh = Secret(key="GITHUB_TOKEN", workspace=ws, created_by=admin)
gh.set_value("fixture-not-a-real-token")
gh.save()

ds = DataStore.objects.create(name="settings", workspace=ws, created_by=admin, description="fixture store")
for key, value in [("greeting", "hi"), ("count", 3), ("flags", {"beta": True, "list": [1, 2]})]:
    entry = DataStoreEntry(datastore=ds, key=key)
    entry.set_value(value)
    entry.save()

now = timezone.now()
for i, s in enumerate(scripts[:2]):
    Run.objects.create(
        script=s, workspace=ws, status="success", exit_code=0, stdout=f"run {i}\n", stderr="",
        started_at=now - timedelta(minutes=10 - i), ended_at=now - timedelta(minutes=9 - i),
        code_snapshot=s.code, triggered_by=admin, trigger_type="manual",
    )

brain = BrainService.create(ws, "notes", name="Notes", author=admin)
brain_sync.configure(ws, brain.pk, repository="example/brain-notes", branch="main", secret=gh)

data = BackupService.create_backup(
    include_runs=True, max_runs=1000, include_package_operations=False, include_datastores=True,
    created_by_user=admin,
)
raw, _ = BackupService.serialize_backup(data, format=BackupService.FORMAT_JSON)
with open(f"/scratch/v2_0_0_{engine}.json", "wb") as fh:
    fh.write(raw)
print("WROTE", engine, data["backup_metadata"]["version"], len(raw))
