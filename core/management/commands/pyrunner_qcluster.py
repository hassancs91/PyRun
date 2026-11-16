"""
Start the django-q cluster with PyRun's DB-backed Workers settings applied.

This wraps django-q2's ``qcluster`` command because the values saved on
Settings → Workers cannot take effect any other way: Q_CLUSTER is computed at
settings-module import, inside ``django.setup()``, before the app registry —
and thus the database — exists, in every process. By the time a management
command runs, apps are ready, so this command reads the GlobalSettings row
(plus the fleet's largest script timeout, for the retry safety floor), patches
``django_q.conf.Conf``, and only then starts the cluster.

A plain ``manage.py qcluster`` still works but runs on the env defaults only —
production (entrypoint.sh) and the restart-workers flow use this command.

``--reconcile-orphans`` (passed by entrypoint.sh, the all-in-one container)
first fails RUNNING runs whose script process is gone — see
``Run.reconcile_orphans_at_worker_start``. Workers started separately (split
worker containers) leave the flag off and keep the deadline rule.

Once the cluster is up, this process writes the worker heartbeat itself until
the cluster stops (``core.services.worker_heartbeat``), so workers that are all
busy with long runs still read as running.
"""

import os

import psutil
from django.conf import settings
from django.db import connections
from django_q.cluster import Cluster
from django_q.management.commands.qcluster import Command as QClusterCommand

from core.services.worker_config import apply_db_worker_settings
from core.services.worker_heartbeat import ClusterHeartbeat


class Command(QClusterCommand):
    help = (
        "Starts a Django Q Cluster with PyRun's DB-backed worker settings "
        "(Settings → Workers) applied."
    )

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--reconcile-orphans",
            action="store_true",
            help="Before starting, fail runs still marked running whose script "
            "process no longer exists in this container. Only for a worker that "
            "shares its process namespace with every run it started (the "
            "standard single container).",
        )

    def handle(self, *args, **options):
        self._lead_process_group()
        if options.get("reconcile_orphans"):
            self._reconcile_orphans()
        try:
            config = apply_db_worker_settings()
            self.stdout.write(
                "Worker settings applied: workers={workers} timeout={timeout} "
                "retry={retry} queue_limit={queue_limit}".format(**config)
            )
        except Exception as e:
            # A fresh install (migrations not run yet) must still boot a
            # cluster; env defaults are the safe fallback and the next restart
            # picks the DB values up.
            self.stderr.write(
                f"Could not apply DB worker settings ({e}); "
                "starting with environment defaults."
            )
        self._run_cluster(options)

    @staticmethod
    def _run_cluster(options):
        """django-q's ``qcluster`` handle, plus the heartbeat while the cluster runs.

        After ``start()`` django-q's own command returns and the process just
        waits for the sentinel to exit; here it writes the heartbeat meanwhile.
        A SIGTERM/SIGINT still goes to ``Cluster.sig_handler`` (stop + join),
        and the loop ends within half a second of it. Matches django-q2
        1.11's handle (the version requirements.txt installs), including its
        early ``settings.SECRET_KEY`` check.
        """
        # django-q2 1.11: refuse to start without a SECRET_KEY (tasks are signed).
        settings.SECRET_KEY  # noqa: B018
        cluster_name = options.get("cluster_name")
        if cluster_name:
            os.environ["Q_CLUSTER_NAME"] = cluster_name
        # The sentinel is forked from this process: it must not inherit the
        # connection used above (it closes its copy, which would end ours too).
        connections.close_all()
        cluster = Cluster()
        cluster.start()
        if options.get("run_once", False):
            cluster.stop()
            return
        ClusterHeartbeat(cluster).run()

    @staticmethod
    def _lead_process_group():
        """Make this process the leader of its own process group (POSIX only).

        django-q's sentinel, pusher, monitor and workers are children of this
        process and inherit the group. If this process is killed hard (kill -9,
        an OOM kill) those children are not: they live on, reparented, and keep
        working. entrypoint.sh's monitor then kills the whole group by this
        PID before it starts a replacement, so a crash never leaves two
        clusters running side by side. Scripts are not affected: they start in
        their own session. Already a group or session leader (for example PID 1
        in a worker-only container): nothing to do.
        """
        if not hasattr(os, "setpgid"):
            return
        try:
            if os.getpgid(0) != os.getpid():
                os.setpgid(0, 0)
        except OSError:
            pass  # a session leader cannot move; it already leads its group

    def _reconcile_orphans(self):
        """Fail runs orphaned by a container or worker restart. Never fatal."""
        from core.models import Run

        try:
            started = psutil.Process(os.getpid()).create_time()
            count = Run.reconcile_orphans_at_worker_start(started)
            if count:
                self.stdout.write(
                    f"Marked {count} run(s) failed: they were running when the "
                    "worker stopped and their process is gone."
                )
        except Exception as e:
            self.stderr.write(f"Could not reconcile orphaned runs ({e}); continuing.")
