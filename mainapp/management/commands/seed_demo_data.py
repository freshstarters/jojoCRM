"""Management command to idempotently seed non-production demo QA workspaces."""

import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from mainapp.service.demo_seed_service import (
    DEFAULT_DEV_DEMO_PASSWORD,
    DEMO_PASSWORD_ENV_VAR,
    DemoSeedError,
    seed_demo_workspaces,
)


class Command(BaseCommand):
    help = (
        'Idempotently populate non-production Ghanaian demo workspaces (Owner, Staff, and CRM records) '
        'across all 20 business types or selected packs for manual QA.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset',
            action='store_true',
            default=False,
            help='Delete and cleanly recreate only the targeted demo workspaces without touching non-seeded workspaces.',
        )
        parser.add_argument(
            '--only',
            action='append',
            dest='only_packs',
            metavar='PACK_KEY',
            help=(
                'Limit seeding to one or more business-type pack keys (repeatable or comma-separated, '
                'e.g. --only general_services --only healthcare_wellness).'
            ),
        )
        parser.add_argument(
            '--force',
            action='store_true',
            default=False,
            help=(
                'Allow running when DEBUG=False (for staging/QA only; requires --password or '
                f'{DEMO_PASSWORD_ENV_VAR}).'
            ),
        )
        parser.add_argument(
            '--password',
            type=str,
            default=None,
            help=(
                'Password for newly created demo accounts. Defaults to '
                f'{DEFAULT_DEV_DEMO_PASSWORD} when DEBUG=True unless {DEMO_PASSWORD_ENV_VAR} is set.'
            ),
        )

    def handle(self, *args, **options):
        reset = bool(options.get('reset'))
        only_packs = options.get('only_packs')
        force = bool(options.get('force'))
        password = options.get('password')

        try:
            summaries = seed_demo_workspaces(
                reset=reset,
                only_packs=only_packs,
                force=force,
                password=password,
            )
        except DemoSeedError as exc:
            raise CommandError(str(exc)) from exc

        action_label = 'Reset & seeded' if reset else 'Seeded'
        self.stdout.write(
            self.style.SUCCESS(
                f'{action_label} {len(summaries)} demo workspace(s):'
            )
        )
        for item in summaries:
            status_tag = 'reset' if item.reset_performed else ('created' if item.created_workspace else 'verified')
            c = item.counts
            accounts_str = f"owner={item.owner_email}, staff={item.staff_email}"
            if item.coordinator_email:
                accounts_str += f", coordinator={item.coordinator_email}"
            self.stdout.write(
                f'  - [{item.pack_key}] {item.workspace_name} ({item.workspace_slug}) [{status_tag}] '
                f'| {accounts_str} '
                f'| Acc={c["accounts"]} Con={c["contacts"]} Lead={c["leads"]} Deal={c["deals"]} '
                f'Job={c["jobs"]} Apt={c["appointments"]} Vis={c["visits"]} Tck={c["tickets"]} '
                f'Act={c["activities"]} Aud={c["audit_logs"]}'
            )

        if settings.DEBUG and not password:
            env_password = os.environ.get(DEMO_PASSWORD_ENV_VAR, '').strip()
            if env_password:
                self.stdout.write(
                    f'Dev demo password (newly created users): loaded from {DEMO_PASSWORD_ENV_VAR}.'
                )
            else:
                self.stdout.write(
                    f'Dev demo password (newly created users): {DEFAULT_DEV_DEMO_PASSWORD} '
                    f'(override with --password or {DEMO_PASSWORD_ENV_VAR}).'
                )
