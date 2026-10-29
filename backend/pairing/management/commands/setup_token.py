"""Print the one-time operator setup token (uninitialized installs only).

The dev startup script runs this before launching Django so the local
operator can copy the token from their own terminal into the mother app.
The token is never logged anywhere else and cannot be recovered after the
process exits — use --regenerate to issue a fresh one.
"""

from django.core.management.base import BaseCommand, CommandError

from pairing import services


class Command(BaseCommand):
    help = "Show (or regenerate) the one-time operator setup token."

    def add_arguments(self, parser):
        parser.add_argument(
            "--regenerate",
            action="store_true",
            help="Invalidate any previously issued setup token and make a new one.",
        )

    def handle(self, *args, **options):
        if services.get_operator().initialized and not options["regenerate"]:
            self.stdout.write(
                "Operator already initialized. Re-run with --regenerate to issue "
                "a single-use re-entry token (e.g. expired session, new browser)."
            )
            return
        token = services.ensure_setup_token(regenerate=options["regenerate"])
        if token is None:
            raise CommandError(
                "A setup token was already issued and cannot be recovered. "
                "Re-run with --regenerate to invalidate it and issue a new one."
            )
        self.stdout.write(
            "One-time operator setup token (enter it in the mother app setup page):"
        )
        self.stdout.write(token)
