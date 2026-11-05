"""Add Listing.requested_checks for operator-requested verification."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("listings", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="listing",
            name="requested_checks",
            field=models.JSONField(default=list),
        ),
    ]
